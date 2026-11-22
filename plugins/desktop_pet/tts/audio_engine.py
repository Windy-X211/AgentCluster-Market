# coding=utf-8
"""实时音频引擎：QAudioSink + 语音/BGM 混音 + 播放时钟。

为什么不用 QMediaPlayer：
  * 起播 100~300ms，无法与 BGM 混音，也拿不到稳定的播放时钟；
  * 这里用 push 模式 + 10ms tick，既能精确计时，也能做 ducking。

字幕时钟取自 QAudioOutput.processedUSecs()，即「真正从设备播出去的样本」，
所以不存在 QTimer 累积漂移。
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtMultimedia import QAudio, QAudioDeviceInfo, QAudioFormat, QAudioOutput

from .bgm import loop_to_length
from .resample import resample_int16


def _soft_limit(x: np.ndarray, knee: float = 0.9) -> np.ndarray:
    """软限幅：|x| ≤ knee 原样通过，超过的部分 tanh 平滑压缩到 1.0 以内。"""
    try:
        a = np.abs(x)
        over = a > knee
        if not bool(over.any()):
            return x
        span = max(1e-6, 1.0 - knee)
        lim = knee + span * np.tanh((a - knee) / span)
        return np.where(over, np.sign(x) * lim, x).astype(np.float32)
    except Exception:
        return np.clip(x, -1.0, 1.0)


class AudioEngine(QObject):
    finished = pyqtSignal()
    tick = pyqtSignal(float)          # 当前播放头（ms），-1 表示尚未出声

    def __init__(self, sample_rate: int = 48000, prebuffer_ms: int = 320,
                 buffer_ms: int = 60, parent=None):
        super().__init__(parent)
        self.preferred_sr = int(sample_rate)
        self.prebuffer_ms = int(prebuffer_ms)
        self.buffer_ms = int(buffer_ms)

        self._fmt: Optional[QAudioFormat] = None
        self._out: Optional[QAudioOutput] = None
        self._dev = None
        self._device_name = ""
        self._sr = self.preferred_sr
        self._block = 480

        self._voice = np.zeros(0, dtype=np.float32)   # 设备采样率下的语音缓冲
        self._voice_pos = 0
        self._segments: List[Dict[str, object]] = []
        self._total_samples = 0
        self._eof = False
        self._started = False
        self._stopping = False
        self._starved = 0
        self._clock_base = 0.0
        self._tail = 0
        self._last_emit = 0.0
        self._starve_since = 0.0

        # BGM
        self._bgm: Optional[np.ndarray] = None
        self._bgm_pos = 0
        self._bgm_volume = 0.75
        self._bgm_duck = 0.55
        self._bgm_gain = 1.0
        self._bgm_mode = "speech"
        self._bgm_crossfade = 0
        # 早进晚退 / 渐入渐出
        self._bgm_preroll_samples = 0     # 人声前先垫多久
        self._bgm_tail_samples = 0        # 人声结束后再延多久
        self._bgm_fade_samples = 0        # 淡入淡出时长
        self._bgm_played = 0              # 已播出的 BGM 样本数（算淡入用）
        self._lead_left = 0               # 还没播完的开场静音（垫在语音之前）
        # 等待垫场配音（唱歌合成期间循环播放的"清嗓子"）：独立声道，
        # 不占 _voice 的时间轴，所以不会把真正要唱的那段往后顶、也不会混进字幕/歌词。
        self._wait_unit: Optional[np.ndarray] = None
        self._wait_pos = 0
        self._wait_fade_left = 0
        self._wait_fade_samples = 0
        self._wait_loop = False
        self._tail_left = 0               # 尾奏剩余样本
        self._tail_on = False
        self._voice_onset_ms = 0.0        # 语音真正开始的时刻（相对设备时钟）
        self._voice_origin_ms = None      # 人声时间轴锚点（设备时钟，含 lead）
        self._ending = False

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._on_tick)
        self._dbg = bool(os.environ.get("DESKTOP_PET_TTS_DEBUG"))

    # ------------------------------------------------------------ 设备
    def open(self) -> Dict[str, object]:
        info = QAudioDeviceInfo.defaultOutputDevice()
        self._device_name = info.deviceName()

        fmt = QAudioFormat()
        fmt.setSampleRate(self.preferred_sr)
        fmt.setChannelCount(1)
        fmt.setSampleSize(16)
        fmt.setCodec("audio/pcm")
        fmt.setByteOrder(QAudioFormat.LittleEndian)
        fmt.setSampleType(QAudioFormat.SignedInt)

        supported = info.isFormatSupported(fmt)
        if not supported:
            fmt = info.nearestFormat(fmt)
        self._fmt = fmt
        self._sr = int(fmt.sampleRate())
        self._block = max(64, int(self._sr * 0.01))

        out = QAudioOutput(info, fmt, self)
        out.setBufferSize(max(2048, int(self._sr * 2 * self.buffer_ms / 1000.0)))
        out.setVolume(1.0)
        self._out = out
        self._dev = out.start()
        self._timer.start()

        self._info = {
            "device": self._device_name,
            "sample_rate": self._sr,
            "channels": int(fmt.channelCount()),
            "format_ok": bool(supported),
            "buffer_ms": self.buffer_ms,
        }
        return dict(self._info)

    @property
    def info(self) -> Dict[str, object]:
        return dict(getattr(self, "_info", {}))

    def reset(self) -> None:
        """清空待播语音（打断/新一句时用），保持设备与 BGM 不动。"""
        self._voice = np.zeros(0, dtype=np.float32)
        self._voice_pos = 0
        self._total_samples = 0
        self._segments = []
        self._eof = False
        self._stopping = False
        self._started = False
        self._starved = 0
        self._clock_base = 0.0
        self._lead_left = 0
        self._voice_onset_ms = 0.0
        self._lip_peak = 0.0
        self._voice_origin_ms = None
        self._ending = False
        self._starve_since = 0.0
        self._tail_left = 0
        self._tail_on = False
        self._bgm_played = 0
        self.stop_wait_voice(0)

    def close(self) -> None:
        self._timer.stop()
        if self._out is not None:
            try:
                self._out.stop()
            except Exception:
                pass
            self._out = None

    @property
    def sample_rate(self) -> int:
        return self._sr

    def clock_ms(self) -> float:
        """已播出的毫秒数；未起播返回 -1。"""
        if self._out is None or not self._started:
            return -1.0
        try:
            return self._out.processedUSecs() / 1000.0 - getattr(self, "_clock_base", 0.0)
        except Exception:
            return -1.0

    def voice_clock_ms(self) -> float:
        """语音播放头（相对第一句人声起点）。BGM 提前进场的那段静音不计入。

        字幕/逐字高亮必须用这个时钟，否则「BGM 先响 1.2 秒」会把字幕顶到人声前面。
        唱歌时前面还会有一段"清嗓子"的等待垫场（独立声道），设备时钟早就走了十几秒，
        所以人声起点必须按"第一段语音入队那一刻的设备时钟 + lead"锚定，不能从设备 0 算。
        """
        c = self.clock_ms()
        if c < 0:
            return -1.0
        if getattr(self, "_voice_origin_ms", None) is not None:
            return c - float(self._voice_origin_ms)
        return c - float(self._voice_onset_ms)

    def bgm_tail_ms(self) -> int:
        """尾奏实际时长（ms），给字幕"停留 3 秒"扣掉用。"""
        if not self._tail_on or self._bgm_tail_samples <= 0:
            return 0
        return int(round(self._bgm_tail_samples * 1000.0 / self._sr))

    def lip_level(self) -> float:
        """当前正在播的这一点人声有多"响"，0.0~1.0，用来驱动口型。

        取播放头前后各 25ms 窗口的 RMS（播放头之后那半是"马上要发出来的音"），
        这样口型会比声音略早几毫秒开、也能提前收，听感上是贴着的。
        """
        v = self._voice
        if v is None or getattr(v, "size", 0) <= 0:
            # 唱歌的等待期：嘴型跟着"清嗓子"那句走（它不在 _voice 里）
            return self._wait_lip_level()
        if int(getattr(self, "_lead_left", 0)) > 0:
            if self._wait_unit is not None:
                return self._wait_lip_level()
            return 0.0        # BGM 垫场还没结束，人声一个字都没出，嘴要闭着
        pos = int(self._voice_pos)
        win = max(1, int(self._sr * 0.025))
        a = max(0, pos - win)
        b = min(int(v.size), pos + win)
        if b <= a:
            return 0.0
        blk = v[a:b]
        try:
            rms = float(np.sqrt(float(np.mean(blk * blk))))
        except Exception:
            return 0.0
        # 逐段自适应量程：本地 Piper 人声 RMS 约 0.05~0.15，在线 Edge 的 mp3 解码后
        # 常见 0.15~0.35。固定系数会让在线口型一直顶到 1.0（张着嘴不动），
        # 本地又张不开。这里用本段自己的响度峰（90 分位）做 100% 基准，
        # 两个后端给出的嘴型幅度就一致了。
        peak = 0.0
        try:
            pos_ms = pos * 1000.0 / max(1, self._sr)
            for s in self._segments:
                if int(s.get("start_ms", 0)) <= pos_ms:
                    peak = float(s.get("lip_peak") or 0.0)
                else:
                    break
        except Exception:
            peak = 0.0
        if peak <= 0.02:
            peak = float(getattr(self, "_lip_peak", 0.0) or 0.0)
        if peak <= 0.02:
            peak = 0.18
        floor = max(0.006, peak * 0.06)
        lv = (rms - floor) / max(1e-4, (peak - floor))
        if lv <= 0.0:
            return 0.0
        return round(min(1.0, lv), 2)

    def _wait_lip_level(self) -> float:
        """等待配音的口型：按循环单元当前播放头附近 25ms 的 RMS 折算。"""
        unit = self._wait_unit
        if unit is None or unit.size <= 0:
            return 0.0
        win = max(1, int(self._sr * 0.025))
        pos = int(self._wait_pos)
        a = max(0, pos - win)
        b = min(int(unit.size), pos + win)
        if b <= a:
            return 0.0
        blk = unit[a:b]
        try:
            rms = float(np.sqrt(float(np.mean(blk * blk))))
        except Exception:
            return 0.0
        peak = float(getattr(self, "_lip_peak", 0.0) or 0.0)
        if peak <= 0.02:
            peak = 0.18
        floor = max(0.006, peak * 0.06)
        lv = (rms - floor) / max(1e-4, (peak - floor))
        return round(min(1.0, max(0.0, lv)), 2)

    def _calc_lip_peak(self, pcm_f: np.ndarray) -> float:
        """一段人声的响度基准：20ms 窗口 RMS 的 90 分位（避开爆破音的极值）。"""
        try:
            if pcm_f is None or pcm_f.size < 200:
                return 0.0
            win = max(1, int(self._sr * 0.02))
            step = win
            vals = []
            lim = int(pcm_f.size) - win
            for i in range(0, max(1, lim), step):
                blk = pcm_f[i:i + win]
                r = float(np.sqrt(float(np.mean(blk * blk))))
                if r > 1e-4:
                    vals.append(r)
            if not vals:
                return 0.0
            return float(np.percentile(np.asarray(vals, dtype=np.float32), 90))
        except Exception:
            return 0.0

    # ------------------------------------------------------------ 入队
    def enqueue(self, pcm16: np.ndarray, marks: Dict[str, object], src_sr: Optional[int] = None,
                lead_ms: int = 0, declick: bool = True, lip: bool = True) -> Dict[str, object]:
        """追加一段语音（PCM 24k/48k），返回这段的绝对时间轴。

        lead_ms：在这段语音前垫多长的静音（只在开头生效）。用来让人声等 BGM 先"进个场"，
        垫的这段静音里设备照样出 BGM，所以听感是「音乐先起、人声接上」。
        declick：在发声边界做 4ms 淡入淡出，防止段接缝爆音。流式投递时要传 False ——
        每块都做一次淡入淡出会在块边界挖出 4ms 空隙，听感是 320ms 一次的"喘气"，
        流式的淡入 / 收尾由调用方在首块和末块自己做。
        lip：是否用这一块更新口型响度基准。流式的大段拆成很多小块投递时传 False，
        否则每块都会改一次基准，嘴型会跟着抖。
        """
        src_sr = int(src_sr or self.preferred_sr)
        pcm = np.asarray(pcm16, dtype=np.int16)
        if src_sr != self._sr:
            pcm = resample_int16(pcm, src_sr, self._sr)
        pcm_f = pcm.astype(np.float32) / 32768.0
        if declick:
            pcm_f = self._declick(pcm_f)

        if lead_ms and self._total_samples == 0:
            lead = int(self._sr * max(0, int(lead_ms)) / 1000.0)
            if lead > 0:
                self._lead_left = lead
                self._voice_onset_ms = lead * 1000.0 / self._sr
        if self._total_samples == 0:
            # 人声时间轴的锚点：第一段语音入队时的设备时钟 + lead。
            # 唱歌等待垫场是独立声道，会把设备时钟推到十几秒，字幕若跟着设备时钟
            # 就会"还没唱到、颜色已经跑到前面"。
            try:
                c = self.clock_ms()
            except Exception:
                c = -1.0
            self._voice_origin_ms = float(c if c >= 0 else 0.0) + float(
                lead_ms if lead_ms else 0.0)

        start_sample = self._total_samples
        self._voice = np.concatenate([self._voice, pcm_f]) if self._voice.size else pcm_f
        self._total_samples += pcm_f.size

        start_ms = int(round(start_sample * 1000.0 / self._sr))
        dur_ms = int(round(pcm_f.size * 1000.0 / self._sr))
        # 每段的响度基准（给口型用）：在线/本地音量差一倍也能得到同样的嘴型幅度
        lip_peak = self._calc_lip_peak(pcm_f) if lip else 0.0
        self._lip_peak = lip_peak or self._lip_peak
        seg = dict(marks or {})
        seg.update({
            "start_ms": start_ms,
            "end_ms": start_ms + dur_ms,
            "dur_ms": dur_ms,
            "lip_peak": lip_peak,
            "i": len(self._segments),
        })
        self._segments.append(seg)

        if self._bgm is not None and not self._bgm_mode == "speech":
            pass
        if self._out is not None and self._started and self._out.state() == QAudio.SuspendedState:
            self._out.resume()
        # 上一句播完后 _on_tick 会停掉定时器；新一句入队必须重新开泵，
        # 否则语音堆在 _voice 里永远不播（第二句"没声音"的根因）。
        if not self._timer.isActive():
            self._timer.start()
        return seg

    # ------------------------------------------------------------ 等待垫场配音
    def set_wait_voice(self, pcm16, src_sr: Optional[int] = None, gap_ms: int = 600,
                       loop: bool = False) -> int:
        """设置"等待期说明配音"（唱歌合成十几秒里的那句解释）。

        独立声道：不进 `_voice`、不进字幕时间轴，也就不可能把真正的歌往后顶。
        默认只念一遍（循环会显得机械），念完自动收掉，背景音继续；
        歌如果来得更早，由 `stop_wait_voice()` 淡出打断。
        返回这一遍的时长（毫秒）。
        """
        self._wait_loop = bool(loop)
        src_sr = int(src_sr or self.preferred_sr)
        pcm = np.asarray(pcm16, dtype=np.int16)
        if pcm.ndim > 1:
            pcm = pcm[:, 0]
        if src_sr != self._sr:
            pcm = resample_int16(pcm, src_sr, self._sr)
        pcm_f = pcm.astype(np.float32) / 32768.0
        pcm_f = self._declick(pcm_f) if pcm_f.size > self._sr * 0.05 else pcm_f
        gap = np.zeros(int(self._sr * max(0, gap_ms) / 1000.0), dtype=np.float32)
        self._wait_unit = np.concatenate([pcm_f, gap]) if pcm_f.size else None
        self._wait_pos = 0
        self._wait_fade_left = 0
        self._wait_fade_samples = 0
        if self._wait_unit is not None and self._out is not None and not self._timer.isActive():
            self._timer.start()
        return int(round((self._wait_unit.size if self._wait_unit is not None else 0)
                         * 1000.0 / self._sr))

    def wait_voice_active(self) -> bool:
        """等待说明句是否还在播（字幕用）。"""
        return self._wait_unit is not None

    def wait_voice_ms(self) -> float:
        """等待说明句的播放头（毫秒），给逐字字幕对时用。"""
        if self._wait_unit is None:
            return -1.0
        return float(self._wait_pos) * 1000.0 / max(1, self._sr)

    def stop_wait_voice(self, fade_ms: int = 300) -> None:
        """把等待配音淡出掉（歌开口 / 出错 / 播完）。"""
        if self._wait_unit is None:
            return
        ms = max(0, int(fade_ms))
        if ms <= 0:
            self._wait_unit = None
            self._wait_pos = 0
            self._wait_fade_left = 0
            self._wait_fade_samples = 0
            self._wait_loop = False
            return
        self._wait_fade_samples = max(1, int(self._sr * ms / 1000.0))
        self._wait_fade_left = self._wait_fade_samples

    def _wait_block(self, n: int) -> Optional[np.ndarray]:
        """取 n 个样本的等待配音；只念一遍时到尾部就收掉（剩下补零）。"""
        unit = self._wait_unit
        if unit is None or unit.size == 0:
            return None
        out = np.zeros(n, dtype=np.float32)
        got = 0
        while got < n:
            take = min(n - got, unit.size - self._wait_pos)
            if take <= 0:
                break
            out[got:got + take] = unit[self._wait_pos:self._wait_pos + take]
            self._wait_pos += take
            got += take
            if self._wait_pos >= unit.size:
                if getattr(self, "_wait_loop", False):
                    self._wait_pos = 0        # 循环：句间 gap 就是自然的换气停顿
                else:
                    # 念完一遍就退场，别机械复读；背景音继续垫着
                    self._wait_unit = None
                    self._wait_pos = 0
                    self._wait_fade_left = 0
                    self._wait_fade_samples = 0
                    break
        return out

    def _declick(self, x: np.ndarray) -> np.ndarray:
        """段首/段尾各 4ms 淡入淡出，消掉段接缝的"啪"。

        MiMo 的 WAV 会裁掉首尾静音，段尾因此停在一个非零样本上；下一段又从
        非零样本开始，两段硬接就是一声爆音（听感像"滋"一下的电流声）。
        只对真正的发声边界做斜坡 —— 尾部补的静音不参与，免得把停顿吃掉。
        """
        try:
            n = int(x.size)
            k = int(self._sr * 0.004)
            if n <= 2 or k < 2:
                return x
            nz = np.flatnonzero(np.abs(x) > 0.004)
            if nz.size == 0:
                return x
            a = max(0, int(nz[0]) - k)
            b = min(n, int(nz[-1]) + 1 + k)
            k2 = min(k, (b - a) // 2)
            if k2 < 2:
                return x
            ramp = np.linspace(0.0, 1.0, k2, dtype=np.float32)
            x[a:a + k2] = x[a:a + k2] * ramp
            x[b - k2:b] = x[b - k2:b] * ramp[::-1]
        except Exception:
            return x
        return x

    def _bgm_lead_skip(self, a: np.ndarray) -> int:
        """跳过素材开头那段"几乎没声音"的前奏，返回起播样本位置。"""
        try:
            if a is None or a.size <= int(self._sr * 0.2):
                return 0
            win = max(1, int(self._sr * 0.1))
            step = max(1, int(self._sr * 0.05))
            lim = min(int(a.size), int(self._sr * 6.0))
            need = 0.03                     # 比这更轻的窗口算"没声"
            for i in range(0, max(1, lim - win), step):
                blk = a[i:i + win]
                if float(np.sqrt(float(np.mean(blk * blk)))) >= need:
                    return i
        except Exception:
            pass
        return 0

    def set_bgm(self, bgm: Optional[np.ndarray], volume: float = 0.85,
                duck: float = 0.60, mode: str = "speech",
                preroll_ms: int = 0, tail_ms: int = 0, fade_ms: int = 400) -> None:
        # 循环接缝预处理：把尾巴 40ms 与开头 40ms 等功率交叠，
        # 否则 30s 素材一循环就会「啪」一下，听感很脏。
        if bgm is not None and bgm.size > int(self._sr * 0.12):
            edge = int(self._sr * 0.04)
            ramp = np.linspace(0.0, 1.0, edge, dtype=np.float32)
            head = bgm[:edge].copy()
            bgm = bgm.copy()
            bgm[:edge] = head * ramp + bgm[-edge:] * (1.0 - ramp)
            bgm = bgm[:-edge]
        self._bgm = bgm
        self._bgm_volume = max(0.0, min(1.0, float(volume)))
        self._bgm_duck = max(0.0, min(1.0, float(duck)))
        self._bgm_mode = mode if mode in ("speech", "always") else "speech"
        # 有些素材开头几秒是近乎无声的前奏，"早进"就变成了进一片安静 → 听感是 BGM 根本没响。
        # 开头 6 秒内找到第一个"真有声"的 100ms 窗口，从那里起播；找不到就正常从头。
        self._bgm_pos = self._bgm_lead_skip(self._bgm) if self._bgm is not None else 0
        if self._dbg and self._bgm_pos:
            print("[ae] bgm 跳过开头 %dms 近静音" % int(self._bgm_pos * 1000 / self._sr),
                  flush=True)
        self._bgm_gain = 1.0
        self._bgm_played = 0
        self._bgm_fadeout = 0
        self._bgm_fadeout_left = 0
        self._bgm_preroll_samples = int(self._sr * max(0, int(preroll_ms)) / 1000.0)
        self._bgm_tail_samples = int(self._sr * max(0, int(tail_ms)) / 1000.0)
        self._bgm_fade_samples = int(self._sr * max(0, int(fade_ms)) / 1000.0)
        self._tail_left = 0
        self._tail_on = False
        self._ending = False

    def fade_out_bgm(self, ms: int = 600) -> None:
        """把当前 BGM 淡出后移除。

        "等待期垫背景音"用完要收掉：直接 set_bgm(None) 是硬切，会在歌声第一个音
        之前留一下突兀的断口；这里用一段线性淡出，由 `_on_tick` 播完自动清空。
        """
        if self._bgm is None:
            return
        self._bgm_fadeout = max(1, int(self._sr * max(0, int(ms)) / 1000.0))
        self._bgm_fadeout_left = self._bgm_fadeout

    def mark_eof(self) -> None:
        self._eof = True
        if self._out is not None and self._out.state() == QAudio.SuspendedState:
            self._out.resume()
        if not self._started:
            self._start_if_possible(force=True)

    # ------------------------------------------------------------ 内部
    def _push(self, data: np.ndarray) -> int:
        """写 int16 到设备，返回写入样本数。"""
        if self._out is None or self._dev is None:
            return 0
        free = int(self._out.bytesFree() // 2)
        if free <= 0:
            return 0
        n = min(int(data.size), free)
        if n <= 0:
            return 0
        self._dev.write(np.ascontiguousarray(data[:n]).tobytes())
        return n

    def _start_if_possible(self, force: bool = False) -> None:
        if self._started:
            return
        need = int(self._sr * self.prebuffer_ms / 1000.0)
        # 开场静音也算"有料可播"：人声还没到，但 BGM 已经在垫，设备可以起播
        avail = (self._voice.size - self._voice_pos) + self._lead_left
        # "always" 模式的 BGM 自己就是可播内容：唱歌等待期只有音乐、还没有人声时，
        # 也必须把设备拉起来，否则会一直 Idle（用户听到的就是一片安静）。
        music_only = (self._bgm is not None and self._bgm_mode == "always"
                      and self._bgm_volume > 0)
        if self._wait_unit is not None:
            music_only = True      # 等待配音本身就是可播内容（最开始几秒还没有 BGM）
        if not force and avail < need and not music_only:
            return
        if self._out is not None and self._out.state() == QAudio.SuspendedState:
            self._out.resume()
        self._started = True
        self._t0 = time.monotonic()
        # processedUSecs 是设备启动以来的累计值，reset 不会清零；
        # 记一个基线，让 clock_ms 每句都从 0 开始，字幕才不会漂。
        try:
            self._clock_base = self._out.processedUSecs() / 1000.0 if self._out is not None else 0.0
        except Exception:
            self._clock_base = 0.0

    def _bgm_samples(self, n: int) -> np.ndarray:
        if self._bgm is None or self._bgm_volume <= 0 or self._bgm.size == 0:
            return np.zeros(n, dtype=np.float32)
        total = self._bgm.size
        pos = self._bgm_pos
        # 取一段并用循环拼接到 n
        idx = (np.arange(pos, pos + n) % total)
        out = self._bgm[idx]
        self._bgm_pos = (pos + n) % total
        return out

    def _on_tick(self) -> None:
        if self._out is None:
            return
        if not self._started:
            self._start_if_possible()
            if not self._started:
                return

        # 欠载时引擎会 suspend 设备来"冻住"时钟；下一段语音到货（或 BGM 还在放）时必须唤醒，
        # 否则 processedUSecs 永远不动、spoken 永远等不到结束——字幕和 speaking 会一起卡死。
        try:
            if self._out.state() == QAudio.SuspendedState:
                if ((self._voice.size - self._voice_pos) > 0 or self._lead_left > 0
                        or (self._bgm is not None and self._bgm_mode == "always")
                        or self._wait_unit is not None):
                    self._out.resume()
        except Exception:
            pass

        free = int(self._out.bytesFree() // 2)
        if free < self._block:
            self._emit_clock()
            return

        # 一次喂到水位（最多 4 个块 ≈ 40ms），而不是每 tick 只写 10ms。
        # 旧写法要求 10ms 定时器"一次不丢"，但 Windows 定时器本身有 1~15ms 抖动，
        # 主线程还要跑 WebEngine 帧回传/JS 桥（实测占 35% 时设备只播了 8.99s/10s）：
        # 缓冲被抽干 → 一顿一顿 + 接缝爆音。填满后掉一次帧也能立刻补回来。
        n = min(free, self._block * 4)

        # 1) 开场静音：人声之前先让 BGM 独奏 lead_ms
        lead_n = min(n, self._lead_left)
        self._lead_left -= lead_n

        # 2) 语音块（从 lead_n 之后开始填）
        need_voice = n - lead_n
        avail = self._voice.size - self._voice_pos
        take = min(need_voice, max(0, avail))
        if take > 0:
            vblk = self._voice[self._voice_pos:self._voice_pos + take]
            self._voice_pos += take
        else:
            vblk = None

        # 3) BGM：音量 × 淡入 × duck × 尾奏淡出
        if self._bgm is not None and self._bgm_volume > 0:
            base = self._bgm_samples(n) * self._bgm_volume

            if self._bgm_fade_samples > 0:
                start = self._bgm_played
                idx = np.arange(start, start + n, dtype=np.float32)
                base = base * np.clip(idx / float(self._bgm_fade_samples), 0.0, 1.0)
            self._bgm_played += n

            if self._tail_on and self._bgm_tail_samples > 0:
                t = np.arange(n, dtype=np.float32)
                g = (self._tail_left - t) / float(self._bgm_tail_samples)
                base = base * np.clip(g, 0.0, 1.0)
                self._tail_left -= n

            # 只在真正有人声的块里压低 BGM；开场静音/尾奏期间保持原音量
            speaking_now = take > 0
            target = self._bgm_duck if speaking_now else 1.0
            coef = 0.012 if target < self._bgm_gain else 0.004
            self._bgm_gain = self._bgm_gain + (target - self._bgm_gain) * coef

            # 淡出收尾（唱歌开始前把"等待期背景音"收掉）
            if self._bgm_fadeout_left > 0:
                k = (self._bgm_fadeout_left - np.arange(n, dtype=np.float32)) / float(self._bgm_fadeout)
                base = base * np.clip(k, 0.0, 1.0)
                self._bgm_fadeout_left -= n
                if self._bgm_fadeout_left <= 0:
                    self._bgm = None

            mix = base * self._bgm_gain
        else:
            mix = np.zeros(n, dtype=np.float32)

        if vblk is not None:
            mix[lead_n:lead_n + take] += vblk
        # 等待期循环配音（唱歌合成期间）：独立声道，不与歌共享时间轴
        wblk = self._wait_block(n)
        if wblk is not None:
            if self._wait_fade_left > 0:
                k = (self._wait_fade_left - np.arange(n, dtype=np.float32)) / float(
                    max(1, self._wait_fade_samples))
                wblk = wblk * np.clip(k, 0.0, 1.0)
                self._wait_fade_left -= n
                if self._wait_fade_left <= 0:
                    self._wait_unit = None
                    self._wait_pos = 0
            mix = mix + wblk
        # 软限幅而不是硬 clip：人声峰值 0.88 + BGM（0.85×duck）相加会超过 1.0，
        # 硬裁剪会削出方波，"滋啦"一声就是用户说的磁噪音。0.90 以下原样保留，
        # 超过的部分用 tanh 平滑压进去，永远不会超出 ±1。
        mix = np.clip(mix, -1.0, 1.0) if float(np.max(np.abs(mix))) <= 0.9 else _soft_limit(mix, 0.9)

        # 调音验收用：每 40 个块打一次 BGM / 人声 / 合成后的 RMS，确认 BGM 真的"响"了
        if self._dbg and self._bgm is not None:
            self._rms_n = int(getattr(self, "_rms_n", 0)) + 1
            if self._rms_n % 40 == 0:
                try:
                    b = float(np.sqrt(float(np.mean(base * base)))) if self._bgm_volume > 0 else 0.0
                except Exception:
                    b = 0.0
                v = float(np.sqrt(float(np.mean(vblk * vblk)))) if vblk is not None else 0.0
                m = float(np.sqrt(float(np.mean(mix * mix))))
                print("[ae] rms bgm=%.3f voice=%.3f mix=%.3f duck=%.2f" % (b, v, m, self._bgm_gain),
                      flush=True)

        if self._dbg and self._voice_pos <= self._block * 2:
            print("[ae] tick n=%d lead=%d take=%s state=%s" % (
                n, lead_n, take, self._out.state()), flush=True)
        self._push((mix * 32767.0).astype(np.int16))
        if self._dbg and self._voice_pos <= self._block * 2:
            print("[ae] pushed ok", flush=True)

        # 人声空了；如果 BGM 是"always"模式且还在响，设备其实有料可播，
        # 不能按"欠载"处理 —— 否则会 8ms 一轮 suspend/resume（音乐一顿一顿的）。
        voice_empty = (self._voice.size - self._voice_pos) <= 0 and self._lead_left <= 0
        keep_music = (self._bgm is not None and self._bgm_mode == "always"
                      and self._bgm_volume > 0)
        if self._wait_unit is not None:
            keep_music = True      # 等待配音还在响：别把它按"欠载"掐掉
        starved = voice_empty and not keep_music
        if starved and not self._eof:
            self._starved += 1
            # 欠载立刻挂起，会让每次"下一段还在合成"的段间等待都走一趟 suspend/resume，
            # 接缝处必然爆一下。给 250ms 宽限：普通分段等待就不挂起，设备自然放空；
            # 真的长时间没料（合成慢/网络卡）才挂起冻住时钟。
            if self._starve_since <= 0.0:
                self._starve_since = time.monotonic()
            elif (self._out.state() == QAudio.ActiveState
                    and time.monotonic() - self._starve_since > 0.25):
                self._out.suspend()
        else:
            self._starve_since = 0.0

        # 收工判断用人声是否播完（voice_empty），不能用 starved：
        # BGM "always" 模式下 starved 恒 False，否则一直播着背景音永不结束。
        if self._eof and voice_empty:
            # 人声没了，但 BGM 还要"晚退"：先播完尾奏再收工
            if (self._bgm is not None and self._bgm_tail_samples > 0
                    and not self._tail_on and not self._ending):
                self._tail_on = True
                self._tail_left = self._bgm_tail_samples
                self._emit_clock()
                return
            if self._tail_on and self._tail_left > 0:
                self._emit_clock()
                return
            if self._bgm_mode == "speech":
                self._bgm = None
            self._finish()
            return

        # 裁剪已播过的语音，控制内存
        if self._voice_pos > self._sr * 5 and self._voice_pos > self._voice.size // 2:
            self._voice = self._voice[self._voice_pos:]
            self._voice_pos = 0

        self._emit_clock()

    def _emit_clock(self) -> None:
        now = time.monotonic()
        if now - self._last_emit < 0.02:
            return
        self._last_emit = now
        try:
            self.tick.emit(self.clock_ms())
        except Exception:
            pass

    def _finish(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._timer.stop()
        try:
            self.tick.emit(self.duration_ms())
        except Exception:
            pass
        self.finished.emit()

    # ------------------------------------------------------------ 信息
    def duration_ms(self) -> int:
        return int(round(self._total_samples * 1000.0 / self._sr))

    @property
    def segments(self) -> List[Dict[str, object]]:
        return self._segments

    def stats(self) -> Dict[str, object]:
        return {
            "device": self._device_name,
            "sample_rate": self._sr,
            "segments": len(self._segments),
            "duration_ms": self.duration_ms(),
            "starved_ticks": self._starved,
        }
