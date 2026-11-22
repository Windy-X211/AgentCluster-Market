"""基于 PortAudio(sounddevice) 的播放引擎。

与 audio_engine.AudioEngine 同接口，区别只在底层：Qt push-mode 的 QAudioOutput
在某些组合下会触发原生 fastfail(0xC0000409)，PortAudio 回调线程更稳，且首声延迟更低。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import numpy as np
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

SR = 48000


class SdAudioEngine(QObject):

    finished = pyqtSignal()
    tick = pyqtSignal(float)

    def __init__(self, sample_rate: int = SR, prebuffer_ms: int = 320, parent=None):
        super().__init__(parent)
        self._sr = int(sample_rate)
        self.prebuffer_ms = int(prebuffer_ms)
        self._block = 480                      # 10ms @48k
        self._lock = threading.Lock()
        self._voice = np.zeros(0, dtype=np.float32)   # 已重采样到 _sr 的 float32
        self._voice_pos = 0
        self._eof = False
        self._stream = None
        self._bgm: Optional[np.ndarray] = None
        self._bgm_pos = 0
        self._bgm_volume = 0.0
        self._bgm_duck = 0.25
        self._bgm_gain = 1.0
        self._segments: List[Dict[str, object]] = []
        self._total_ms = 0.0
        self._started = False
        self._start_ms = 0.0
        self._played = 0                        # 已播放的语音帧数
        self._starved = 0
        self._done = False

        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------ 生命周期
    def open(self) -> Dict[str, object]:
        if self._stream is not None:
            return self.info
        import sounddevice as sd
        st = sd.OutputStream(samplerate=self._sr, channels=1, dtype="int16",
                             blocksize=self._block, callback=self._cb, latency="low")
        st.start()
        self._stream = st
        dev = sd.query_devices(kind="output")
        self._info = {"device": dev.get("name", ""), "sample_rate": self._sr,
                      "channels": 1, "format_ok": True,
                      "buffer_ms": int(round(self._block * 1000 / self._sr))}
        return self.info

    @property
    def info(self) -> Dict[str, object]:
        return dict(getattr(self, "_info", {}))

    def close(self) -> None:
        self._timer.stop()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None

    def reset(self) -> None:
        with self._lock:
            self._voice = np.zeros(0, dtype=np.float32)
            self._voice_pos = 0
            self._eof = False
            self._segments = []
            self._total_ms = 0.0
            self._started = False
            self._played = 0
            self._starved = 0
            self._done = False
            self._bgm_pos = 0

    # ------------------------------------------------------------ 播放
    def enqueue(self, pcm16: np.ndarray, marks: Dict[str, object],
                src_sr: Optional[int] = None) -> Dict[str, object]:
        from .resample import resample_float
        pcm = np.asarray(pcm16)
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        f = pcm.astype(np.float32) / 32768.0
        sr = int(src_sr or self._sr)
        if sr != self._sr:
            f = resample_float(f, sr, self._sr)
        dur_ms = int(round(f.size * 1000.0 / self._sr))
        start = int(self._total_ms)
        with self._lock:
            self._voice = np.concatenate([self._voice, f]) if self._voice.size else f
            seg = {"text": str(marks.get("text", "")), "start_ms": start,
                   "end_ms": start + dur_ms, "dur_ms": dur_ms, "i": len(self._segments)}
            self._segments.append(seg)
            self._total_ms += dur_ms
        if not self._timer.isActive():
            self._timer.start()
        return seg

    def mark_eof(self) -> None:
        with self._lock:
            self._eof = True
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self.reset()

    # ------------------------------------------------------------ BGM
    def set_bgm(self, samples, volume: float = 0.35, duck: float = 0.25) -> None:
        a = np.asarray(samples)
        if a.dtype == np.int16:
            a = a.astype(np.float32) / 32768.0
        else:
            a = a.astype(np.float32)
        if a.ndim > 1:
            a = a[:, 0]
        with self._lock:
            self._bgm = a if a.size else None
            self._bgm_pos = 0
            self._bgm_volume = float(volume)
            self._bgm_duck = float(duck)
            self._bgm_gain = 1.0

    def clear_bgm(self) -> None:
        with self._lock:
            self._bgm = None
            self._bgm_volume = 0.0

    # ------------------------------------------------------------ 时钟
    def clock_ms(self) -> float:
        if not self._started:
            return -1.0
        return self._played * 1000.0 / self._sr

    @property
    def done(self) -> bool:
        return self._done

    def stats(self) -> Dict[str, object]:
        return {"device": self.info.get("device", ""), "sample_rate": self._sr,
                "segments": len(self._segments),
                "duration_ms": int(round(self._total_ms)),
                "starved_ticks": self._starved}

    # ------------------------------------------------------------ 音频回调
    def _cb(self, outdata, frames, time_info, status):  # noqa: A002
        n = frames
        with self._lock:
            avail = self._voice.size - self._voice_pos
            if not self._started:
                need = int(self._sr * self.prebuffer_ms / 1000.0)
                if avail < need and not self._eof:
                    outdata[:] = 0
                    return
                self._started = True
                self._start_ms = time.monotonic()
            take = int(min(n, max(0, avail)))
            v = self._voice[self._voice_pos:self._voice_pos + take] if take else None
            self._voice_pos += take
            # BGM
            if self._bgm is not None and self._bgm.size and self._bgm_volume > 0:
                total = self._bgm.size
                idx = (np.arange(self._bgm_pos, self._bgm_pos + n) % total)
                d = self._bgm[idx]
                self._bgm_pos = (self._bgm_pos + n) % total
                target = self._bgm_duck if take else 1.0
                coef = 0.012 if target < self._bgm_gain else 0.004
                self._bgm_gain += (target - self._bgm_gain) * coef
                mix = d * (self._bgm_volume * self._bgm_gain)
            else:
                mix = np.zeros(n, dtype=np.float32)
            if v is not None:
                if mix.size != v.size:
                    mix = mix[:v.size]
                mix = mix + v
            if take:
                self._played += take
            if self._voice.size - self._voice_pos <= 0 and self._eof:
                self._done = True
            elif self._voice.size - self._voice_pos <= 0:
                self._starved += 1
        pcm = (np.clip(mix, -1.0, 1.0) * 32767.0).astype(np.int16)
        if pcm.size < n:
            pad = np.zeros(n - pcm.size, dtype=np.int16)
            pcm = np.concatenate([pcm, pad])
        outdata[:, 0] = pcm

    # ------------------------------------------------------------ 主线程轮询
    def _poll(self) -> None:
        c = self.clock_ms()
        self.tick.emit(c)
        if self._done:
            self._timer.stop()
            self._done = False
            self.finished.emit()
