"""desktop_control 插件 —— 桌面控制

支持屏幕截图、鼠标交互、键盘输入、屏幕录制、音频采集。

调用签名 (由 tool.yaml 的 parameters 决定)：
    run(action="screenshot", purpose="inspect", region={...}, ...)
    run(action="click", x=0.5, y=0.5, ...)
    run(action="key_press", key="enter", ...)
    run(action="record", save_path="...", ...)

13 个 action：
screenshot / click / double_click / right_click / scroll / drag / wait
key_press / hotkey / type_text / record / record_control
"""
from __future__ import annotations

import base64
import io
import json as _json
import os
import re
import sys
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许独立 import（不依赖 backend 作为包）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

# ==============================================================
# 模块级状态（进程级，多次 run() 调用间保持）
# ==============================================================
_lock = threading.Lock()

# 屏幕分辨率（延迟初始化）
_SCREEN_WIDTH: int = 0
_SCREEN_HEIGHT: int = 0

# 录制会话状态
_RECORDING_SESSION: dict = {}
_RECORDING_LOCK = threading.Lock()

# 浮层窗口引用
_OVERLAY_WINDOW = None
# ==============================================================
# 延迟导入
# ==============================================================
def _import_modules():
    """延迟导入模块，避免启动时依赖检查失败"""
    global _MSS_OK, _PYAUTOGUI_OK, _PYPERCLIP_OK, _CV2_OK, _NUMPY_OK, _PYQT5_OK

    try:
        import mss
        _MSS_OK = True
    except ImportError:
        _MSS_OK = False

    try:
        import pyautogui
        _PYAUTOGUI_OK = True
    except ImportError:
        _PYAUTOGUI_OK = False

    try:
        import pyperclip
        _PYPERCLIP_OK = True
    except ImportError:
        _PYPERCLIP_OK = False

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
        from PyQt5.QtWidgets import QWidget, QLabel, QHBoxLayout
        from PyQt5.QtCore import Qt, QTimer
        from PyQt5.QtGui import QColor, QFont
        _PYQT5_OK = True
    except ImportError:
        _PYQT5_OK = False


# ==============================================================
# 屏幕分辨率管理
# ==============================================================
def _get_screen_resolution() -> tuple[int, int]:
    """获取屏幕分辨率（延迟初始化）"""
    global _SCREEN_WIDTH, _SCREEN_HEIGHT

    if _SCREEN_WIDTH == 0 or _SCREEN_HEIGHT == 0:
        if not _MSS_OK:
            raise RuntimeError("mss 库未安装，请执行 pip install mss")

        import mss
        with mss.mss() as s:
            monitor = s.monitors[0]
            _SCREEN_WIDTH = monitor["width"]
            _SCREEN_HEIGHT = monitor["height"]

    return _SCREEN_WIDTH, _SCREEN_HEIGHT


# ==============================================================
# 坐标换算
# ==============================================================
def _normalize_to_pixel(x: float, y: float) -> tuple[int, int]:
    """归一化坐标转像素坐标"""
    width, height = _get_screen_resolution()

    if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
        raise ValueError(f"坐标越界: x={x}, y={y}，必须在 [0.0, 1.0] 范围内")

    px = round(x * width)
    py = round(y * height)
    return px, py


# ==============================================================
# 截图处理
# ==============================================================
def _handle_screenshot(kwargs: dict) -> dict:
    """处理截图请求"""
    purpose = kwargs.get("purpose", "inspect")
    region = kwargs.get("region")
    save_path = kwargs.get("save_path")

    if not _MSS_OK:
        return {"ok": False, "error": "mss 库未安装，请执行 pip install mss"}

    import mss
    from PIL import Image

    try:
        with mss.mss() as s:
            if region is None:
                monitor = s.monitors[0]
                screenshot = s.grab(monitor)
            else:
                # 区域截图
                x1 = int(region.get("x1", 0) * s.monitors[0]["width"])
                y1 = int(region.get("y1", 0) * s.monitors[0]["height"])
                x2 = int(region.get("x2", 1.0) * s.monitors[0]["width"])
                y2 = int(region.get("y2", 1.0) * s.monitors[0]["height"])
                monitor = {"top": y1, "left": x1, "width": x2 - x1, "height": y2 - y1}
                screenshot = s.grab(monitor)

            img = Image.frombytes("RGB", (screenshot.width, screenshot.height), screenshot.rgb)
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            png_bytes = buffer.getvalue()

        if purpose == "save":
            if not save_path:
                return {"ok": False, "error": "save 模式需要 save_path 参数"}

            path = Path(save_path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "wb") as f:
                f.write(png_bytes)

            return {
                "ok": True,
                "action": "screenshot",
                "purpose": "save",
                "saved_path": str(path),
            }
        else:
            # inspect 模式：返回 base64
            base64_str = base64.b64encode(png_bytes).decode("utf-8")
            truncated = False

            # 检查是否超过 2MB
            if len(base64_str) > 2 * 1024 * 1024 * 0.75:
                # 缩放处理
                scale = 0.5
                while len(base64_str) > 2 * 1024 * 1024 * 0.75 and scale >= 0.1:
                    new_size = (int(img.width * scale), int(img.height * scale))
                    resized = img.resize(new_size, Image.LANCZOS)
                    buffer = io.BytesIO()
                    resized.save(buffer, format="PNG")
                    base64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
                    scale -= 0.1
                truncated = True

            return {
                "ok": True,
                "action": "screenshot",
                "purpose": "inspect",
                "data": base64_str,
                "truncated": truncated,
            }

    except Exception as e:
        return {"ok": False, "error": f"截图失败: {str(e)}"}


# ==============================================================
# 鼠标处理
# ==============================================================
def _handle_click(kwargs: dict) -> dict:
    """处理单击"""
    if not _PYAUTOGUI_OK:
        return {"ok": False, "error": "pyautogui 库未安装，请执行 pip install pyautogui"}

    import pyautogui

    x = kwargs.get("x", 0.5)
    y = kwargs.get("y", 0.5)

    try:
        px, py = _normalize_to_pixel(x, y)
        pyautogui.click(px, py)
        _time.sleep(0.1)
        return {"ok": True, "action": "click", "x": x, "y": y}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_double_click(kwargs: dict) -> dict:
    """处理双击"""
    if not _PYAUTOGUI_OK:
        return {"ok": False, "error": "pyautogui 库未安装，请执行 pip install pyautogui"}

    import pyautogui

    x = kwargs.get("x", 0.5)
    y = kwargs.get("y", 0.5)

    try:
        px, py = _normalize_to_pixel(x, y)
        pyautogui.doubleClick(px, py)
        return {"ok": True, "action": "double_click", "x": x, "y": y}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_right_click(kwargs: dict) -> dict:
    """处理右键"""
    if not _PYAUTOGUI_OK:
        return {"ok": False, "error": "pyautogui 库未安装，请执行 pip install pyautogui"}

    import pyautogui

    x = kwargs.get("x", 0.5)
    y = kwargs.get("y", 0.5)

    try:
        px, py = _normalize_to_pixel(x, y)
        pyautogui.rightClick(px, py)
        return {"ok": True, "action": "right_click", "x": x, "y": y}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_scroll(kwargs: dict) -> dict:
    """处理滚动"""
    if not _PYAUTOGUI_OK:
        return {"ok": False, "error": "pyautogui 库未安装，请执行 pip install pyautogui"}

    import pyautogui

    amount = kwargs.get("amount", 0)
    x = kwargs.get("x")
    y = kwargs.get("y")

    if amount == 0:
        return {"ok": False, "error": "滚动量不能为 0"}

    if abs(amount) > 20:
        return {"ok": False, "error": f"滚动量 |amount| 必须 <= 20，当前: {amount}"}

    try:
        if x is not None and y is not None:
            px, py = _normalize_to_pixel(x, y)
            pyautogui.scroll(amount, px, py)
        else:
            pyautogui.scroll(amount)

        return {"ok": True, "action": "scroll", "amount": amount}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_drag(kwargs: dict) -> dict:
    """处理拖动"""
    if not _PYAUTOGUI_OK:
        return {"ok": False, "error": "pyautogui 库未安装，请执行 pip install pyautogui"}

    import pyautogui

    x1 = kwargs.get("x1", 0.3)
    y1 = kwargs.get("y1", 0.5)
    x2 = kwargs.get("x2", 0.7)
    y2 = kwargs.get("y2", 0.5)
    duration = kwargs.get("duration", 0.5)

    if not 0.1 <= duration <= 5.0:
        return {"ok": False, "error": f"duration 必须在 [0.1, 5.0] 范围内，当前: {duration}"}

    try:
        px1, py1 = _normalize_to_pixel(x1, y1)
        px2, py2 = _normalize_to_pixel(x2, y2)

        pyautogui.moveTo(px1, py1)
        pyautogui.dragTo(px2, py2, duration=duration)

        return {
            "ok": True,
            "action": "drag",
            "from": (x1, y1),
            "to": (x2, y2),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ==============================================================
# 键盘处理
# ==============================================================
KEY_MAP = {
    "a": "a", "b": "b", "c": "c", "d": "d", "e": "e", "f": "f", "g": "g",
    "h": "h", "i": "i", "j": "j", "k": "k", "l": "l", "m": "m", "n": "n",
    "o": "o", "p": "p", "q": "q", "r": "r", "s": "s", "t": "t", "u": "u",
    "v": "v", "w": "w", "x": "x", "y": "y", "z": "z",
    "0": "0", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
    "7": "7", "8": "8", "9": "9",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5", "f6": "f6",
    "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
    "enter": "enter", "esc": "esc", "tab": "tab", "backspace": "backspace",
    "delete": "delete", "insert": "insert", "space": "space",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "win",
}

MODIFIER_KEYS = {"ctrl", "alt", "shift", "win"}


def _normalize_key(key: str) -> str:
    return key.lower().strip()


def _is_valid_key(key: str) -> bool:
    return _normalize_key(key) in KEY_MAP


def _is_modifier(key: str) -> bool:
    return _normalize_key(key) in MODIFIER_KEYS


def _handle_key_press(kwargs: dict) -> dict:
    """处理按键输入"""
    if not _PYAUTOGUI_OK:
        return {"ok": False, "error": "pyautogui 库未安装，请执行 pip install pyautogui"}

    import pyautogui

    key = kwargs.get("key", "")

    if not _is_valid_key(key):
        return {"ok": False, "error": f"非法按键: {key}"}

    try:
        pyautogui.press(KEY_MAP[_normalize_key(key)])
        return {"ok": True, "action": "key_press", "key": key}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _handle_hotkey(kwargs: dict) -> dict:
    """处理组合键"""
    if not _PYAUTOGUI_OK:
        return {"ok": False, "error": "pyautogui 库未安装，请执行 pip install pyautogui"}

    import pyautogui

    keys = kwargs.get("keys", [])

    if not (2 <= len(keys) <= 5):
        return {"ok": False, "error": f"组合键数量必须在 [2, 5] 范围内，当前: {len(keys)}"}

    normalized = [_normalize_key(k) for k in keys]

    for k in normalized:
        if not _is_valid_key(k):
            return {"ok": False, "error": f"非法按键: {k}"}

    if len(set(normalized)) != len(normalized):
        return {"ok": False, "error": "组合键不能包含重复按键"}

    if not any(_is_modifier(k) for k in normalized):
        return {"ok": False, "error": "组合键必须包含至少一个修饰键 (ctrl/alt/shift/win)"}

    try:
        pyautogui.hotkey(*[KEY_MAP[k] for k in normalized])
        return {"ok": True, "action": "hotkey", "keys": normalized}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _is_chinese(text: str) -> bool:
    import re
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _handle_type_text(kwargs: dict) -> dict:
    """处理文字输入"""
    if not _PYAUTOGUI_OK:
        return {"ok": False, "error": "pyautogui 库未安装，请执行 pip install pyautogui"}

    import pyautogui

    text = kwargs.get("text", "")
    interval = kwargs.get("interval", 0.0)

    if not text:
        return {"ok": False, "error": "文本不能为空"}

    if len(text) > 1000:
        return {"ok": False, "error": f"文本长度不能超过 1000，当前: {len(text)}"}

    if not 0.0 <= interval <= 1.0:
        return {"ok": False, "error": f"interval 必须在 [0.0, 1.0] 范围内，当前: {interval}"}

    try:
        if all(ord(c) < 128 for c in text):
            # ASCII 模式：逐字符键入
            pyautogui.write(text, interval=interval)
            return {"ok": True, "input_method": "typed"}
        else:
            # 中文模式：剪贴板粘贴
            if not _PYPERCLIP_OK:
                return {"ok": False, "error": "pyperclip 库未安装，请执行 pip install pyperclip"}

            import pyperclip

            clipboard_restored = True
            original_clipboard = ""

            try:
                original_clipboard = pyperclip.paste()
            except Exception:
                clipboard_restored = False

            try:
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
            finally:
                try:
                    if original_clipboard:
                        pyperclip.copy(original_clipboard)
                except Exception:
                    clipboard_restored = False

            return {
                "ok": True,
                "input_method": "pasted",
                "clipboard_restored": clipboard_restored,
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ==============================================================
# 等待处理
# ==============================================================
def _handle_wait(kwargs: dict) -> dict:
    """处理等待"""
    duration = kwargs.get("duration", 1.0)

    if duration < 0.1 or duration > 60.0:
        return {"ok": False, "error": f"duration 必须在 [0.1, 60.0] 范围内，当前: {duration}"}

    try:
        _time.sleep(duration)
        return {"ok": True, "action": "wait", "duration": duration}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ==============================================================
# 录制处理（完整实现）
# ==============================================================

# 录制线程和录制器实例
_RECORDING_THREAD: threading.Thread | None = None
_RECORDING_STOP_EVENT: threading.Event = threading.Event()
_RECORDING_PAUSE_EVENT: threading.Event = threading.Event()
_RECORDING_RESULT: dict = {}
_FRAME_COLLECTOR: Any = None
_VIDEO_ENCODER: Any = None
_RECORDING_OVERLAY: Any = None
_AUDIO_COLLECTOR: Any = None


def _overlay_button_callback(cmd: str) -> None:
    """浮层按钮回调（在独立线程中执行，避免阻塞录制线程）"""
    threading.Thread(
        target=lambda: run(action="record_control", mode=cmd),
        name="overlay-cmd",
        daemon=True,
    ).start()


def _mux_audio_video(video_path: str, audio_path: str, output_path: str) -> None:
    """使用 ffmpeg 合成视频和音频"""
    import subprocess

    ffmpeg_path = r"F:\tools\ffmpeg-2026-06-08-git-6028720d70-essentials_build\bin\ffmpeg.exe"

    cmd = [
        ffmpeg_path, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path,
    ]

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE

    result = subprocess.run(cmd, startupinfo=startupinfo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30.0)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 合成失败，返回码: {result.returncode}")


def _recording_loop(session: dict, stop_event: threading.Event,
                    pause_event: threading.Event, result_store: dict) -> None:
    """录制循环（在后台线程中运行）

    Args:
        session: 录制会话信息
        stop_event: 停止事件
        pause_event: 暂停事件（set=暂停, clear=录制）
        result_store: 存储结果的字典（线程安全写入）
    """
    save_path = session["save_path"]
    region = session["region"]
    duration = session["duration"]
    audio_source = session["audio_source"]
    recording_id = session["recording_id"]

    screen_width, screen_height = _get_screen_resolution()

    if region is None:
        frame_size = (screen_width, screen_height)
        record_region = None
    else:
        x1 = int(region.get("x1", 0) * screen_width)
        y1 = int(region.get("y1", 0) * screen_height)
        x2 = int(region.get("x2", 1.0) * screen_width)
        y2 = int(region.get("y2", 1.0) * screen_height)
        frame_size = (x2 - x1, y2 - y1)
        record_region = {"x1": region.get("x1", 0), "y1": region.get("y1", 0),
                         "x2": region.get("x2", 1.0), "y2": region.get("y2", 1.0)}

    global _FRAME_COLLECTOR, _VIDEO_ENCODER, _RECORDING_OVERLAY

    plugin_dir = str(Path(__file__).resolve().parent)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    from recorder import FrameCollector, VideoEncoder
    from overlay import RecordingOverlay

    import cv2 as _cv2

    _FRAME_COLLECTOR = FrameCollector(frame_rate=15, region=record_region)
    if not _FRAME_COLLECTOR.start():
        result_store.update({"ok": False, "error": "帧采集器启动失败（mss 未安装？）"})
        return

    temp_video_path = save_path
    if audio_source != "none":
        temp_video_path = save_path + ".tmp_video.mp4"

    _VIDEO_ENCODER = VideoEncoder(temp_video_path, frame_rate=15, frame_size=frame_size)
    if not _VIDEO_ENCODER.start():
        _FRAME_COLLECTOR.stop()
        result_store.update({"ok": False, "error": "视频编码器启动失败，请检查 ffmpeg 路径"})
        return

    _RECORDING_OVERLAY = RecordingOverlay()
    overlay_ok = _RECORDING_OVERLAY.start(screen_width, screen_height, audio_source)
    if overlay_ok:
        _RECORDING_OVERLAY.set_callback(_overlay_button_callback)

    audio_collector = None
    audio_temp_path = None
    if audio_source != "none":
        try:
            from recorder import AudioCollector
            audio_temp_path = save_path + ".tmp_audio.wav"
            audio_collector = AudioCollector(audio_source, audio_temp_path)
            if audio_collector.start():
                global _AUDIO_COLLECTOR
                _AUDIO_COLLECTOR = audio_collector
            else:
                audio_collector = None
                audio_temp_path = None
        except Exception:
            audio_collector = None
            audio_temp_path = None

    start_time = _time.time()
    frames_written = 0
    paused_duration = 0.0
    pause_start = 0.0
    elapsed = 0.0

    try:
        while not stop_event.is_set():
            now = _time.time()
            elapsed = now - start_time - paused_duration
            if duration > 0 and elapsed >= duration:
                break

            if pause_event.is_set():
                if pause_start == 0.0:
                    pause_start = now
                    if _RECORDING_OVERLAY:
                        _RECORDING_OVERLAY.update_state("paused")
                _time.sleep(0.05)
                continue
            else:
                if pause_start > 0.0:
                    paused_duration += now - pause_start
                    pause_start = 0.0
                    if _RECORDING_OVERLAY:
                        _RECORDING_OVERLAY.update_state("recording")

            frame = _FRAME_COLLECTOR.get_frame()
            if frame is not None:
                if frame.shape[1] != frame_size[0] or frame.shape[0] != frame_size[1]:
                    frame = _cv2.resize(frame, (frame_size[0], frame_size[1]))
                if _VIDEO_ENCODER.write_frame(frame):
                    frames_written += 1
            else:
                _time.sleep(0.005)

            _time.sleep(0.001)

        elapsed = _time.time() - start_time - paused_duration

    except Exception as e:
        elapsed = _time.time() - start_time - paused_duration
        result_store.update({"ok": False, "error": f"录制过程中发生错误: {str(e)}"})
        return
    finally:
        if _FRAME_COLLECTOR:
            _FRAME_COLLECTOR.stop()
        if _VIDEO_ENCODER:
            _VIDEO_ENCODER.stop()
        if audio_collector:
            audio_collector.stop()
        if _RECORDING_OVERLAY:
            _RECORDING_OVERLAY.stop()
            _RECORDING_OVERLAY = None
        _AUDIO_COLLECTOR = None

    if audio_collector and audio_temp_path and Path(audio_temp_path).exists():
        audio_size = Path(audio_temp_path).stat().st_size
        if audio_size > 1000:
            try:
                _mux_audio_video(temp_video_path, audio_temp_path, save_path)
                if temp_video_path != save_path:
                    Path(temp_video_path).unlink(missing_ok=True)
            except Exception:
                if temp_video_path != save_path:
                    try:
                        Path(temp_video_path).rename(save_path)
                    except Exception:
                        pass
        else:
            if temp_video_path != save_path:
                try:
                    Path(temp_video_path).rename(save_path)
                except Exception:
                    pass
        Path(audio_temp_path).unlink(missing_ok=True)
    elif temp_video_path != save_path:
        try:
            Path(temp_video_path).rename(save_path)
        except Exception:
            pass

    result_store.update({
        "ok": True,
        "recording_id": recording_id,
        "saved_path": save_path,
        "elapsed": elapsed,
        "frames_written": frames_written,
    })


def _handle_record(kwargs: dict) -> dict:
    """处理录制启动"""
    global _RECORDING_SESSION, _RECORDING_THREAD, _RECORDING_STOP_EVENT
    global _RECORDING_PAUSE_EVENT, _RECORDING_RESULT

    save_path = kwargs.get("save_path")
    region = kwargs.get("region")
    duration = kwargs.get("duration", 0)
    audio_source = kwargs.get("audio_source", "none")

    if not save_path:
        return {"ok": False, "error": "save_path 为必填参数"}

    if duration < 0:
        return {"ok": False, "error": "duration 不能为负数"}

    if audio_source not in ["none", "system", "microphone", "both"]:
        return {"ok": False, "error": f"非法音频来源: {audio_source}"}

    if not _MSS_OK:
        return {"ok": False, "error": "mss 库未安装，请执行 pip install mss"}
    if not _CV2_OK:
        return {"ok": False, "error": "opencv-python 库未安装，请执行 pip install opencv-python"}
    if not _NUMPY_OK:
        return {"ok": False, "error": "numpy 库未安装，请执行 pip install numpy"}

    with _RECORDING_LOCK:
        if _RECORDING_THREAD and not _RECORDING_THREAD.is_alive():
            _RECORDING_SESSION = {}
            _RECORDING_THREAD = None

        if _RECORDING_SESSION.get("state") in ("recording", "paused"):
            return {"ok": False, "error": "已有活动录制会话，请先停止或丢弃"}

        if _RECORDING_THREAD and _RECORDING_THREAD.is_alive():
            return {"ok": False, "error": "已有录制线程在运行，请先停止或丢弃"}

        recording_id = f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        _RECORDING_SESSION = {
            "state": "recording",
            "recording_id": recording_id,
            "save_path": save_path,
            "region": region,
            "duration": duration,
            "audio_source": audio_source,
            "start_time": _time.time(),
        }

        try:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"无法创建保存目录: {str(e)}"}

        _RECORDING_STOP_EVENT.clear()
        _RECORDING_PAUSE_EVENT.clear()
        _RECORDING_RESULT.clear()
        _RECORDING_THREAD = threading.Thread(
            target=_recording_loop,
            args=(_RECORDING_SESSION, _RECORDING_STOP_EVENT,
                  _RECORDING_PAUSE_EVENT, _RECORDING_RESULT),
            name="desktop-recorder",
            daemon=True,
        )
        _RECORDING_THREAD.start()

    return {
        "ok": True,
        "recording_id": recording_id,
        "record_status": "recording",
        "elapsed": 0,
        "audio_source": audio_source,
    }


def _handle_record_control(kwargs: dict) -> dict:
    """处理录制控制"""
    mode = kwargs.get("mode", "")

    global _RECORDING_SESSION, _RECORDING_STOP_EVENT, _RECORDING_THREAD
    global _RECORDING_PAUSE_EVENT, _RECORDING_RESULT, _AUDIO_COLLECTOR

    with _RECORDING_LOCK:
        if _RECORDING_THREAD and not _RECORDING_THREAD.is_alive():
            _RECORDING_SESSION = {}
            _RECORDING_THREAD = None

        if not _RECORDING_SESSION:
            return {"ok": False, "error": "无活动录制会话"}

        current_state = _RECORDING_SESSION.get("state")

        if mode == "pause":
            if current_state != "recording":
                return {"ok": False, "error": f"当前状态 {current_state} 无法暂停"}
            _RECORDING_PAUSE_EVENT.set()
            if _AUDIO_COLLECTOR:
                _AUDIO_COLLECTOR.pause()
            _RECORDING_SESSION["state"] = "paused"
            return {"ok": True, "record_status": "paused"}

        elif mode == "resume":
            if current_state != "paused":
                return {"ok": False, "error": f"当前状态 {current_state} 无法恢复"}
            _RECORDING_PAUSE_EVENT.clear()
            if _AUDIO_COLLECTOR:
                _AUDIO_COLLECTOR.resume()
            _RECORDING_SESSION["state"] = "recording"
            return {"ok": True, "record_status": "recording"}

        elif mode == "stop":
            if current_state not in ["recording", "paused"]:
                return {"ok": False, "error": f"当前状态 {current_state} 无法停止"}

            _RECORDING_STOP_EVENT.set()
            _RECORDING_PAUSE_EVENT.clear()

            if _RECORDING_THREAD and _RECORDING_THREAD.is_alive():
                _RECORDING_THREAD.join(timeout=10.0)

            saved_path = _RECORDING_SESSION.get("save_path", "")
            elapsed = _RECORDING_RESULT.get("elapsed", 0)
            frames_written = _RECORDING_RESULT.get("frames_written", 0)
            ok = _RECORDING_RESULT.get("ok", True)
            error = _RECORDING_RESULT.get("error")

            _RECORDING_SESSION = {}
            _RECORDING_RESULT.clear()

            result = {"ok": ok, "record_status": "stopped",
                      "saved_path": saved_path, "elapsed": elapsed,
                      "frames_written": frames_written}
            if error:
                result["error"] = error
            return result

        elif mode == "discard":
            if current_state not in ["recording", "paused"]:
                return {"ok": False, "error": f"当前状态 {current_state} 无法丢弃"}

            _RECORDING_STOP_EVENT.set()
            _RECORDING_PAUSE_EVENT.clear()

            if _RECORDING_THREAD and _RECORDING_THREAD.is_alive():
                _RECORDING_THREAD.join(timeout=5.0)

            saved_path = _RECORDING_SESSION.get("save_path", "")
            if saved_path and Path(saved_path).exists():
                try:
                    Path(saved_path).unlink()
                except Exception:
                    pass

            _RECORDING_SESSION = {}
            _RECORDING_RESULT.clear()
            return {"ok": True, "record_status": "discarded"}

        else:
            return {"ok": False, "error": f"非法控制模式: {mode}"}


# ==============================================================
# run() —— 插件入口
# ==============================================================
def run(**kwargs) -> dict:
    """供 LLM 调用的主入口

    Args:
        **kwargs: 由 tool.yaml 的 parameters 定义

    Returns:
        {"ok": bool, "data": any, "error": str?}
    """
    # 延迟导入
    _import_modules()

    action = kwargs.get("action", "")

    handlers = {
        "screenshot": _handle_screenshot,
        "click": _handle_click,
        "double_click": _handle_double_click,
        "right_click": _handle_right_click,
        "scroll": _handle_scroll,
        "drag": _handle_drag,
        "wait": _handle_wait,
        "key_press": _handle_key_press,
        "hotkey": _handle_hotkey,
        "type_text": _handle_type_text,
        "record": _handle_record,
        "record_control": _handle_record_control,
    }

    handler = handlers.get(action)
    if not handler:
        return {"ok": False, "error": f"未知 action: {action}，可用：{list(handlers.keys())}"}

    try:
        return handler(kwargs)
    except Exception as e:
        import traceback
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}