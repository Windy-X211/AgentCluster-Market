"""recorder.py —— 屏幕录制模块

使用 opencv-python 进行视频编码，支持：
- 区域/全屏录制
- 暂停/恢复/停止/丢弃
- 音频采集（WASAPI loopback + PyAudio）
- 浮层管理
"""
from __future__ import annotations

import io
import os
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

try:
    import numpy as np
    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False

try:
    import mss
    _MSS_OK = True
except ImportError:
    _MSS_OK = False

# 录制状态常量
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"
STATE_DISCARDED = "discarded"


class RecordingSession:
    """录制会话状态机

    状态转换：
    idle → recording：record() 成功
    recording → paused：record_control(mode=pause)
    paused → recording：record_control(mode=resume)
    recording → stopped：record_control(mode=stop) / 自动停止
    paused → stopped：record_control(mode=stop)
    recording → discarded：record_control(mode=discard)
    paused → discarded：record_control(mode=discard)
    """

    def __init__(self):
        self._state = STATE_IDLE
        self._lock = threading.RLock()
        self._start_time = 0.0
        self._pause_time = 0.0
        self._paused_duration = 0.0
        self._save_path = ""
        self._region = None
        self._duration = 0
        self._audio_source = "none"
        self._recording_id = ""
        self._frame_rate = 15
        self._video_writer = None
        self._running = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def elapsed(self) -> float:
        with self._lock:
            if self._state == STATE_RECORDING:
                return _time.time() - self._start_time - self._paused_duration
            elif self._state == STATE_PAUSED:
                return self._pause_time - self._start_time - self._paused_duration
            return 0.0

    def can_transition_to(self, new_state: str) -> bool:
        """检查状态转换是否合法"""
        with self._lock:
            current = self._state

            transitions = {
                STATE_IDLE: [STATE_RECORDING],
                STATE_RECORDING: [STATE_PAUSED, STATE_STOPPED, STATE_DISCARDED],
                STATE_PAUSED: [STATE_RECORDING, STATE_STOPPED, STATE_DISCARDED],
                STATE_STOPPED: [],
                STATE_DISCARDED: [],
            }

            return new_state in transitions.get(current, [])

    def start(self, save_path: str, region: dict | None, duration: int,
              audio_source: str, recording_id: str) -> dict:
        """开始录制"""
        with self._lock:
            if self._state != STATE_IDLE:
                return {"ok": False, "error": f"当前状态 {self._state} 无法开始录制"}

            self._state = STATE_RECORDING
            self._start_time = _time.time()
            self._pause_time = 0.0
            self._paused_duration = 0.0
            self._save_path = save_path
            self._region = region
            self._duration = duration
            self._audio_source = audio_source
            self._recording_id = recording_id
            self._running = True

            return {
                "ok": True,
                "recording_id": recording_id,
                "record_status": "recording",
                "elapsed": 0,
                "audio_source": audio_source,
            }

    def pause(self) -> dict:
        """暂停录制"""
        with self._lock:
            if not self.can_transition_to(STATE_PAUSED):
                return {"ok": False, "error": f"当前状态 {self._state} 无法暂停"}

            self._state = STATE_PAUSED
            self._pause_time = _time.time()
            return {"ok": True, "record_status": "paused", "elapsed": self.elapsed}

    def resume(self) -> dict:
        """恢复录制"""
        with self._lock:
            if not self.can_transition_to(STATE_RECORDING):
                return {"ok": False, "error": f"当前状态 {self._state} 无法恢复"}

            self._state = STATE_RECORDING
            self._paused_duration += _time.time() - self._pause_time
            return {"ok": True, "record_status": "recording", "elapsed": self.elapsed}

    def stop(self) -> dict:
        """停止录制"""
        with self._lock:
            if not self.can_transition_to(STATE_STOPPED):
                return {"ok": False, "error": f"当前状态 {self._state} 无法停止"}

            self._state = STATE_STOPPED
            self._running = False
            return {
                "ok": True,
                "record_status": "stopped",
                "saved_path": self._save_path,
                "elapsed": self.elapsed,
            }

    def discard(self) -> dict:
        """丢弃录制"""
        with self._lock:
            if not self.can_transition_to(STATE_DISCARDED):
                return {"ok": False, "error": f"当前状态 {self._state} 无法丢弃"}

            self._state = STATE_DISCARDED
            self._running = False
            return {"ok": True, "record_status": "discarded"}

    def get_status(self) -> dict:
        """获取状态"""
        with self._lock:
            return {
                "state": self._state,
                "recording_id": self._recording_id,
                "elapsed": self.elapsed,
                "save_path": self._save_path,
                "audio_source": self._audio_source,
            }


class FrameCollector:
    """帧采集循环

    使用 mss 持续采集屏幕帧，支持指定帧率和区域。
    复用 mss 实例以避免每帧创建/销毁上下文的开销。
    """

    def __init__(self, frame_rate: int = 15, region: dict | None = None):
        self._frame_rate = frame_rate
        self._region = region
        self._running = False
        self._frame_interval = 1.0 / frame_rate
        self._last_frame_time = 0.0
        self._sct = None
        self._monitor = None

    def start(self):
        """启动帧采集"""
        if not _MSS_OK:
            return False
        self._sct = mss.mss()
        if self._region is None:
            self._monitor = self._sct.monitors[0]
        else:
            mw = self._sct.monitors[0]["width"]
            mh = self._sct.monitors[0]["height"]
            x1 = int(self._region["x1"] * mw)
            y1 = int(self._region["y1"] * mh)
            x2 = int(self._region["x2"] * mw)
            y2 = int(self._region["y2"] * mh)
            self._monitor = {"top": y1, "left": x1, "width": x2 - x1, "height": y2 - y1}
        self._running = True
        self._last_frame_time = _time.time()
        return True

    def stop(self):
        """停止帧采集"""
        self._running = False
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None

    def get_frame(self) -> np.ndarray | None:
        """获取一帧

        Returns:
            BGR 格式帧，或 None（采集失败或未到帧间隔）
        """
        if not self._running:
            return None

        if not _MSS_OK or not _NUMPY_OK or not _CV2_OK:
            return None

        current_time = _time.time()
        if current_time - self._last_frame_time < self._frame_interval:
            return None

        try:
            screenshot = self._sct.grab(self._monitor)
            img = np.frombuffer(screenshot.rgb, dtype=np.uint8)
            img = img.reshape((screenshot.height, screenshot.width, 3))
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            self._last_frame_time = current_time
            return img
        except Exception:
            return None


class VideoEncoder:
    """视频编码器

    使用 ffmpeg 进行 H.264 编码。
    """

    def __init__(self, save_path: str, frame_rate: int = 15,
                 frame_size: tuple[int, int] = (1920, 1080)):
        self._save_path = save_path
        self._frame_rate = frame_rate
        self._frame_size = frame_size
        self._process = None
        self._codec = "libx264"  # 默认 H.264 编码

    def start(self) -> bool:
        """启动编码器"""
        import subprocess
        import os

        # ffmpeg 可执行文件路径
        ffmpeg_path = r"F:\tools\ffmpeg-2026-06-08-git-6028720d70-essentials_build\bin\ffmpeg.exe"

        try:
            # 使用 ffmpeg 进行编码
            cmd = [
                ffmpeg_path,
                "-y",  # 覆盖输出文件
                "-f", "rawvideo",
                "-vcodec", "rawvideo",
                "-s", f"{self._frame_size[0]}x{self._frame_size[1]}",
                "-pix_fmt", "bgr24",
                "-r", str(self._frame_rate),
                "-i", "-",  # 从 stdin 读取
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                self._save_path,
            ]

            # Windows: 隐藏控制台窗口
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            print(f"VideoEncoder: Starting ffmpeg with command: {' '.join(cmd)}", flush=True)

            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
            )

            poll_result = self._process.poll()
            if poll_result is not None:
                print(f"ffmpeg process failed to start, poll={poll_result}", flush=True)
                return False

            return True
        except Exception as e:
            print(f"VideoEncoder.start exception: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return False

    def write_frame(self, frame: np.ndarray) -> bool:
        """写入一帧"""
        if self._process is None or self._process.stdin is None:
            return False

        try:
            self._process.stdin.write(frame.tobytes())
            return True
        except Exception:
            return False

    def stop(self):
        """停止编码器"""
        if self._process is not None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.wait(timeout=5.0)
            except Exception:
                self._process.terminate()
            finally:
                self._process = None

class AudioCollector:
    """音频采集器

    支持三种音频来源：
    - system: 系统声音（WASAPI loopback 或立体声混音）
    - microphone: 麦克风输入
    - both: 同时采集系统声音和麦克风

    采集的音频写入 WAV 文件，供后续与视频合成。
    """

    def __init__(self, audio_source: str, output_path: str,
                 sample_rate: int = 44100, channels: int = 2,
                 chunk_size: int = 1024):
        self._audio_source = audio_source
        self._output_path = output_path
        self._sample_rate = sample_rate
        self._channels = channels
        self._chunk_size = chunk_size
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pa = None
        self._stream = None
        self._wave_file = None
        self._device_index = None

    def _find_loopback_device(self) -> int | None:
        """查找系统声音环回设备（WASAPI loopback 或立体声混音）"""
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                name = info["name"]
                if info["maxInputChannels"] > 0:
                    if "立体声混音" in name or "Stereo Mix" in name or "stereo mix" in name.lower():
                        pa.terminate()
                        return i
            pa.terminate()
        except Exception:
            pass

        try:
            from pycaw.pycaw import AudioUtilities, IAudioClient
            from ctypes import cast, pointer
            import comtypes

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(
                IAudioClient._iid_, comtypes.CLSCTX_ALL, None
            )
            _ = cast(interface, pointer(IAudioClient))
            return None
        except Exception:
            pass

        return None

    def _find_microphone_device(self) -> int | None:
        """查找麦克风设备"""
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                name = info["name"]
                if info["maxInputChannels"] > 0:
                    if "Realtek" in name and "麦克风" in name:
                        pa.terminate()
                        return i
            pa.terminate()
        except Exception:
            pass
        return None

    def start(self) -> bool:
        """启动音频采集"""
        try:
            import pyaudio
            import wave

            if self._audio_source == "system":
                self._device_index = self._find_loopback_device()
                if self._device_index is None:
                    return False
            elif self._audio_source == "microphone":
                self._device_index = self._find_microphone_device()
            elif self._audio_source == "both":
                self._device_index = self._find_microphone_device()
            else:
                return False

            self._wave_file = wave.open(self._output_path, "wb")
            self._wave_file.setnchannels(self._channels)
            self._wave_file.setsampwidth(2)
            self._wave_file.setframerate(self._sample_rate)

            self._stop_event.clear()
            self._pause_event.clear()
            self._thread = threading.Thread(
                target=self._capture_loop,
                name="audio-collector",
                daemon=True,
            )
            self._thread.start()
            return True
        except Exception:
            return False

    def _capture_loop(self):
        """音频采集循环"""
        try:
            import pyaudio

            self._pa = pyaudio.PyAudio()

            open_kwargs = {
                "format": pyaudio.paInt16,
                "channels": self._channels,
                "rate": self._sample_rate,
                "input": True,
                "frames_per_buffer": self._chunk_size,
            }
            if self._device_index is not None:
                open_kwargs["input_device_index"] = self._device_index

            self._stream = self._pa.open(**open_kwargs)

            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    _time.sleep(0.05)
                    continue
                try:
                    data = self._stream.read(self._chunk_size, exception_on_overflow=False)
                    self._wave_file.writeframes(data)
                except Exception:
                    break
        except Exception:
            pass
        finally:
            try:
                if self._stream:
                    self._stream.stop_stream()
                    self._stream.close()
            except Exception:
                pass
            try:
                if self._pa:
                    self._pa.terminate()
            except Exception:
                pass
            try:
                if self._wave_file:
                    self._wave_file.close()
            except Exception:
                pass

    def pause(self):
        """暂停采集"""
        self._pause_event.set()

    def resume(self):
        """恢复采集"""
        self._pause_event.clear()

    def stop(self):
        """停止采集"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)