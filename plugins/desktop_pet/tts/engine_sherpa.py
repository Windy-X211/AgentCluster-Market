# coding=utf-8
"""sherpa-onnx 多角色本地引擎（CPU / ONNX / 零显存）。

为什么要单独一个引擎文件：
  * Piper 是单音色，撑不起"多角色立刻切换"；Qwen 在这台机器上 RTF 6~15 不可用。
  * sherpa-onnx 的 VITS / Matcha 模型都是 22.05kHz、CPU 实时，而且**一个模型可以挂
    几百个 speaker（sid）**；把 3 个模型全部常驻后，换音色只是换一个整数参数。

实测（本机，串行）：
  * 加载：eula 4.9s / theresa 5.0s / matcha 8.7s，三模型同驻 RSS 约 545MB
  * 之后换 sid / 换模型：0 额外开销（只是 in-process 选参数）
  * 首段（首句）延迟：matcha 114ms、eula 371~419ms、theresa 462~510ms
  * 30 字整句 RTF：eula 0.36~0.41、theresa 0.40~0.47、matcha 0.09

对外接口与 PiperTTSEngine 完全一致（load / unload / status / synthesize / sample_rate），
daemon 不需要改协议。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .voices import MODELS, WARM_MODELS, resolve

SPEED_MIN = 0.6
SPEED_MAX = 1.6


def _rss_mb() -> float:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),
                                                     ctypes.byref(c), c.cb)
            return c.WorkingSetSize / (1024 * 1024)
        except Exception:
            return 0.0


@dataclass
class SherpaStats:
    loaded: bool = False
    load_ms: float = 0.0
    sample_rate: int = 22050
    synth_count: int = 0
    last_synth_ms: float = 0.0
    last_audio_ms: float = 0.0
    models: Dict[str, Dict[str, object]] = field(default_factory=dict)
    voices: List[str] = field(default_factory=list)


class SherpaEnginePool:
    """多模型常驻池：voice -> (model, sid)，模型只加载一次，切音色零等待。"""

    def __init__(self, trim_silence: bool = True, num_threads: int = 4,
                 warm: Optional[List[str]] = None, model_override: Optional[Dict[str, dict]] = None):
        self.trim_silence = bool(trim_silence)
        self.num_threads = max(1, int(num_threads))
        self.warm = list(warm or WARM_MODELS)
        self.models_override = dict(model_override or {})
        self._pool: Dict[str, object] = {}
        self._lock = threading.Lock()          # 合成串行化
        self._load_lock = threading.RLock()    # 加载串行化（预热线程与首个 speak 可能同时进来）
        self._sr = 22050
        self.keep_warm = True                  # daemon 空闲时不要卸载：重新加载要 18 秒
        self.stats = SherpaStats(voices=[v for v in MODELS])
        self._base_rss = _rss_mb()

    # ---------- 构造配置 ----------
    def _cfg_for(self, key: str):
        import sherpa_onnx as so

        spec = dict(MODELS[key])
        spec.update(self.models_override.get(key) or {})
        d = spec["dir"]
        tokens = os.path.join(d, "tokens.txt") if os.path.exists(os.path.join(d, "tokens.txt")) else ""
        lexicon = os.path.join(d, "lexicon.txt") if os.path.exists(os.path.join(d, "lexicon.txt")) else ""
        dict_dir = os.path.join(d, "dict") if os.path.isdir(os.path.join(d, "dict")) else ""
        data_dir = os.path.join(d, "espeak-ng-data") if os.path.isdir(os.path.join(d, "espeak-ng-data")) else ""
        fsts = ",".join(os.path.join(d, x) for x in
                        ("phone.fst", "date.fst", "number.fst", "new_heteronym.fst")
                        if os.path.exists(os.path.join(d, x)))
        mc_kw = dict(num_threads=self.num_threads, provider="cpu", debug=False)

        if spec.get("kind") == "matcha":
            m = so.OfflineTtsMatchaModelConfig(
                acoustic_model=spec.get("acoustic") or os.path.join(d, "acoustic_model.onnx"),
                vocoder=spec.get("vocoder") or os.path.join(d, "vocoder.onnx"),
                tokens=tokens, data_dir=data_dir, dict_dir=dict_dir, lexicon=lexicon)
            return so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(matcha=m, **mc_kw),
                                       rule_fsts=fsts, rule_fars="")

        onnx = spec.get("onnx") or ""
        path = os.path.join(d, onnx) if onnx else ""
        if not path or not os.path.isfile(path):
            cands = sorted([p for p in os.listdir(d) if p.endswith(".onnx")])
            if not cands:
                raise FileNotFoundError("模型目录里没有 onnx：%s" % d)
            path = os.path.join(d, cands[0])
        m = so.OfflineTtsVitsModelConfig(model=path, tokens=tokens, lexicon=lexicon, dict_dir=dict_dir)
        return so.OfflineTtsConfig(model=so.OfflineTtsModelConfig(vits=m, **mc_kw), rule_fsts=fsts)

    # ---------- 生命周期 ----------
    def load(self) -> Dict[str, object]:
        """把 warm 里的模型全部读进内存。load_ms 只统计真正新加载的那部分。"""
        with self._load_lock:
            if self._pool and all(k in self._pool for k in self.warm):
                self.stats.loaded = True
                return {"load_ms": 0.0, "cached": True}
            t0 = time.monotonic()
            for key in self.warm:
                self._ensure(key)
            total = (time.monotonic() - t0) * 1000.0
            self.stats.loaded = bool(self._pool)
            self.stats.load_ms = total
            self.stats.sample_rate = self._sr
            return {"load_ms": total, "cached": False}

    def _ensure(self, key: str):
        with self._load_lock:
            if key in self._pool:
                return self._pool[key]
            if key not in MODELS:
                raise KeyError("未知模型：%s" % key)
            import sherpa_onnx as so

            t0 = time.monotonic()
            eng = so.OfflineTts(self._cfg_for(key))
            ms = (time.monotonic() - t0) * 1000.0
            self._pool[key] = eng
            self._sr = int(getattr(eng, "sample_rate", 22050) or 22050)
            self.stats.models[key] = {
                "load_ms": round(ms, 1),
                "sample_rate": int(getattr(eng, "sample_rate", 0) or 0),
                "speakers": int(getattr(eng, "num_speakers", 0) or 0),
                "license": MODELS[key].get("license", ""),
            }
            return eng

    def unload(self) -> int:
        with self._lock:
            self._pool.clear()
        self.stats.loaded = False
        self.stats.models.clear()
        return 0

    @property
    def sample_rate(self) -> int:
        return int(self._sr)

    def status(self) -> Dict[str, object]:
        rss = _rss_mb()
        return {
            "loaded": bool(self._pool),
            "engine": "sherpa",
            "model": ",".join(sorted(self._pool)) or "",
            "dtype": "onnx-cpu(int8/fp32)",
            "device": "cpu",
            "sample_rate": self.sample_rate,
            "speakers": sorted(self._pool),
            "vram_alloc_mb": 0,
            "vram_peak_mb": 0,
            "vram_free_mb": 0,
            "ram_rss_mb": round(rss, 1),
            "ram_models_mb": round(max(0.0, rss - self._base_rss), 1),
            "load_ms": round(self.stats.load_ms, 1),
            "synth_count": self.stats.synth_count,
            "last_synth_ms": round(self.stats.last_synth_ms, 1),
            "models": dict(self.stats.models),
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
        """返回 (int16 PCM, 采样率, 合成耗时 ms)。

        speaker 是 voice id（见 tts/voices.py）；不认识就回落默认音色，
        绝不因为音色名写错而返回空音频。
        """
        v = resolve(speaker, "sherpa")
        key = v["model"]
        sid = int(v.get("sid") or 0)
        t0 = time.monotonic()
        with self._lock:
            eng = self._ensure(key)          # 换模型时才会真的加载（warm 内的模型早已常驻）
            a = eng.generate(text, sid=sid, speed=1.0)
        samples = np.asarray(a.samples, dtype=np.float32)
        sr = int(getattr(a, "sample_rate", 0) or self._sr)
        if samples.size:
            samples = np.clip(samples, -1.0, 1.0)
            pcm = (samples * 32767.0).astype(np.int16)
        else:
            pcm = np.zeros(0, dtype=np.int16)

        # 语速：和 Piper 一样用 WSOLA 保持音高（模型自带的 speed 参数实测不稳）
        try:
            spd = max(SPEED_MIN, min(SPEED_MAX, float(speed) if speed else 1.0))
        except Exception:
            spd = 1.0
        if abs(spd - 1.0) > 0.02 and pcm.size:
            from .engine_piper import _stretch
            pcm = _stretch(pcm, sr, spd)
        if self.trim_silence and pcm.size:
            from .engine_piper import _trim
            pcm = _trim(pcm, sr)

        ms = (time.monotonic() - t0) * 1000.0
        self.stats.synth_count += 1
        self.stats.last_synth_ms = ms
        self.stats.last_audio_ms = pcm.size * 1000.0 / max(1, sr)
        return pcm, sr, ms
