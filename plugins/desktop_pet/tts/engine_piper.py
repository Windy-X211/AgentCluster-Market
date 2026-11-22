# coding=utf-8
"""Piper 轻量流式 TTS 引擎（ONNX，CPU 推理，零显存占用）。

为什么换它：Qwen3-TTS-0.6B 在 RTX 2060（sm75）上实测 RTF 6~15，
一条 8 字短句要 24s 才出声；Piper zh_CN-huayan-medium 只有 63MB，
实测 load 5.5s、首块 639ms、RTF 0.17，CPU 就能实时，且完全不抢桌宠渲染的显存。

对外接口与 QwenTTSEngine 保持一致（load / unload / status / synthesize / stats / sample_rate），
daemon 只需要换一个类，协议不用动。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

# 噪声强度：默认 0.667/0.8 时同一句连跑三次时长差 0.33s、内部停顿位置还会
# 整段多出来或消失；实测 0.10/0.10 时主停顿抖动只有 ±4ms，仍保留非零随机
# （不是"完全一致"的机械音）。用户 2026-11-14 选定这一档。
DEFAULT_NOISE_SCALE = 0.10
DEFAULT_NOISE_W = 0.10


@dataclass
class PiperStats:
    loaded: bool = False
    load_ms: float = 0.0
    model_path: str = ""
    sample_rate: int = 22050
    speakers: List[str] = field(default_factory=list)
    synth_count: int = 0
    last_synth_ms: float = 0.0
    last_audio_ms: float = 0.0
    first_chunk_ms: float = 0.0


class PiperTTSEngine:
    """单进程单模型：PiperVoice 内部持有 onnxruntime session，线程安全靠锁串行化。"""

    def __init__(self, model_path: str, speaker: str = "huayan", trim_silence: bool = True,
                 noise_scale: float = DEFAULT_NOISE_SCALE,
                 noise_w: float = DEFAULT_NOISE_W):
        self.model_path = os.path.abspath(model_path)
        self.speaker_name = speaker or "huayan"
        self.trim_silence = bool(trim_silence)
        try:
            self.noise_scale = max(0.0, min(1.0, float(noise_scale)))
            self.noise_w = max(0.0, min(1.0, float(noise_w)))
        except Exception:
            self.noise_scale = DEFAULT_NOISE_SCALE
            self.noise_w = DEFAULT_NOISE_W
        self._voice = None
        self._lock = threading.Lock()
        self._sr = 22050
        self.stats = PiperStats(model_path=self.model_path, speakers=[self.speaker_name])

    # ---------- 生命周期 ----------
    def load(self) -> Dict[str, object]:
        if self._voice is not None:
            return {"load_ms": 0.0, "cached": True}
        from piper import PiperVoice

        t0 = time.monotonic()
        with self._lock:
            self._voice = PiperVoice.load(self.model_path)
        self._sr = int(getattr(self._voice, "config", None).sample_rate) if getattr(
            self._voice, "config", None) is not None else 22050
        self.stats.loaded = True
        self.stats.load_ms = (time.monotonic() - t0) * 1000.0
        self.stats.sample_rate = self._sr
        return {"load_ms": self.stats.load_ms, "cached": False}

    def unload(self) -> int:
        with self._lock:
            self._voice = None
        self.stats.loaded = False
        return 0        # 纯 CPU，没有显存可还

    @property
    def sample_rate(self) -> int:
        return int(self._sr)

    def status(self) -> Dict[str, object]:
        return {
            "loaded": self.stats.loaded,
            "engine": "piper",
            "model": os.path.basename(self.model_path),
            "dtype": "int8/onnx-cpu",
            "device": "cpu",
            "sample_rate": self.sample_rate,
            "speakers": list(self.stats.speakers),
            "vram_alloc_mb": 0,
            "vram_peak_mb": 0,
            "vram_free_mb": 0,
            "load_ms": round(self.stats.load_ms, 1),
            "synth_count": self.stats.synth_count,
            "last_synth_ms": round(self.stats.last_synth_ms, 1),
            "first_chunk_ms": round(self.stats.first_chunk_ms, 1),
        }

    # ---------- 合成 ----------
    def synthesize(
        self,
        text: str,
        speaker: str = "",
        language: str = "Chinese",
        max_new_tokens: int = 0,
        non_streaming_mode: bool = False,
        speed: float = 1.0,
    ) -> Tuple[np.ndarray, int, float]:
        """返回 (int16 PCM, 采样率, 合成耗时 ms)。Piper 单音色，speaker 参数只做兼容。

        speed：语速倍数（1.0 = 原速，>1 更快）。

        为什么不用 Piper 自带的 length_scale：实测它对时长只有不到线性一半的影响，
        而且抖动很大（同一句 ls=1.3 比 ls=1.6 还长），用户听到的语速根本不可控。
        所以这里按原速合成，再用 WSOLA 做保持音高的时间伸缩，
        输出时长严格等于「原时长 / speed」，语速是真可控的。
        """
        if self._voice is None:
            self.load()
        t0 = time.monotonic()
        blob = bytearray()
        first_ms = -1.0
        syn = None
        try:
            from piper import SynthesisConfig
            # noise_w 决定每个字的时长抖动（停顿不稳的元凶），noise_scale 决定音色/抑扬。
            # 两者都压到 0.1 档：节奏稳定，但仍有轻微变化，不会变成"一遍一遍一模一样"。
            syn = SynthesisConfig(noise_scale=self.noise_scale,
                                  noise_w_scale=self.noise_w,
                                  length_scale=1.0)
        except Exception:
            syn = None
        with self._lock:
            for ch in (self._voice.synthesize(text, syn_config=syn) if syn is not None
                       else self._voice.synthesize(text)):
                b = getattr(ch, "audio_int16_bytes", None)
                if not b:
                    continue
                if first_ms < 0:
                    first_ms = (time.monotonic() - t0) * 1000.0
                blob.extend(b)
        if first_ms < 0:
            first_ms = (time.monotonic() - t0) * 1000.0
        pcm = np.frombuffer(bytes(blob), dtype=np.int16).copy()
        spd = 1.0
        try:
            spd = max(0.6, min(1.6, float(speed) if speed else 1.0))
        except Exception:
            spd = 1.0
        if abs(spd - 1.0) > 0.02 and pcm.size:
            pcm = _stretch(pcm, self._sr, spd)
        if self.trim_silence and pcm.size:
            pcm = _trim(pcm, self._sr)
        synth_ms = (time.monotonic() - t0) * 1000.0
        self.stats.synth_count += 1
        self.stats.last_synth_ms = synth_ms
        self.stats.last_audio_ms = pcm.size * 1000.0 / max(1, self._sr)
        self.stats.first_chunk_ms = first_ms
        return pcm, self._sr, synth_ms


def _stretch(pcm: np.ndarray, sr: int, speed: float,
             frame_ms: float = 30.0, hop_ms: float = 10.0) -> np.ndarray:
    """WSOLA 时间伸缩：改变时长但保持音高（语速用）。

    speed>1 变快（跳着取分析帧），speed<1 变慢（多取重复帧）。
    每帧在 ±6ms 内找与「上一帧自然延续」最相似的位置再叠加，
    避免直接丢样本造成的咔哒声。时长误差 <1 帧（约 30ms）。
    """
    x = np.asarray(pcm, dtype=np.float32) / 32768.0
    if x.size < int(sr * 0.08) or abs(speed - 1.0) < 0.02:
        return np.asarray(pcm)

    N = max(64, int(sr * frame_ms / 1000.0))       # 帧长
    Hs = max(16, int(sr * hop_ms / 1000.0))        # 合成步长
    Ha = int(round(Hs * speed))                    # 分析步长
    Ha = max(1, min(Ha, N - Hs))                   # 分析步长不能超过帧长
    search = max(8, int(sr * 0.006))               # ±6ms 相似搜索
    win = np.hanning(N + 2)[1:-1].astype(np.float32)

    est = int(round(x.size / speed)) + N
    out = np.zeros(est + N, dtype=np.float32)
    wsum = np.zeros_like(out)

    src = 0
    dst = 0
    tmpl = None
    seg_lo = None
    while dst < est and src < x.size:
        ipos = src
        if tmpl is not None and tmpl.size == Hs:
            lo = max(0, ipos - search)
            hi = min(x.size - Hs, ipos + search)
            if hi > lo:
                # 用滑动窗口视图一次性算完所有候选的相关，取最像的那个
                cand = np.lib.stride_tricks.sliding_window_view(x[lo:hi + Hs], Hs)
                scores = cand @ tmpl
                ipos = lo + int(np.argmax(scores))
        ipos = max(0, min(ipos, max(0, x.size - N)))
        seg = x[ipos:ipos + N]
        if seg.size < N:
            break
        out[dst:dst + N] += seg * win
        wsum[dst:dst + N] += win
        nxt = ipos + Hs
        tmpl = x[nxt:nxt + Hs] if nxt + Hs <= x.size else None
        src += Ha
        dst += Hs
        seg_lo = nxt

    body = out[:dst]
    w = wsum[:dst]
    np.maximum(w, 1e-4, out=w)
    body = body / w
    np.clip(body, -1.0, 1.0, out=body)
    return (body * 32767.0).astype(np.int16)


def _trim(pcm: np.ndarray, sr: int) -> np.ndarray:
    """剪掉首尾静音（Piper 会给句子加一点空白），省掉"说完还空 0.3 秒"的观感。"""
    if pcm.size < sr // 10:
        return pcm
    thr = max(200, int(abs(pcm).max() * 0.04))
    idx = np.nonzero(np.abs(pcm) > thr)[0]
    if idx.size == 0:
        return pcm
    pad = int(sr * 0.04)
    a = max(0, int(idx[0]) - pad)
    b = min(pcm.size, int(idx[-1]) + pad)
    return pcm[a:b]
