# coding=utf-8
"""背景音（BGM）：解码、循环、与语音混音。

BGM 与语音在 worker 内实时混音，参数：
  volume  0~1         基础音量（相对语音）
  duck    0~1         说话时的压低比例（1.0 = 不压）
  fade_ms             起播/停止淡入淡出
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

BGM_DIR = Path(__file__).resolve().parent.parent / "assets" / "bgm"

# 内置轻音乐：名字 → 素材文件。素材在 assets/bgm 下，均为真实下载的 CC0/公有领域/CC-BY 曲目，
# 已统一成 48kHz 单声道 16bit、峰值约 -3dBFS、首尾做过等功率淡化，适合循环。
PRESETS = {
    # 温柔钢琴（默认场景，安静不抢人声）
    "piano": "light_piano.wav",
    "light": "light_piano.wav",
    "light_piano": "light_piano.wav",
    "轻音乐": "light_piano.wav",
    "钢琴": "light_piano.wav",
    # 柔和氛围垫 / 轻弦乐，空灵
    "ambient": "soft_ambient.wav",
    "soft": "soft_ambient.wav",
    "soft_ambient": "soft_ambient.wav",
    "氛围": "soft_ambient.wav",
    "空灵": "soft_ambient.wav",
    # lo-fi / 咖啡馆，慵懒日常
    "cafe": "cafe_lofi.wav",
    "lofi": "cafe_lofi.wav",
    "lo-fi": "cafe_lofi.wav",
    "cafe_lofi": "cafe_lofi.wav",
    "咖啡馆": "cafe_lofi.wav",
    "慵懒": "cafe_lofi.wav",
    # 夜晚，低沉略带忧郁
    "night": "night_soft.wav",
    "night_soft": "night_soft.wav",
    "夜晚": "night_soft.wav",
    "忧郁": "night_soft.wav",
    # 自然 / 环境感（风、森林、清晨）
    "nature": "nature_calm.wav",
    "nature_calm": "nature_calm.wav",
    "自然": "nature_calm.wav",
    "环境": "nature_calm.wav",
    "森林": "nature_calm.wav",
    # 轻快明亮，适合开心 / 节日场景
    "bright": "bright_playful.wav",
    "playful": "bright_playful.wav",
    "bright_playful": "bright_playful.wav",
    "活泼": "bright_playful.wav",
    "轻快": "bright_playful.wav",
    "节日": "bright_playful.wav",
}

FALLBACK_PRESET = "light_piano.wav"

# 素材缺失时的兜底顺序：钢琴 → 氛围 → 夜晚 → 自然 → lo-fi → 明快
_FALLBACK_ORDER = (
    "light_piano.wav",
    "soft_ambient.wav",
    "night_soft.wav",
    "nature_calm.wav",
    "cafe_lofi.wav",
    "bright_playful.wav",
)


def _merge_manifest_presets(presets: dict) -> dict:
    """把 manifest.json（{"预设名": "文件名"} 的平铺映射）并入预设表。

    只接受字符串值且素材文件真实存在的条目；同名内置预设保持内置优先，
    避免 manifest 里残留的失效名字把请求解析到不存在的文件。
    """
    try:
        data = json.loads((BGM_DIR / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return presets
    if not isinstance(data, dict):
        return presets
    for key, val in data.items():
        if isinstance(val, str) and val.strip() and (BGM_DIR / val.strip()).is_file():
            presets.setdefault(str(key).strip().lower(), val.strip())
    return presets


PRESETS = _merge_manifest_presets(PRESETS)


def available_presets() -> list:
    """当前实际可用的预设名（素材文件存在），供上层展示或校验。"""
    return sorted(k for k, v in PRESETS.items() if (BGM_DIR / v).is_file())


def first_available_file() -> Optional[Path]:
    """按兜底顺序返回第一个存在的真实素材文件；全都没了才返回 None。"""
    for filename in _FALLBACK_ORDER:
        path = BGM_DIR / filename
        if path.is_file():
            return path
    return None


def resolve_bgm(name: str, sr: int = 48000, fallback_pad_seconds: float = 8.0) -> Optional[np.ndarray]:
    """把 bgm 参数解析成波形：内置轻音乐 → 素材文件；空/off → None；其余当文件路径。

    素材缺失时按 指定预设 → 默认钢琴 → 其它已存在的真实素材 → 合成垫乐 逐级退让，
    绝不因为少一个文件就没声音（也不会退化成静音）。
    """
    key = str(name or "").strip()
    if not key or key.lower() in ("none", "off", "0", "false"):
        return None
    low = key.lower()
    if low in PRESETS:
        path = BGM_DIR / PRESETS[low]
        if path.is_file():
            return load_bgm(str(path), sr=sr)
        default = BGM_DIR / FALLBACK_PRESET
        if default.is_file():
            return load_bgm(str(default), sr=sr)
        alt = first_available_file()
        if alt is not None:
            return load_bgm(str(alt), sr=sr)
        return synth_soft_pad(sr=sr, seconds=fallback_pad_seconds)
    if low in ("pad", "synth", "内置", "default"):
        return synth_soft_pad(sr=sr, seconds=fallback_pad_seconds)
    return load_bgm(key, sr=sr)


# ---------------------------------------------------------------- 读取
def load_bgm(path: str, sr: int = 48000) -> np.ndarray:
    """读取音频文件为 float32 单声道，目标采样率 sr。"""
    p = str(path or "").strip()
    if not p or not os.path.isfile(p):
        raise FileNotFoundError("BGM 文件不存在: %s" % p)

    ext = os.path.splitext(p)[1].lower()
    if ext in (".wav", ".flac", ".ogg", ".aiff", ".aif"):
        import soundfile as sf
        data, src_sr = sf.read(p, dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
    else:
        # mp3 / m4a / aac：走 ffmpeg（本机已安装）
        mono, src_sr = _decode_with_ffmpeg(p)

    if int(src_sr) != int(sr):
        from .resample import resample_float
        mono = resample_float(mono, int(src_sr), int(sr))
    return np.ascontiguousarray(mono.astype(np.float32))


def _decode_with_ffmpeg(path: str) -> Tuple[np.ndarray, int]:
    import subprocess
    import tempfile

    out = os.path.join(tempfile.gettempdir(), "pet_bgm_%d.wav" % os.getpid())
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", path,
        "-ac", "1", "-ar", "48000", "-f", "wav", out,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    import soundfile as sf
    data, sr = sf.read(out, dtype="float32", always_2d=True)
    try:
        os.remove(out)
    except Exception:
        pass
    return data.mean(axis=1), int(sr)


# ---------------------------------------------------------------- 循环
def loop_to_length(bgm: np.ndarray, n_samples: int, crossfade_ms: int = 30, sr: int = 48000) -> np.ndarray:
    """把 BGM 平铺到 n 个样本；接缝处做等功率交叉淡化，避免爆音。"""
    if bgm.size == 0:
        return np.zeros(max(0, n_samples), dtype=np.float32)
    if n_samples <= bgm.size:
        return bgm[:n_samples].copy()

    reps = int(math.ceil(n_samples / bgm.size))
    tiled = np.tile(bgm, reps + 1)[: n_samples + int(sr * crossfade_ms / 1000)]
    fade = int(sr * crossfade_ms / 1000)
    if fade > 0 and bgm.size > fade * 2:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        for k in range(1, reps + 1):
            pos = k * bgm.size
            if pos + fade >= tiled.size:
                break
            # 等功率交叉：新区段淡入，旧区段尾巴淡出（近似，已足够听感）
            tiled[pos:pos + fade] = (
                tiled[pos:pos + fade] * (1.0 - ramp)
                + bgm[:fade] * ramp
            )
    return tiled[:n_samples]


def synth_soft_pad(sr: int = 48000, seconds: float = 8.0) -> np.ndarray:
    """合成一段免版权、可无缝循环的环境垫乐（用于首次试听/无素材时兜底）。"""
    n = int(sr * seconds)
    t = np.arange(n, dtype=np.float64) / sr
    # 三个低频正弦 + 轻微失谐，构成柔和的和声垫
    parts = []
    for f, a, ph in ((110.0, 0.30, 0.0), (146.83, 0.22, 0.8), (174.61, 0.18, 1.7), (220.0, 0.10, 2.6)):
        parts.append(a * np.sin(2 * math.pi * f * t + ph))
    pad = np.sum(parts, axis=0)

    # 缓慢 LFO（周期整除时长，保证首尾相接）
    cycles = 2.0
    lfo = 0.5 + 0.5 * np.sin(2 * math.pi * cycles * t / seconds - math.pi / 2)
    pad = pad * (0.55 + 0.45 * lfo)

    # 一点点空气感噪声（同样整周期包络）
    rng = np.random.default_rng(7)
    noise = rng.normal(0.0, 1.0, n)
    # 简单一阶低通
    alpha = 0.02
    lp = np.empty_like(noise)
    acc = 0.0
    for i in range(0, n, 512):
        blk = noise[i:i + 512]
        # 分块近似滤波，避免纯 Python 逐点循环过慢
        for j in range(blk.size):
            acc = acc + alpha * (blk[j] - acc)
            lp[i + j] = acc
    pad = pad + lp * 0.02 * (0.5 + 0.5 * lfo)

    pad = pad / max(1e-6, np.max(np.abs(pad))) * 0.55
    # 首尾各 20ms 余弦淡入淡出，loop 时听不出接缝
    edge = int(sr * 0.02)
    if edge > 0:
        r = np.linspace(0, math.pi / 2, edge, dtype=np.float32)
        pad[:edge] *= np.sin(r) ** 2
        pad[-edge:] *= (np.cos(r) ** 2)[::-1]
    return pad.astype(np.float32)


# ---------------------------------------------------------------- 混音
def mix_voice_bgm(
    voice: np.ndarray,
    sr: int,
    bgm: Optional[np.ndarray],
    volume: float = 0.35,
    duck: float = 0.25,
    fade_ms: int = 180,
    attack_ms: int = 80,
    release_ms: int = 250,
) -> np.ndarray:
    """离线混音（用于导出试听 wav）：语音 + 循环 BGM + 说话时压低。"""
    voice = np.asarray(voice, dtype=np.float32)
    if bgm is None or volume <= 0 or bgm.size == 0:
        return voice

    n = voice.size
    track = loop_to_length(bgm, n, sr=sr) * float(volume)

    # 语音活跃度包络（逐样本判断，再按 attack/release 平滑）
    active = (np.abs(voice) > 0.02).astype(np.float32)

    # 分块平滑，避免逐点循环太慢
    block = max(1, int(sr * 0.01))
    gain = np.ones(n, dtype=np.float32)
    g = 1.0
    atk = 1.0 - math.exp(-block / (sr * attack_ms / 1000.0 + 1e-9))
    rel = 1.0 - math.exp(-block / (sr * release_ms / 1000.0 + 1e-9))
    duck_val = max(0.0, min(1.0, float(duck)))

    for i in range(0, n, block):
        seg_active = float(active[i:i + block].mean())
        target = duck_val if seg_active > 0.15 else 1.0
        coef = atk if target < g else rel
        g = g + (target - g) * coef
        gain[i:i + block] = g

    fade = int(sr * max(0, fade_ms) / 1000)
    if fade > 2:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        track[:fade] *= ramp
        track[-fade:] *= ramp[::-1]

    return np.clip(voice + track * gain, -1.0, 1.0)
