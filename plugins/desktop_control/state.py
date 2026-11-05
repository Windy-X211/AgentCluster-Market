"""state.py —— 全局状态管理

管理屏幕分辨率和录制会话状态。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

# 屏幕分辨率（延迟初始化）
_SCREEN_WIDTH: int = 0
_SCREEN_HEIGHT: int = 0
_SCREEN_LOCK = threading.Lock()

# 录制会话状态
_RECORDING_SESSION: dict = {}
_RECORDING_LOCK = threading.Lock()


def get_screen_resolution() -> tuple[int, int]:
    """获取屏幕分辨率

    Returns:
        (width, height)
    """
    with _SCREEN_LOCK:
        return _SCREEN_WIDTH, _SCREEN_HEIGHT


def set_screen_resolution(width: int, height: int):
    """设置屏幕分辨率

    Args:
        width: 屏幕宽度
        height: 屏幕高度
    """
    with _SCREEN_LOCK:
        global _SCREEN_WIDTH, _SCREEN_HEIGHT
        _SCREEN_WIDTH = width
        _SCREEN_HEIGHT = height


def get_recording_session() -> dict:
    """获取当前录制会话

    Returns:
        录制会话字典
    """
    with _RECORDING_LOCK:
        return _RECORDING_SESSION.copy()


def set_recording_session(session: dict):
    """设置录制会话

    Args:
        session: 录制会话字典
    """
    with _RECORDING_LOCK:
        global _RECORDING_SESSION
        _RECORDING_SESSION = session


def clear_recording_session():
    """清除录制会话"""
    with _RECORDING_LOCK:
        global _RECORDING_SESSION
        _RECORDING_SESSION = {}


def generate_recording_id() -> str:
    """生成录制 ID

    Returns:
        格式: rec_YYYYMMDD_HHMMSS
    """
    return f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"