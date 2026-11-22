# coding=utf-8
"""采样率转换（24k TTS 输出 → 设备采样率）。"""

from __future__ import annotations

from math import gcd

import numpy as np


def resample_int16(pcm: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """int16 单声道 PCM 重采样，零相位（scipy polyphase）。"""
    x = np.asarray(pcm)
    if src_sr == dst_sr or x.size == 0:
        return x.astype(np.int16, copy=False)
    try:
        from scipy.signal import resample_poly
    except Exception:  # scipy 缺失时退化为线性插值（保底，不常用）
        n = int(round(x.size * dst_sr / src_sr))
        xi = np.linspace(0, x.size - 1, n, dtype=np.float64)
        y = np.interp(xi, np.arange(x.size), x.astype(np.float32))
        return np.clip(y, -32768, 32767).astype(np.int16)

    g = gcd(int(src_sr), int(dst_sr))
    up = int(dst_sr) // g
    down = int(src_sr) // g
    y = resample_poly(x.astype(np.float32) / 32768.0, up, down)
    y = np.clip(y, -1.0, 1.0)
    return (y * 32767.0).astype(np.int16)


def resample_float(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """float32 [-1,1] 单声道重采样。"""
    x = np.asarray(x, dtype=np.float32)
    if src_sr == dst_sr or x.size == 0:
        return x
    try:
        from scipy.signal import resample_poly
    except Exception:
        n = int(round(x.size * dst_sr / src_sr))
        xi = np.linspace(0, x.size - 1, n)
        return np.interp(xi, np.arange(x.size), x).astype(np.float32)
    g = gcd(int(src_sr), int(dst_sr))
    return resample_poly(x, int(dst_sr) // g, int(src_sr) // g).astype(np.float32)
