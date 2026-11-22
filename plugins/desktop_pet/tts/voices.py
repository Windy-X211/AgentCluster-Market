# coding=utf-8
"""多角色音色注册表（sherpa-onnx 本地引擎用）。

设计要点：
  * **一个模型 + 多个 sid = 多个音色**。sid 只是 `generate(..., sid=N)` 的一个整数，
    换 sid、换音色 **零开销**，也不用第二份权重 —— 这是最省内存的多音色方案。
  * **只常驻一个模型**：单模型实测 RSS 240MB（含进程基线 38MB，即权重约 200MB），
    三个模型同驻要 545MB。本期只热 eula，需要再加模型时改 WARM_MODELS 即可。
  * eula 的 804 个 sid **全部是女声**（实测 28 个采样点 F0 207~355Hz，无男声），
    所以这里只提供女声；要男声得换模型（zh-ll 的 sid4、fanchen 的一部分），
    那是"加一份 130MB 权重"的代价，不属于单模型方案。

筛选记录（_workers/audition/sids/sid_screen.json）：
  28 个 sid 抽样全部可用（无空音频、无削顶、语速 5.6~7.0 字/秒），F0 覆盖 207~355Hz。
"""

from __future__ import annotations

import os

MODEL_ROOT = os.environ.get("DESKTOP_PET_MODELS_ROOT", r"E:\AgentClusterModels")

# ---------------------------------------------------------------------------
# 模型（每个模型 = 一份权重；单模型常驻约 200MB，纯 CPU，不占显存）
# ---------------------------------------------------------------------------
MODELS = {
    "eula": {
        "kind": "vits",
        "dir": os.path.join(MODEL_ROOT, "sherpa-onnx", "vits-zh-hf-eula"),
        "onnx": "eula.onnx",
        "sr": 22050,
        "speakers": 804,
        "license": "Unknown（原神角色同人微调，仅自用）",
    },
    # 备用（不常驻）：需要"标准播音普通话"或更低延迟时再把它加进 WARM_MODELS。
    # 单说话人、22.05kHz、RTF 约 0.09、首句约 114ms，但热加载需 8.7s。
    "matcha": {
        "kind": "matcha",
        "dir": os.path.join(MODEL_ROOT, "matcha", "matcha-icefall-zh-baker"),
        "acoustic": os.path.join(MODEL_ROOT, "matcha", "matcha-icefall-zh-baker", "model-steps-3.onnx"),
        "vocoder": os.path.join(MODEL_ROOT, "matcha", "vocos-22khz-univ.onnx"),
        "sr": 22050,
        "speakers": 1,
        "license": "非商用（AISHELL3 baker 微调）",
    },
}

# 只热一个模型：换音色靠 sid，内存占用 ≈ 200MB，切换零等待。
WARM_MODELS = ["eula"]

# ---------------------------------------------------------------------------
# 音色（全部来自 eula 一个模型，智能体按 trait 选）
# ---------------------------------------------------------------------------
VOICES = [
    {
        "id": "eula_girl",
        "name": "艾拉·清亮",
        "model": "eula",
        "sid": 0,
        "gender": "女",
        "trait": "动漫少女，清亮偏冷，语气自然，适合符玄这种角色日常对话（默认）",
        "f0": 341,
    },
    {
        "id": "eula_bright",
        "name": "艾拉·明快",
        "model": "eula",
        "sid": 40,
        "gender": "女",
        "trait": "音高最高、明亮有元气，适合兴奋、撒娇、惊喜这类情绪",
        "f0": 355,
    },
    {
        "id": "eula_warm",
        "name": "艾拉·柔缓",
        "model": "eula",
        "sid": 401,
        "gender": "女",
        "trait": "中音区、语速稍快，温柔但不甜，适合讲解、安抚、长句叙述",
        "f0": 297,
    },
    {
        "id": "eula_low",
        "name": "艾拉·低沉",
        "model": "eula",
        "sid": 720,
        "gender": "女",
        "trait": "低音区、偏成熟冷淡，适合毒舌、严肃、低声吐槽",
        "f0": 207,
    },
]

SHERPA_VOICES = {v["id"]: v for v in VOICES}
DEFAULT_VOICE = "eula_girl"

# 仍然是 Piper（单音色）与 Qwen（预设音色）的老名字，保持向后兼容
PIPER_VOICE = "huayan"


def voice_list() -> list:
    """给 tts_status / 工具描述用的本地音色清单。"""
    out = [{"id": PIPER_VOICE, "name": "华颜", "gender": "女", "model": "piper",
            "trait": "Piper 轻量基线：延迟最低（首声 ~0.1s）但音色平、偏播音；作为兜底保留"}]
    for v in VOICES:
        out.append({"id": v["id"], "name": v["name"], "gender": v["gender"],
                    "model": v["model"], "trait": v["trait"],
                    "license": MODELS[v["model"]]["license"]})
    return out


def resolve(voice_id, engine: str = "sherpa") -> dict:
    """voice id -> {model, sid, ...}；不认识的名字回落到默认音色（不报错、不出哑音）。"""
    vid = str(voice_id or "").strip()
    if engine == "piper":
        return {"id": PIPER_VOICE, "model": "piper", "sid": 0, "name": "华颜"}
    if vid in SHERPA_VOICES:
        return dict(SHERPA_VOICES[vid])
    for v in VOICES:
        if vid and (vid == v["name"] or vid == v["model"]):
            return dict(v)
    return dict(SHERPA_VOICES[DEFAULT_VOICE])
