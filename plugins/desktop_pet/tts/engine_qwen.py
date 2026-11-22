# coding=utf-8
"""本地 Qwen3-TTS 引擎（只在 _tts_env 中运行，依赖 torch / qwen-tts）。

设计要点：
  * 懒加载：第一次合成才载入模型，避免插件启动就吃显存；
  * 可卸载：idle 超时后释放权重 + empty_cache，显存回 0；
  * fp16 + sdpa：RTX 2060（Turing，无原生 bf16，无 flash-attn）；
  * 显存守卫：空闲显存不足时拒绝加载，由上层转在线引擎。
"""

from __future__ import annotations

import gc
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np


@dataclass
class EngineStats:
    loaded: bool = False
    load_ms: float = 0.0
    model_dir: str = ""
    dtype: str = ""
    device: str = ""
    vram_alloc_mb: int = 0
    vram_peak_mb: int = 0
    vram_free_mb: int = 0
    synth_count: int = 0
    last_synth_ms: float = 0.0
    last_audio_ms: float = 0.0
    speakers: List[str] = field(default_factory=list)


class QwenTTSEngine:
    def __init__(
        self,
        model_dir: str,
        dtype: str = "float16",
        attn: str = "sdpa",
        device: str = "cuda:0",
        min_free_vram_mb: int = 1600,
        trim_silence: bool = True,
    ):
        self.model_dir = os.path.abspath(model_dir)
        self.dtype_name = dtype
        self.attn = attn
        self.device = device
        self.min_free_vram_mb = int(min_free_vram_mb)
        self.trim_silence = bool(trim_silence)
        self._model = None
        self._torch = None
        self._sr = 24000
        self.stats = EngineStats(model_dir=self.model_dir, dtype=dtype, device=device)

    # ---------- 生命周期 ----------
    def _vram(self) -> Tuple[int, int]:
        try:
            free, total = self._torch.cuda.mem_get_info(0)
            return int(free // (1024 * 1024)), int(total // (1024 * 1024))
        except Exception:
            return -1, -1

    def load(self) -> Dict[str, object]:
        if self._model is not None:
            return {"load_ms": 0.0, "cached": True}

        import torch  # noqa: 延迟到真正用时
        self._torch = torch

        if self.device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用（torch.cuda.is_available()=False）")

        if self.device.startswith("cuda"):
            free_mb, _ = self._vram()
            if free_mb >= 0 and free_mb < self.min_free_vram_mb:
                raise RuntimeError(
                    "VRAM_LOW: 空闲显存 %dMB < 阈值 %dMB" % (free_mb, self.min_free_vram_mb)
                )

        t0 = time.monotonic()
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True,max_split_size_mb:64")

        # transformers 4.57 用 dtype=，旧版用 torch_dtype=
        dt = getattr(torch, self.dtype_name, torch.float16)
        kwargs = dict(
            device_map=self.device,
            attn_implementation=self.attn,
        )
        from qwen_tts import Qwen3TTSModel  # noqa: 延迟 import

        try:
            model = Qwen3TTSModel.from_pretrained(self.model_dir, dtype=dt, **kwargs)
        except TypeError:
            model = Qwen3TTSModel.from_pretrained(self.model_dir, torch_dtype=dt, **kwargs)

        try:
            model.model.eval()
        except Exception:
            pass

        self._model = model
        load_ms = (time.monotonic() - t0) * 1000.0

        try:
            speakers = model.get_supported_speakers() or []
        except Exception:
            speakers = []

        self.stats.loaded = True
        self.stats.load_ms = load_ms
        self.stats.speakers = [str(s) for s in speakers]
        self.refresh_vram()
        return {"load_ms": round(load_ms, 1), "cached": False, "speakers": self.stats.speakers}

    def unload(self) -> int:
        """释放模型，返回释放后的空闲显存（MB，-1 表示不可测）。"""
        if self._model is not None:
            try:
                del self._model
            except Exception:
                pass
            self._model = None
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_available():
            try:
                self._torch.cuda.empty_cache()
                self._torch.cuda.ipc_collect()
            except Exception:
                pass
        self.stats.loaded = False
        free_mb, _ = self._vram()
        self.stats.vram_alloc_mb = 0
        self.stats.vram_free_mb = free_mb
        return free_mb

    def refresh_vram(self) -> None:
        if self._torch is None or not self._torch.cuda.is_available():
            return
        free_mb, _ = self._vram()
        self.stats.vram_free_mb = free_mb
        try:
            self.stats.vram_alloc_mb = int(self._torch.cuda.memory_allocated() // (1024 * 1024))
            self.stats.vram_peak_mb = int(self._torch.cuda.max_memory_allocated() // (1024 * 1024))
        except Exception:
            pass

    # ---------- 合成 ----------
    def synthesize(
        self,
        text: str,
        speaker: str = "Serena",
        language: str = "Chinese",
        max_new_tokens: int = 1024,
        non_streaming_mode: bool = False,
        do_sample: bool = True,
        subtalker_dosample: bool = True,
        speed: float = 1.0,
    ) -> Tuple[np.ndarray, int, float]:
        """返回 (int16 PCM, sample_rate, synth_ms)。

        speed：Qwen 的预设音色接口不暴露语速控制，这里只接收不生效，
        由调用方保证"用户能听到的语速"这件事由 Piper / Edge 负责。
        """
        self.load()
        torch = self._torch
        assert self._model is not None

        t0 = time.monotonic()
        with torch.inference_mode():
            wavs, sr = self._model.generate_custom_voice(
                text=text,
                speaker=speaker,
                language=language,
                non_streaming_mode=non_streaming_mode,
                max_new_tokens=max_new_tokens,
                do_sample=do_sample,
                subtalker_dosample=subtalker_dosample,
            )
        synth_ms = (time.monotonic() - t0) * 1000.0

        if not wavs:
            raise RuntimeError("TTS_EMPTY: 模型没有返回音频")
        wav = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        if wav.size == 0:
            raise RuntimeError("TTS_EMPTY: 返回音频长度为 0")

        if self.trim_silence:
            wav = _trim_silence(wav, threshold=0.006, pad_ms=60, sr=int(sr))

        pcm = np.clip(wav, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype(np.int16)

        self._sr = int(sr)
        audio_ms = len(pcm16) * 1000.0 / float(sr)
        self.stats.synth_count += 1
        self.stats.last_synth_ms = synth_ms
        self.stats.last_audio_ms = audio_ms
        self.refresh_vram()
        return pcm16, int(sr), synth_ms

    @property
    def sample_rate(self) -> int:
        return self._sr

    def status(self) -> Dict[str, object]:
        self.refresh_vram()
        return {
            "loaded": bool(self.stats.loaded),
            "model_dir": self.model_dir,
            "dtype": self.dtype_name,
            "attn": self.attn,
            "device": self.device,
            "load_ms": round(self.stats.load_ms, 1),
            "vram_alloc_mb": self.stats.vram_alloc_mb,
            "vram_peak_mb": self.stats.vram_peak_mb,
            "vram_free_mb": self.stats.vram_free_mb,
            "synth_count": self.stats.synth_count,
            "last_synth_ms": round(self.stats.last_synth_ms, 1),
            "last_audio_ms": round(self.stats.last_audio_ms, 1),
            "speakers": self.stats.speakers,
        }


def _trim_silence(wav: np.ndarray, threshold: float = 0.006, pad_ms: int = 60, sr: int = 24000) -> np.ndarray:
    """裁掉首尾静音（保留 pad_ms 垫音），直接降低首声延迟。"""
    if wav.size == 0:
        return wav
    idx = np.where(np.abs(wav) > threshold)[0]
    if idx.size == 0:
        return wav
    pad = int(sr * pad_ms / 1000.0)
    start = max(0, int(idx[0]) - pad)
    end = min(wav.size, int(idx[-1]) + pad)
    if end - start < int(sr * 0.05):
        return wav
    return wav[start:end]
