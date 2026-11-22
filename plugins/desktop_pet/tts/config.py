# coding=utf-8
"""配音配置与路径约定。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

PLUGIN_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PLUGIN_DIR / "tts_config.json"
VENV_PY = PLUGIN_DIR / "_tts_env" / "Scripts" / "python.exe"
MODELS_DIR = PLUGIN_DIR / "models"

DEFAULT_MODEL_DIR = os.environ.get(
    "DESKTOP_PET_TTS_MODEL", r"E:\AgentClusterModels\Qwen3-TTS-12Hz-0.6B-CustomVoice"
)

DEFAULTS: Dict[str, Any] = {
    "backend": "auto",
    # 本地默认音色：sherpa 引擎的 voice id（见 tts/voices.py）。
    # 换音色只换这个 id / speak 的 voice 参数，不换模型、不重新加载。
    "voice": {"local": "eula_girl", "online": "zh-CN-XiaoxiaoNeural", "mimo": "冰糖"},
    "language": "Chinese",
    "subtitle": {"style": "karaoke", "max_chars": 42, "mode": "line", "hold_ms": 3000},
    "bgm": {
        "default": "",
        "volume": 0.85,
        "duck": 0.60,
        "fade_ms": 500,
        "preroll_ms": 1200,
        "tail_ms": 1600,
        "loop": True,
        "mode": "speech",
    },
    "lip_sync": True,
    # 播放侧：设备缓冲长度。Qt 默认 60ms，Windows 上 10ms tick 稍有抖动就会欠载
    # （表现就是唱歌这种长音频"一顿一顿"）。100ms 换来 40ms 余量，延迟上听不出来。
    "audio": {"buffer_ms": 100},
    # 说话时的程序化微动作：gain 是幅度总开关（1.0 = 默认档，明显可见但不抽搐），
    # nod_every_ms 是句内自动点头的间隔（带随机抖动）
    "motion_gain": 1.0,
    "motion_nod_every_ms": 2200,
    "local": {
        # piper ：63MB 单音色，RTF 0.076、首句 96ms，兜底用
        # sherpa：一个模型 + 多个 sid 音色，22.05kHz，RTF 约 0.4、首句 0.37~0.42s，
        #         单模型常驻约 200MB 内存、零显存；换音色零开销（默认）
        # qwen  ：Qwen3-TTS-0.6B，音质更好但 RTF 6~15，这台机器只能短句兜底
        "engine": "sherpa",
        "piper_model": os.environ.get(
            "DESKTOP_PET_PIPER_MODEL",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "assets", "piper", "zh_CN-huayan-medium.onnx")),
        "model_dir": DEFAULT_MODEL_DIR,
        "variant": "custom_voice",
        "dtype": "bfloat16",
        "attn": "sdpa",
        "idle_unload_s": 900,
        "daemon_exit_s": 1800,
        "min_free_vram_mb": 2400,
        "first_packet_timeout_ms": 1200,
        "max_new_tokens": 512,
        "local_stall_s": 45,
        # 首段播放前先攒多少毫秒音频。sherpa 每段合成本身要 0.4~0.5s（比 Piper 慢），
        # 攒得少一点能直接把首声提前约 0.1s，而合成耗时已经足够填满解码缓冲，不会断音。
        "prebuffer_ms": 220,
        # 语速基准：对外 speed=1.0 就是"当前这套引擎的正常语速"，
        # 内部换算成 Piper 原速的倍数用这个基准（1.0 * 0.80 = 原速 0.80）。
        # 想让"正常"更快/更慢，只调这个基准，智能体侧的 speed 语义不变。
        "base_speed": 0.80,
        # sherpa 的天然语速比 Piper 快约 39%（同一句 25 字：sherpa 4.31s vs Piper@0.80 6.00s，
        # 5.8 字/秒 vs 4.2 字/秒）。0.72 = 4.31/6.00，把"正常"对齐到用户认可的 Piper@0.80 语速，
        # 换引擎不会突然变快；以后要调"正常"的快慢，只改这个数。
        "sherpa_speed_scale": 0.72,
        "min_chars": 6,
        "max_chars": 24,
        # 文本停顿/连读符号：~ 连读、| 短停、|| 中停、||| 长停
        "pause_marks": True,
        "pause_short_ms": 150,
        "pause_mid_ms": 300,
        "pause_long_ms": 500,
        # Piper 随机性：noise_w 决定字长抖动（停顿是否稳），noise_scale 决定音色。
        # 默认 0.667/0.8 时同一句时长差 0.33s、停顿位置会整段消失；0.10/0.10 抖动 ±4ms。
        "noise_scale": 0.10,
        "noise_w": 0.10,
        "trim_silence": True,
        "do_sample": True,
        "subtalker_dosample": True,
    },
    "online": {
        # provider 决定 backend=auto（以及不指定 backend 的在线调用）走哪家：
        #   edge = 微软 Edge-TTS（免 key，原生 WordBoundary 逐字时间戳）
        #   mimo = 小米 MiMo-V2.5-TTS（需要 API Key，音质/风格控制更强）
        # 想强制用某一家也可以直接 speak(backend="mimo") / speak(backend="edge")。
        "provider": "edge",
        "api_key_env": "",
        "proxy": "",
        # ---- 小米 MiMo-V2.5-TTS（OpenAI 兼容 /chat/completions）----
        # key 查找顺序：api_key → api_key_file → 环境变量 api_key_env（默认 MIMO_API_KEY）
        "mimo": {
            "base_url": "https://api.xiaomimimo.com/v1",
            # mimo-v2.5-tts（预置音色，默认） / mimo-v2.5-tts-voicedesign（文本设计音色）
            # / mimo-v2.5-tts-voiceclone（音频复刻音色）
            "model": "mimo-v2.5-tts",
            "voice": "冰糖",
            "api_key": "",
            "api_key_env": "MIMO_API_KEY",
            "api_key_file": "",
            # voicedesign 模型的音色描述（model=voicedesign 且 style 为空时用它兜底）
            "voice_design": "",
            # 默认风格指令，写进 role=user（语气/情绪/角色扮演/导演模式都能写）
            "style": "",
            # 语速：MiMo 没有官方的 rate 参数，只能折成自然语言指令近似控制；
            # 想要"精确倍数"就别开它（默认开，够用且不影响音色）。
            "speed_hint": True,
            # 首尾静音裁剪：MiMo 回包常带 0.1~0.3s 静音，裁掉能提前首声、句间不多一拍
            "trim_silence": True,
            # 并发预取的句子数（每句一次请求，2~3 足够把播放缓冲喂满又不触发限流）
            "parallel": 2,
            # 说话走流式（stream=True + pcm16）：实测 40 字首块 1.32s 就能出声，
            # 整包非流式要等全部算完。唱歌**不要**开流式：实测只回 1 块、首块时间=总耗时。
            "stream": True,
            # 流式首块到货时字幕还拿不到真实时长，先按"字数 × 这个毫秒数"估一个，
            # 收完再修正。260ms/字是 MiMo 说话实测值（40 字 ≈ 10.72s）。
            "stream_est_ms_per_char": 260,
            # 唱歌：MiMo 是整段推理，实测 0.29 秒/字（100 字 = 29 秒才出第一个音）。
            # 默认 0 = 整曲一次：旋律连贯、不跑调，代价是首声慢。
            # 想要首声快可以设成 28 之类按句拆（首句压到 sing_first_chars 字），
            # 但实测每段都会自己重新起调：49 字拆 3 段时第 3 段整段低了 5.5 半音、
            # 接缝跳变 -16.3 半音 —— 拆得越碎，跑调越明显。
            "sing_split_chars": 0,
            "sing_first_chars": 20,
            # 唱歌进度气泡的估算速率（毫秒/字）：MiMo 唱歌实测 0.29~0.37 秒/字，
            # 桌宠头顶按"已过时间 ÷ 这个值"折算百分比，只用于显示，不影响音频。
            "sing_ms_per_char": 300,
            # 唱歌等待期垫场：整曲一次要等 15~30 秒才出第一声，这段时间先让背景音
            # 和一句"清嗓子"配音顶着，避免用户以为卡死。
            "sing_wait_bgm": True,          # 等待期播背景音（唱歌一到就淡出，不在歌里搅和）
            "sing_wait_scene": "ambient",   # 用哪个内置场景；piano/ambient/cafe/night/nature/bright
            "sing_wait_volume": 0.55,       # 等待期 BGM 音量（低于正常配音的 0.85，别盖住人声）
            "sing_wait_voice": True,        # 播"清嗓子"配音（读 _tts/sing_wait/<音色>.wav 缓存）
            # 等待配音的台词，只用于烘焙缓存（tools/bake_sing_wait.py），不参与请求
            "sing_wait_line": "嗯……我先清一下嗓子，稍等一会儿，马上就唱给你听。",
            # 跨 speak 条目预取（depth=1，仅 mode=queue 说话，sing 不参与）：
            # 当前句在播时，后台把队列里"下一条"完整合成好（按段切分 + 流式 chunk 原样收集），
            # 下一条开口时不再等网络首块（实测 1.3s+），段间 gap 显著下降。
            # 预取结果只存在 service 层的 _speech_cache，绝不提前 enqueue，
            # 命中后仍走 sc/sed/end 正常投递链路（字幕/口型/BGM 时间轴不变）。
            # 关掉设 false 即回退到逐条实时合成。
            "speech_prefetch_enabled": True,
            # 缓存 TTL（秒）与条目数上限：防内存膨胀与陈旧内容
            "speech_prefetch_ttl_s": 300,
            "speech_prefetch_max_entries": 4,
            # 输出峰值上限：MiMo 唱歌峰值常常打满 32767，与 BGM 相加会削波失真，
            # 这里先衰减到 88% 满量程留余量（0 = 不处理）
            "headroom": 0.88,
            "timeout_s": 30.0,
            "sample_rate": 48000,
            "format": "wav",
            "optimize_text_preview": False,
            "proxy": "",
        },
    },
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        if CONFIG_PATH.is_file():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg = deep_merge(cfg, data)
    except Exception:
        pass
    return cfg


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cfg = deep_merge(load_config(), patch or {})
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cfg


def model_dir_from_config(cfg: Dict[str, Any] = None) -> str:
    cfg = cfg or load_config()
    return str((cfg.get("local") or {}).get("model_dir") or DEFAULT_MODEL_DIR)


def venv_python() -> str:
    return str(VENV_PY)
