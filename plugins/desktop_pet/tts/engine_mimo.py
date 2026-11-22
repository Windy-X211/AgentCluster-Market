# coding=utf-8
"""在线 TTS：小米 MiMo-V2.5-TTS 系列（OpenAI 兼容 /chat/completions）。

接口与 engine_edge 保持同一套签名（synth_segment 返回 int16 PCM/sr/words/ttfa），
由 tts/service.py 的 _run_mimo 按句调度，所以上层字幕、口型、BGM 全都不用改。

设计要点：
  * **零第三方依赖**：只用 urllib / json / base64 / wave + numpy，不装 requests。
    插件宿主进程（PyQt5）里不保证有 requests，多装一个包就多一个装不上就崩的风险。
  * **一次请求合成一句**：MiMo 走 /chat/completions，目标文本必须放在 role=assistant，
    服务端按"消息"整段推理，不是原生分流式；上层按 ≤24 字切句、并发预取已经够快。
  * **风格控制**：user 消息是自然语言指令（语气/情绪/语速/角色扮演），
    目标文本里的 (风格) 与 [音频标签] 原样透传（MiMo 原生支持）。
  * **没有逐字时间戳**：words 返回 None，字幕由上层按字数均分（与 Edge 缺 WordBoundary 时一致）。
  * **语速没有官方参数**：把 speed 折成一句中文指令塞进 user 消息，属于"近似控制"，
    不保证精确倍数 —— 这一点写在 docstring 和 TTS_MIMO.md 里，不假装是精确变速。

自检（需要 key）：
  python -m tts.engine_mimo --text "你好，我是桌宠。" --out out.wav
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import wave
from typing import Dict, List, Optional, Tuple

import numpy as np

MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"

# 三个模型的功能边界（见官方 quick-start）：
#   mimo-v2.5-tts            预置音色，支持唱歌模式
#   mimo-v2.5-tts-voicedesign 用文本描述设计音色（user 消息必填 = 音色描述）
#   mimo-v2.5-tts-voiceclone  用音频样本复刻音色（voice 传 data:audio/...;base64,xxx）
MIMO_MODEL = "mimo-v2.5-tts"
MIMO_MODELS = {
    "mimo-v2.5-tts": "预置精品音色（默认），支持 (唱歌) 标签",
    "mimo-v2.5-tts-voicedesign": "用文本描述设计音色，不支持预置音色/克隆",
    "mimo-v2.5-tts-voiceclone": "用音频样本复刻音色，不支持预置音色/设计",
}

# 预置音色（Voice ID 就是显示名；中国集群的 mimo_default = 冰糖）
MIMO_DEFAULT_VOICE = "冰糖"
MIMO_VOICES: List[Dict[str, object]] = [
    {"id": "冰糖", "gender": "女", "trait": "中文女声，清晰自然，日常对话首选（默认）"},
    {"id": "茉莉", "gender": "女", "trait": "中文女声，柔和细腻，适合陪伴、安抚"},
    {"id": "苏打", "gender": "男", "trait": "中文男声，明亮偏年轻，适合少年感"},
    {"id": "白桦", "gender": "男", "trait": "中文男声，沉稳厚重，适合叙事、播报"},
    {"id": "Mia", "gender": "女", "trait": "英文女声，自然"},
    {"id": "Chloe", "gender": "女", "trait": "英文女声，活泼上扬"},
    {"id": "Milo", "gender": "男", "trait": "英文男声，年轻"},
    {"id": "Dean", "gender": "男", "trait": "英文男声，低沉"},
    {"id": "mimo_default", "gender": "女", "trait": "集群默认音色（中国集群=冰糖）"},
]

_MIMO_IDS = {str(v["id"]).lower(): str(v["id"]) for v in MIMO_VOICES}
# 常见别名：别让智能体写个中文名就被打回默认音色
_ALIASES = {
    "default": "mimo_default",
    "auto": "mimo_default",
    "bingtang": "冰糖",
    "冰糖": "冰糖",
    "moli": "茉莉",
    "茉莉": "茉莉",
    "suda": "苏打",
    "苏打": "苏打",
    "baihua": "白桦",
    "白桦": "白桦",
    "mia": "Mia",
    "chloe": "Chloe",
    "milo": "Milo",
    "dean": "Dean",
}


class MimoTTSError(RuntimeError):
    pass


# ---------------------------------------------------------------- 配置
def load_settings(cfg: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """把 tts_config.json 的 online / online.mimo 归一化成一份扁平设置。"""
    if cfg is None:
        try:
            from .config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    node = dict((cfg or {}).get("online") or {})
    mimo = dict(node.get("mimo") or {})
    return {
        "base_url": str(mimo.get("base_url") or MIMO_BASE_URL).rstrip("/"),
        "model": str(mimo.get("model") or MIMO_MODEL),
        # 默认音色（预置音色的 Voice ID，或 voiceclone 的 data:audio/...;base64,...）
        "voice": str(mimo.get("voice") or MIMO_DEFAULT_VOICE),
        # 音色描述（voicedesign 模型用）
        "voice_design": str(mimo.get("voice_design") or ""),
        "style": str(mimo.get("style") or ""),
        "format": str(mimo.get("format") or "wav").lower(),
        "api_key": str(mimo.get("api_key") or ""),
        "api_key_env": str(mimo.get("api_key_env") or "MIMO_API_KEY"),
        "api_key_file": str(mimo.get("api_key_file") or ""),
        "timeout_s": float(mimo.get("timeout_s") or 30.0),
        "sample_rate": int(mimo.get("sample_rate") or 48000),
        "parallel": int(mimo.get("parallel") or 2),
        # 说话是否走流式（stream=True + pcm16）。唱歌永远不走：实测唱歌流式只回 1 块
        "stream": bool(mimo.get("stream", True)),
        # 流式首块到达时字幕还拿不到真实时长，先按"字数 × 这个毫秒数"估
        "stream_est_ms_per_char": int(mimo.get("stream_est_ms_per_char") or 260),
        # 唱歌一次请求最多带多少字（0 = 整曲一次不拆）。MiMo 唱歌是"整段推理"，
        # 实测 0.29 秒/字：100 字要等 29 秒才出第一个音，只能靠拆句把首声拉回来。
        "sing_split_chars": int(mimo.get("sing_split_chars", 28) or 0),
        # 长歌词拆句后，第一句再压到多少字（0 = 不额外压），首声延迟 ≈ 字数 × 0.29s
        "sing_first_chars": int(mimo.get("sing_first_chars", 20) or 0),
        # 唱歌进度气泡用的估算速率（毫秒/字）：只影响桌宠头顶"唱歌合成 xx%"的显示，
        # 不参与合成。实测 0.29~0.37 秒/字，取中位 300。
        "sing_ms_per_char": int(mimo.get("sing_ms_per_char", 300) or 300),
        # 唱歌等待期垫场（背景音 + "清嗓子"配音）。音色/台词只影响 _tts/sing_wait 下的
        # 缓存 wav，与合成请求无关。
        "sing_wait_bgm": bool(mimo.get("sing_wait_bgm", True)),
        "sing_wait_scene": str(mimo.get("sing_wait_scene") or "ambient"),
        "sing_wait_volume": float(mimo.get("sing_wait_volume", 0.55) or 0.55),
        "sing_wait_voice": bool(mimo.get("sing_wait_voice", True)),
        "sing_wait_line": str(mimo.get("sing_wait_line") or ""),
        # 输出峰值上限（0.88 = 88% 满量程）：MiMo 唱歌常把峰值打满 32767，
        # 与 BGM 相加后必被削平，听感就是刺耳的失真
        "headroom": float(mimo.get("headroom", 0.88) or 0.0),
        "speed_hint": bool(mimo.get("speed_hint", True)),
        "trim_silence": bool(mimo.get("trim_silence", True)),
        "optimize_text_preview": bool(mimo.get("optimize_text_preview", False)),
        "proxy": str(mimo.get("proxy") or node.get("proxy") or ""),
    }


def find_api_key(settings: Optional[Dict[str, object]] = None,
                 override: str = "") -> str:
    """取 key：本次请求传入 > 配置项 > api_key_file > 环境变量。

    override 是"调用时临时带上的 key"，只活在这一个请求里，不落盘、不回显。
    """
    key = str(override or "").strip()
    if key:
        return key
    st = settings or load_settings()
    key = str(st.get("api_key") or "").strip()
    if key:
        return key
    path = str(st.get("api_key_file") or "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except Exception:
            pass
    env_name = str(st.get("api_key_env") or "MIMO_API_KEY")
    return (os.environ.get(env_name) or os.environ.get("MIMO_API_KEY") or "").strip()


def key_source(settings: Optional[Dict[str, object]] = None) -> str:
    """配置里的 key 到底来自哪儿：config / file / env / 空（不看调用时传入的）。"""
    st = settings or load_settings()
    if str(st.get("api_key") or "").strip():
        return "config"
    path = str(st.get("api_key_file") or "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read().strip():
                    return "file"
        except Exception:
            pass
    if (os.environ.get(str(st.get("api_key_env") or "MIMO_API_KEY"))
            or os.environ.get("MIMO_API_KEY")):
        return "env"
    return ""


def resolve_voice(name: object, default: str = "") -> str:
    """音色名归一化；不认识的回落默认音色（绝不把错名字丢给服务端）。

    也允许直接传 voiceclone 的 `data:audio/...;base64,...`（原样透传）。
    """
    s = str(name or "").strip()
    if s.startswith("data:audio/"):
        return s
    low = s.lower()
    if low in _MIMO_IDS:
        return _MIMO_IDS[low]
    if low in _ALIASES:
        return _ALIASES[low]
    d = str(default or "").strip()
    if d and d.lower() in _MIMO_IDS:
        return _MIMO_IDS[d.lower()]
    return MIMO_DEFAULT_VOICE


# ---------------------------------------------------------------- 指令拼装
_SING_TAGS = ("唱歌", "sing", "singing")
# 括号风格标签可用的三种写法（文档：半角 ()、全角（）、方括号 []）
_TAG_CLOSERS = {"(": ")", "（": "）", "[": "]"}


def _tag_parts(tag: str) -> List[str]:
    """把一对括号里的风格名拆成列表：分隔符不限（逗号/顿号/斜杠/空格都算）。"""
    parts: List[str] = []
    for chunk in (str(tag or "").replace("，", ",").replace("、", ",").replace("/", ",").split(",")):
        for p in chunk.split():
            p = p.strip()
            if p:
                parts.append(p)
    return parts


def _lead_tag(text: str) -> Tuple[str, str]:
    """拆出文本开头的一对方括号风格标签，返回 (标签内容, 标签之后的正文)。

    括号太长（>40 字）或没闭合就当正文，避免把一整句话错当标签吃掉。
    """
    s = str(text or "")
    stripped = s.lstrip()
    if not stripped:
        return "", s
    closer = _TAG_CLOSERS.get(stripped[0])
    if not closer:
        return "", s
    idx = stripped.find(closer)
    if idx <= 1 or idx > 40:
        return "", s
    return stripped[1:idx].strip(), stripped[idx + 1:].lstrip()


def is_singing(text: str) -> bool:
    """文本开头是否已经带了唱歌标签（唱歌/sing/singing，大小写不敏感）。"""
    tag, _body = _lead_tag(text)
    return any(p.lower() in _SING_TAGS for p in _tag_parts(tag))


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_CJK_GAP_RE = re.compile(r"(?<=[\u3400-\u9fff，,。！？；：、])\s+(?=[\u3400-\u9fff])")


def _line_pause_punct(line: str) -> str:
    """一行歌词/台词 → 带停顿标点的一行（只改发给 MiMo 的文本，不碰字幕）。"""
    line = re.sub(r"[ \t]+", " ", str(line or "")).strip()
    if not line:
        return ""
    cjk = bool(_CJK_RE.search(line))
    if cjk:
        # 先收紧标点两侧的空格，避免"你好 ， 世界"
        line = re.sub(r"\s*([，,。！？!?；;…、])\s*", r"\1", line)
        # 中文之间的空格=分句 → 逗号；中文与英文/数字之间的空格保留（"打开 B 站"不能变成"打开，B，站。"）
        line = _CJK_GAP_RE.sub("，", line)
        line = re.sub(r"[，,]{2,}", "，", line)
        line = re.sub(r"([。！？!?；;…])[，,]+", r"\1", line)
        line = re.sub(r"[，,]\s*(?=[。！？!?；;…])", "", line)
        line = re.sub(r"^[，,]+", "", line)
        line = line.rstrip("，,、;；:： ")
        if not line.endswith(("。", "！", "？", "!", "?", "；", ";", "…")):
            line += "。"
    else:
        if not line.endswith((".", "!", "?", ";", "…", "。", "！", "？")):
            line += "."
    return line


def pause_punct(text: str) -> str:
    """把换行变句号、中文句内空格变逗号：MiMo 靠标点停顿，空格它不认。

    只处理标签外的正文，开头的 `(唱歌)/(东北话)` 标签原样保留；
    字幕时间轴仍用调用方原文（本函数只作用于请求体）。
    """
    tag, body = _lead_tag(str(text or ""))
    lines = [_line_pause_punct(ln) for ln in str(body).replace("\r", "\n").split("\n")]
    body_out = "".join(ln for ln in lines if ln)
    # 中文句号后不加空格；只有英文句子之间补一个空格（"star.how" → "star. how"）
    body_out = re.sub(r"([.!?;])(?=[A-Za-z])", r"\1 ", body_out)
    if tag:
        opener = str(text).lstrip()[0]
        return "%s%s%s%s" % (opener, tag, _TAG_CLOSERS.get(opener, ")"), body_out)
    return body_out


_DIALECTS = ("东北话", "四川话", "河南话", "粤语")


def ensure_dialect_tag(text: str, dialect: str = "") -> str:
    """把方言写进开头风格标签。

    说话：`(东北话)` / `(温柔 粤语)`。
    唱歌：绝对不能再加第二个标签，否则标签会被当歌词唱出来；返回原文，
    方言由 `_user_instruction` 的自然语言指令控制。
    """
    d = str(dialect or "").strip()
    if not d:
        return str(text or "")
    tag, body = _lead_tag(text)
    if not tag:
        return "(%s)%s" % (d, str(text).lstrip())
    if d in _tag_parts(tag):
        return str(text or "")
    if is_singing(text):
        return str(text or "")
    opener = str(text).lstrip()[0]
    closer = _TAG_CLOSERS.get(opener, ")")
    return "%s%s %s%s%s" % (opener, tag, d, closer, body)


def _strip_leading_tags(text: str) -> str:
    """去掉正文最前面连续的 `(风格)` 标签（只用于唱歌请求的清洗）。

    实测 MiMo 只可靠识别最开头的一个风格标签：`(唱歌)(粤语)歌词` 会把
    “粤语”两个字当歌词唱出来，而且仍用普通话。所以唱歌请求只保留
    一个 `(唱歌)`，其余标签全部剥掉，方言改由 user 自然语言指令控制。
    """
    s = str(text or "").lstrip()
    for _ in range(4):
        m = re.match(r"^[（(][^)）]{1,30}[)）]\s*", s)
        if not m:
            break
        s = s[m.end():].lstrip()
    return s


def ensure_sing_tag(text: str) -> str:
    """把请求规范成**唯一**一个开头的 `(唱歌)` 标签。

    文档要求 `(唱歌)` 位于最开头；同时实测多标签写法会被当歌词：
      * `(唱歌)(粤语)歌词` → “(粤语)”被唱出来，仍是普通话；
      * `(唱歌 粤语)歌词` → 演唱模式丢失，变成带口音朗读。
    因此唱歌时不再合并其他风格标签，统一输出 `(唱歌)歌词`；
    方言/情绪等附加要求走 style/自然语言指令。
    """
    tag, body = _lead_tag(text)
    if not tag:
        return "(唱歌)" + str(text or "").lstrip()
    body = _strip_leading_tags(body)
    return "(唱歌)" + body


def strip_sing_tag(text: str) -> str:
    """去掉开头的唱歌标签，只留歌词本体（给字幕/状态显示用）。

    标签本身只对合成有意义：`(唱歌)歌词` 是请求格式，念出来/显示出来都是噪音。
    开头若还带着别的风格词（如 `(慵懒 唱歌)`），保留其余风格词，只摘掉唱歌这一项。
    """
    s = str(text or "")
    tag, body = _lead_tag(s)
    if not tag or not is_singing(s):
        return s
    body = _strip_leading_tags(body)
    rest = [p for p in _tag_parts(tag) if p.lower() not in _SING_TAGS]
    if not rest:
        return body
    opener = s.lstrip()[0]
    return "%s%s%s%s" % (opener, " ".join(rest), _TAG_CLOSERS.get(opener, ")"), body)


def _assert_sing_supported(settings: Dict[str, object], model: str) -> None:
    """唱歌只有预置音色模型支持：voicedesign / voiceclone 官方标注不支持。"""
    if "voicedesign" in model or "voiceclone" in model:
        raise MimoTTSError(
            "唱歌只有 mimo-v2.5-tts 支持：当前 model=%s（voicedesign / voiceclone 不支持唱歌）。"
            "请把 online.mimo.model 改回 mimo-v2.5-tts，或去掉 sing 参数" % model)


def _speed_phrase(speed: float) -> str:
    try:
        s = float(speed)
    except Exception:
        return ""
    if s <= 0 or abs(s - 1.0) < 0.05:
        return ""
    pct = int(round(abs(s - 1.0) * 100))
    pct = max(5, min(100, pct))
    return "语速比正常%s约 %d%%，吐字保持清晰。" % ("快" if s > 1 else "慢", pct)


def _user_instruction(settings: Dict[str, object], speed: float, sing: bool = False,
                      style: str = "", dialect: str = "") -> str:
    """拼 user 消息里的自然语言指令。

    style 是"本次调用"临时指定的表演指令（智能体直接写在 speak(style=...) 里），
    优先于配置里的 online.mimo.style；两者都有时都带上，按次的放前面。
    """
    parts: List[str] = []
    extra = str(style or "").strip()
    if extra:
        parts.append(extra)
    cfg_style = str(settings.get("style") or "").strip()
    if cfg_style and cfg_style != extra:
        parts.append(cfg_style)
    # 唱歌不用语速指令：旋律自带节奏，再塞"快约 30%"只会跟演唱打架
    if bool(settings.get("speed_hint", True)) and not sing:
        sp = _speed_phrase(speed)
        if sp:
            parts.append(sp)
    # 方言唱歌时补一句自然语言指令，和 `(唱歌)(方言)` 标签一起双保险
    if sing and str(dialect or "").strip():
        parts.append("请用%s演唱，保留旋律和节奏。" % str(dialect).strip())
    return " ".join(parts).strip()


def _build_payload(text: str, voice: str, speed: float, settings: Dict[str, object],
                   sing: bool = False, style: str = "", stream: bool = False,
                   fmt: str = "", dialect: str = "") -> Dict[str, object]:
    model = str(settings.get("model") or MIMO_MODEL)
    singing = bool(sing) or is_singing(text)
    # MiMo 不认空格当停顿：换行→句号、中文句内空格→逗号。
    # 只处理唱歌或确实带换行/中文空格的文本；普通说话的分段不能每段硬加句号，会多出停顿。
    if singing or "\n" in str(text) or _CJK_GAP_RE.search(str(text)):
        text = pause_punct(text)
    if singing:
        _assert_sing_supported(settings, model)
        text = ensure_sing_tag(text)
    text = ensure_dialect_tag(text, dialect or str(settings.get("dialect") or ""))
    instr = _user_instruction(settings, speed, sing=singing, style=style, dialect=dialect)
    is_design = "voicedesign" in model
    is_clone = "voiceclone" in model
    if is_design and not instr:
        # voicedesign 的 user 消息就是音色描述，必填；没配就用一个中性可用的兜底
        instr = str(settings.get("voice_design") or "").strip() or "自然、亲切的日常对话女声，语速适中。"

    messages: List[Dict[str, str]] = []
    if instr:
        messages.append({"role": "user", "content": instr})
    # 目标文本必须放在 assistant，放在 user 里只会被当成指令、不会被念出来
    messages.append({"role": "assistant", "content": text})

    audio: Dict[str, object] = {"format": str(fmt or settings.get("format") or "wav")}
    if not is_design:
        audio["voice"] = str(voice or settings.get("voice") or MIMO_DEFAULT_VOICE)
    if is_design and bool(settings.get("optimize_text_preview")):
        audio["optimize_text_preview"] = True
    return {"model": model, "messages": messages, "audio": audio, "stream": bool(stream)}


# ---------------------------------------------------------------- HTTP
def _friendly_http_error(code: int, body: str, settings: Dict[str, object]) -> str:
    detail = ""
    try:
        data = json.loads(body or "{}")
        err = data.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("code") or "")
        elif err:
            detail = str(err)
        if not detail:
            detail = str(data.get("msg") or data.get("message") or "")
    except Exception:
        detail = (body or "").strip()[:300]
    tail = ("：%s" % detail) if detail else ""
    if code == 401:
        return ("MiMo 鉴权失败(401)：API Key 无效或没配置。请设置环境变量 %s，"
                "或在 tts_config.json 的 online.mimo.api_key 填上%s"
                % (settings.get("api_key_env") or "MIMO_API_KEY", tail))
    if code == 403:
        return "MiMo 拒绝访问(403)：key 没有该模型权限，或额度/地区受限%s" % tail
    if code == 404:
        return ("MiMo 接口不存在(404)：检查 online.mimo.base_url（默认 %s）与 model 名%s"
                % (MIMO_BASE_URL, tail))
    if code == 429:
        return "MiMo 限流(429)：请求太密，稍等再试%s" % tail
    if code >= 500:
        return "MiMo 服务端错误(%d)%s" % (code, tail)
    return "MiMo 请求失败(%d)%s" % (code, tail)


def _post_json(url: str, payload: Dict[str, object], key: str, proxy: str = "",
               timeout: float = 30.0,
               settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer %s" % key,
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    ) if proxy else urllib.request.build_opener()
    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "replace")
        except Exception:
            err_body = ""
        raise MimoTTSError(_friendly_http_error(int(e.code), err_body, settings or {})) from e
    except urllib.error.URLError as e:
        raise MimoTTSError("MiMo 网络不可达：%s（需要能访问 api.xiaomimimo.com，必要时配 online.mimo.proxy）"
                           % getattr(e, "reason", e)) from e
    except TimeoutError as e:
        raise MimoTTSError("MiMo 请求超时（%.0fs）" % timeout) from e
    except Exception as e:
        raise MimoTTSError("MiMo 请求失败：%s" % e) from e
    try:
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception as e:
        raise MimoTTSError("MiMo 返回的不是合法 JSON：%s" % (raw[:200],)) from e
    data["_elapsed_ms"] = (time.monotonic() - t0) * 1000.0
    return data


def _extract_audio(data: Dict[str, object]) -> Tuple[str, str]:
    """从 /chat/completions 回包取 base64 音频，返回 (base64, format)。"""
    choices = data.get("choices") or []
    if not choices:
        err = data.get("error") or data.get("message") or "没有 choices"
        raise MimoTTSError("MiMo 没有返回音频：%s" % str(err)[:200])
    msg = (choices[0] or {}).get("message") or {}
    audio = msg.get("audio")
    if isinstance(audio, str):
        return audio, ""
    if isinstance(audio, dict):
        b64 = audio.get("data") or audio.get("audio") or ""
        if not b64:
            raise MimoTTSError("MiMo 回包里 audio.data 为空")
        return str(b64), str(audio.get("format") or "")
    raise MimoTTSError("MiMo 回包里没有 message.audio")


# ---------------------------------------------------------------- 音频解码
def _pcm16_mono(raw: bytes, sampwidth: int, channels: int) -> np.ndarray:
    """任意位宽/声道 → int16 单声道。不用 audioop（3.13 已移除）。"""
    if channels <= 0:
        channels = 1
    if sampwidth == 2 and channels == 1:
        return np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=False)
    if sampwidth == 4 and channels == 1:
        return (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
    frames = np.frombuffer(raw, dtype=np.uint8)
    total = frames.size
    stride = sampwidth * channels
    if stride <= 0 or total < stride:
        return np.zeros(0, np.int16)
    n = total // stride
    frames = frames[: n * stride].reshape(n, channels, sampwidth).astype(np.int64)
    if sampwidth == 1:
        ch = (frames[:, :, 0] - 128) << 8
    elif sampwidth == 2:
        b = frames[:, :, :2]
        lo = b[:, :, 0]
        hi = np.where(b[:, :, 1] >= 128, b[:, :, 1] - 256, b[:, :, 1])
        ch = lo + (hi << 8)
    elif sampwidth == 3:
        b = frames[:, :, :3]
        v = b[:, :, 0] + (b[:, :, 1] << 8) + (b[:, :, 2] << 16)
        v = np.where(v >= (1 << 23), v - (1 << 24), v)
        ch = v >> 8
    elif sampwidth == 4:
        b = frames[:, :, :4]
        v = b[:, :, 0] + (b[:, :, 1] << 8) + (b[:, :, 2] << 16) + (b[:, :, 3] << 24)
        v = np.where(v >= (1 << 31), v - (1 << 32), v)
        ch = v >> 16
    else:
        raise MimoTTSError("不支持的 WAV 采样位宽：%d" % sampwidth)
    if channels > 1:
        ch = ch.mean(axis=1)
    return np.clip(ch, -32768, 32767).astype(np.int16)


def _decode_via_ffmpeg(data: bytes, dst_sr: int) -> np.ndarray:
    """wave 打不开（浮点 WAV / 非标准 RIFF）时的兜底：借 ffmpeg 转 s16le。"""
    tmp_in = tmp_out = ""
    try:
        fd, tmp_in = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with open(tmp_in, "wb") as f:
            f.write(data)
        fd, tmp_out = tempfile.mkstemp(suffix=".pcm")
        os.close(fd)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_in,
             "-f", "s16le", "-ac", "1", "-ar", str(int(dst_sr)), tmp_out],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        with open(tmp_out, "rb") as f:
            raw = f.read()
        return np.frombuffer(raw, dtype="<i2").astype(np.int16)
    except FileNotFoundError as e:
        raise MimoTTSError("MiMo 返回的音频不是标准 PCM WAV，且本机没有 ffmpeg 可以兜底解码") from e
    except subprocess.CalledProcessError as e:
        raise MimoTTSError("MiMo 音频解码失败（ffmpeg）：%s"
                           % (e.stderr or b"")[:200].decode("utf-8", "replace")) from e
    finally:
        for p in (tmp_in, tmp_out):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass


def decode_audio(b64: str, fmt: str, dst_sr: int) -> np.ndarray:
    """base64 音频 → int16 单声道 PCM@dst_sr。"""
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception as e:
        raise MimoTTSError("MiMo 返回的音频 base64 解码失败：%s" % e) from e
    if not data:
        raise MimoTTSError("MiMo 返回的音频为空")
    if str(fmt).lower() in ("pcm16", "pcm", "s16le"):
        pcm = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.int16)
        return pcm
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception:
        return _decode_via_ffmpeg(data, dst_sr)
    pcm = _pcm16_mono(raw, int(sw), int(ch))
    if int(sr) != int(dst_sr) and pcm.size:
        from .resample import resample_int16
        pcm = resample_int16(pcm, int(sr), int(dst_sr))
    return pcm


def _limit_peak(pcm: np.ndarray, ceiling: float = 0.88) -> np.ndarray:
    """把峰值压到 ceiling 以内（只衰减、不放大）。

    MiMo 唱歌的回包峰值几乎恒为 32767，直接送进混音（人声 + BGM）就会削波失真。
    这里按峰值整体等比缩放，音色不变，只是留出 headroom。
    """
    if pcm is None or getattr(pcm, "size", 0) == 0 or ceiling <= 0:
        return pcm if pcm is not None else np.zeros(0, np.int16)
    lim = int(32767 * min(1.0, float(ceiling)))
    peak = int(np.max(np.abs(pcm.astype(np.int32))))
    if peak <= lim or peak <= 0:
        return pcm
    return (pcm.astype(np.float32) * (float(lim) / float(peak))).astype(np.int16)


def _trim_silence(pcm: np.ndarray, sr: int, pad_ms: int = 60) -> np.ndarray:
    """裁掉首尾静音（保留 pad_ms 垫音）：MiMo 回包首尾常带 0.1~0.3s 静音，
    不裁的话句与句之间会莫名多一拍，首声也会被推后。"""
    if pcm.size == 0:
        return pcm
    peak = int(np.max(np.abs(pcm))) if pcm.size else 0
    thr = max(200, int(peak * 0.02))
    idx = np.where(np.abs(pcm.astype(np.int32)) > thr)[0]
    if idx.size == 0:
        return pcm
    pad = int(sr * pad_ms / 1000.0)
    start = max(0, int(idx[0]) - pad)
    end = min(pcm.size, int(idx[-1]) + pad + 1)
    if end - start < int(sr * 0.03):
        return pcm
    return pcm[start:end]


# ---------------------------------------------------------------- 对外接口
def synth_segment(text: str, voice: str = "", speed: float = 1.0,
                  sr: int = 48000, settings: Optional[Dict[str, object]] = None,
                  timeout: Optional[float] = None, api_key: str = "", sing: bool = False,
                  style: str = "", dialect: str = ""
                  ) -> Tuple[np.ndarray, int, Optional[List[Dict[str, object]]], float]:
    """合成一句，返回 (int16 PCM@sr, sr, None, ttfa_ms)。

    与 engine_edge.synth_segment 签名一致；words 恒为 None（MiMo 不返回逐字时间戳）。
    api_key 是"本次请求临时带上的 key"，优先于配置/环境变量，不落盘。
    sing=True 时按"唱歌"处理：文本开头补/合并 `(唱歌)` 标签，只有 mimo-v2.5-tts 可用。
    """
    txt = str(text or "").strip()
    if not txt:
        raise MimoTTSError("要合成的文本为空")
    st = settings or load_settings()
    key = find_api_key(st, override=api_key)
    if not key:
        raise MimoTTSError(
            "MiMo 没有 API Key：调用时传 api_key 参数，或设置环境变量 %s，"
            "或在配置里写 online.mimo.api_key" % (st.get("api_key_env") or "MIMO_API_KEY")
        )
    voice_id = resolve_voice(voice or st.get("voice"), str(st.get("voice") or MIMO_DEFAULT_VOICE))
    timeout = float(timeout if timeout is not None else (st.get("timeout_s") or 30.0))
    singing = bool(sing) or is_singing(txt)
    if singing:
        # 整首歌是一次请求（不分句），30s 的单句超时会把长歌词掐断：按字数放宽
        timeout = max(timeout, min(180.0, 20.0 + len(txt) * 0.12))

    payload = _build_payload(txt, voice_id, float(speed or 1.0), st, sing=singing, style=style,
                             dialect=dialect)
    url = "%s/chat/completions" % str(st.get("base_url") or MIMO_BASE_URL).rstrip("/")
    t0 = time.monotonic()
    data = _post_json(url, payload, key, proxy=str(st.get("proxy") or ""),
                      timeout=timeout, settings=st)
    b64, fmt = _extract_audio(data)
    ttfa = (time.monotonic() - t0) * 1000.0

    pcm = decode_audio(b64, fmt or str(st.get("format") or "wav"), int(sr))
    if pcm.size == 0:
        raise MimoTTSError("MiMo 解码后音频长度为 0")
    if bool(st.get("trim_silence", True)):
        pcm = _trim_silence(pcm, int(sr))
    # MiMo 唱歌常把峰值直接打满 32767：播放侧还要叠 BGM，一起过 0dB 就被硬削波，
    # 听感是明显的刺耳"噪音"。这里只做衰减（绝不放大），给混音留出余量。
    pcm = _limit_peak(pcm, float(st.get("headroom") or 0.0))
    return np.ascontiguousarray(pcm, dtype=np.int16), int(sr), None, ttfa


def synth_stream(text: str, voice: str = "", speed: float = 1.0,
                 sr: int = 24000, settings: Optional[Dict[str, object]] = None,
                 timeout: Optional[float] = None, api_key: str = "", style: str = "",
                 dialect: str = "", on_chunk=None, should_stop=None) -> Dict[str, object]:
    """流式合成一句话（**只用于说话**，唱歌不要走这里：实测唱歌只回 1 块）。

    边收边把 24kHz 单声道 int16 PCM 交给 on_chunk(chunk)，调用方按顺序入队即可
    边合成边出声。文档要求流式必须用 pcm16（wav 是整包 RIFF，没法边拼边播）。

    实测（mimo-v2.5-tts，40 字）：首块 1.32s、33 块、平均块长 320ms、音频 10.72s，
    而整包非流式要等全部算完才回 —— 长句的首声差距就在这里。

    注意：流式回包**恒为 24kHz/单声道/16bit**（文档要求 pcm16），`sr` 只用于回报"这块该按
    多少采样率解释"（固定 24k），不做重采样；重采样交给播放引擎（`AudioEngine.enqueue(src_sr=24000)`）。

    返回 {"samples": 样本数, "first_chunk_ms": 首块延迟, "elapsed_ms": 总耗时, "chunks": 块数}。
    """
    txt = str(text or "").strip()
    if not txt:
        raise MimoTTSError("要合成的文本为空")
    st = settings or load_settings()
    key = find_api_key(st, override=api_key)
    if not key:
        raise MimoTTSError(
            "MiMo 没有 API Key：调用时传 api_key 参数，或设置环境变量 %s，"
            "或在配置里写 online.mimo.api_key" % (st.get("api_key_env") or "MIMO_API_KEY")
        )
    voice_id = resolve_voice(voice or st.get("voice"), str(st.get("voice") or MIMO_DEFAULT_VOICE))
    timeout = float(timeout if timeout is not None else (st.get("timeout_s") or 30.0))
    payload = _build_payload(txt, voice_id, float(speed or 1.0), st, style=style,
                             stream=True, fmt="pcm16", dialect=dialect)
    url = "%s/chat/completions" % str(st.get("base_url") or MIMO_BASE_URL).rstrip("/")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": "Bearer %s" % key,
        },
    )
    proxy = str(st.get("proxy") or "")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    ) if proxy else urllib.request.build_opener()

    t0 = time.monotonic()
    samples, chunks, first_ms = 0, 0, None
    try:
        with opener.open(req, timeout=timeout) as resp:
            while True:
                if should_stop is not None and should_stop():
                    break
                raw = resp.readline()
                if not raw:
                    break
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                payload_line = line[5:].strip()
                if payload_line == b"[DONE]":
                    break
                try:
                    obj = json.loads(payload_line.decode("utf-8", "replace") or "{}")
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                audio = (choices[0].get("delta") or {}).get("audio") or {}
                b64 = audio.get("data") if isinstance(audio, dict) else ""
                if not b64:
                    continue
                try:
                    data = base64.b64decode(b64, validate=False)
                except Exception:
                    continue
                pcm = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.int16)
                if pcm.size == 0:
                    continue
                if first_ms is None:
                    first_ms = (time.monotonic() - t0) * 1000.0
                chunks += 1
                samples += int(pcm.size)
                if on_chunk is not None:
                    on_chunk(pcm)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "replace")
        except Exception:
            err_body = ""
        raise MimoTTSError(_friendly_http_error(int(e.code), err_body, st)) from e
    except urllib.error.URLError as e:
        raise MimoTTSError("MiMo 网络不可达：%s（需要能访问 api.xiaomimimo.com，必要时配 online.mimo.proxy）"
                           % getattr(e, "reason", e)) from e
    except TimeoutError as e:
        raise MimoTTSError("MiMo 流式请求超时（%.0fs）" % timeout) from e

    if samples == 0:
        raise MimoTTSError("MiMo 流式返回了 0 个音频样本")
    return {"samples": samples, "chunks": chunks,
            "first_chunk_ms": round(float(first_ms or 0.0), 1),
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "sr": int(sr)}


def _describe(st: Dict[str, object]) -> Dict[str, object]:
    """把一份扁平设置描述成"可对外展示"的在线自述。

    **不报 key 状态**：key 允许按次携带（参数预设每次请求注入），只查配置/环境变量
    得到的"有没有 key"必然是错的（预设注入时它照样是空的），会把智能体带偏。
    key 缺失不再靠状态汇报，而是在合成时直接返回失败原因（见 synth_segment）。
    """
    return {
        "provider": "mimo",
        "model": st.get("model"),
        "models": MIMO_MODELS,
        "base_url": st.get("base_url"),
        "voice": resolve_voice(st.get("voice"), MIMO_DEFAULT_VOICE),
        "api_key_env": st.get("api_key_env"),
        "style": st.get("style") or "",
        "voices": MIMO_VOICES,
        # 唱歌能力：只有预置音色模型支持；标签必须在目标文本最开头
        "singing": {
            "supported": not ("voicedesign" in str(st.get("model") or "")
                              or "voiceclone" in str(st.get("model") or "")),
            "tag": "(唱歌)",
            "aliases": list(_SING_TAGS),
            "note": "标签放在目标文本最开头；唱歌整曲一次合成（每段独立起调会跑调），"
                    "长歌词首声慢，约 0.29 秒/字",
            "split_chars": int(st.get("sing_split_chars") or 0),
        },
        # 说话是否流式（首块即出声）；唱歌永远不走流式
        "streaming": bool(st.get("stream", True)),
        # style 是本次调用可传的自然语言表演指令（写进 role=user）
        "style_param": "speak(style=...)：自然语言指令，控制情绪/语气/角色/导演模式",
    }


def describe(cfg: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """给 tts_status 用：传整份配置（tts_config.json 那个 dict）即可。"""
    return _describe(load_settings(cfg))


# ---------------------------------------------------------------- 命令行自检
def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="MiMo TTS 自检：合成一句并落盘 wav")
    ap.add_argument("--text", default="你好，我是你的桌面伙伴，现在用的是 MiMo 在线配音。")
    ap.add_argument("--voice", default="")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--style", default="", help="自然语言风格指令（覆盖配置）")
    ap.add_argument("--model", default="", help="默认 mimo-v2.5-tts")
    ap.add_argument("--api-key", default="",
                    help="本次请求临时携带的 key（优先于配置/环境变量，不写盘）")
    ap.add_argument("--sing", action="store_true", help="唱歌模式：按 (唱歌) 标签合成，整曲一次请求")
    ap.add_argument("--out", default="mimo_test.wav")
    ap.add_argument("--status", action="store_true", help="只打印当前配置，不发请求")
    args = ap.parse_args()

    st = load_settings()
    if args.style:
        st["style"] = args.style
    if args.model:
        st["model"] = args.model
    if args.status:
        print(json.dumps(_describe(st), ensure_ascii=False, indent=2))
        return 0
    if not find_api_key(st, override=args.api_key):
        print("没有 API Key：用 --api-key sk-xxxx 临时传入，或设置环境变量 %s"
              % (st.get("api_key_env") or "MIMO_API_KEY"))
        return 2

    t0 = time.monotonic()
    pcm, sr, _words, ttfa = synth_segment(args.text, voice=args.voice, speed=args.speed,
                                          sr=48000, settings=st, api_key=args.api_key,
                                          sing=bool(args.sing))
    audio_ms = pcm.size * 1000.0 / float(sr)
    out = os.path.abspath(args.out)
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    print("OK  key 来源=%s model=%s voice=%s sing=%s" % (
        "arg" if str(args.api_key or "").strip() else (key_source(st) or "none"),
        st.get("model"), resolve_voice(args.voice or st.get("voice"), MIMO_DEFAULT_VOICE),
        bool(args.sing)))
    print("首包 %.0f ms · 音频 %.2f s · 总耗时 %.2f s" % (ttfa, audio_ms / 1000.0, time.monotonic() - t0))
    print("已写入 %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
