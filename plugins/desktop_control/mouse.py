"""mouse.py —— 鼠标交互模块

使用 pyautogui 进行鼠标控制，支持：
- 单击、双击、右键
- 滚动、拖动
- 坐标换算（归一化坐标转像素坐标）
"""
from __future__ import annotations

import time
from typing import Any

try:
    import pyautogui
    _PYAUTOGUI_OK = True
except ImportError:
    _PYAUTOGUI_OK = False


def normalize_to_pixel(x: float, y: float, screen_width: int, screen_height: int) -> tuple[int, int]:
    """归一化坐标转像素坐标

    Args:
        x: 归一化 X 坐标 [0.0, 1.0]
        y: 归一化 Y 坐标 [0.0, 1.0]
        screen_width: 屏幕宽度
        screen_height: 屏幕高度

    Returns:
        (pixel_x, pixel_y)
    """
    if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
        raise ValueError(f"坐标越界: x={x}, y={y}，必须在 [0.0, 1.0] 范围内")

    px = round(x * screen_width)
    py = round(y * screen_height)
    return px, py


def click(x: float, y: float, screen_width: int, screen_height: int) -> dict:
    """执行鼠标左键单击

    Args:
        x: 归一化 X 坐标 [0.0, 1.0]
        y: 归一化 Y 坐标 [0.0, 1.0]
        screen_width: 屏幕宽度
        screen_height: 屏幕高度

    Returns:
        {"ok": bool, "action": str, "x": float, "y": float}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    px, py = normalize_to_pixel(x, y, screen_width, screen_height)
    pyautogui.click(px, py)
    time.sleep(0.1)  # 操作后等待 100ms
    return {"ok": True, "action": "click", "x": x, "y": y}


def double_click(x: float, y: float, screen_width: int, screen_height: int) -> dict:
    """执行鼠标左键双击

    Args:
        x: 归一化 X 坐标 [0.0, 1.0]
        y: 归一化 Y 坐标 [0.0, 1.0]
        screen_width: 屏幕宽度
        screen_height: 屏幕高度

    Returns:
        {"ok": bool, "action": str, "x": float, "y": float}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    px, py = normalize_to_pixel(x, y, screen_width, screen_height)
    pyautogui.doubleClick(px, py)
    return {"ok": True, "action": "double_click", "x": x, "y": y}


def right_click(x: float, y: float, screen_width: int, screen_height: int) -> dict:
    """执行鼠标右键单击

    Args:
        x: 归一化 X 坐标 [0.0, 1.0]
        y: 归一化 Y 坐标 [0.0, 1.0]
        screen_width: 屏幕宽度
        screen_height: 屏幕高度

    Returns:
        {"ok": bool, "action": str, "x": float, "y": float}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    px, py = normalize_to_pixel(x, y, screen_width, screen_height)
    pyautogui.rightClick(px, py)
    return {"ok": True, "action": "right_click", "x": x, "y": y}


def scroll(amount: int, x: float | None = None, y: float | None = None,
           screen_width: int = 0, screen_height: int = 0) -> dict:
    """执行鼠标滚动

    Args:
        amount: 滚动量，正数向下，负数向上，|amount| <= 20
        x: 可选，归一化 X 坐标
        y: 可选，归一化 Y 坐标
        screen_width: 屏幕宽度（x/y 提供时需要）
        screen_height: 屏幕高度（x/y 提供时需要）

    Returns:
        {"ok": bool, "action": str, "amount": int}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    if amount == 0:
        raise ValueError("滚动量不能为 0")

    if abs(amount) > 20:
        raise ValueError(f"滚动量 |amount| 必须 <= 20，当前: {amount}")

    if x is not None and y is not None:
        px, py = normalize_to_pixel(x, y, screen_width, screen_height)
        pyautogui.scroll(amount, px, py)
    else:
        pyautogui.scroll(amount)

    return {"ok": True, "action": "scroll", "amount": amount}


def drag(x1: float, y1: float, x2: float, y2: float,
         screen_width: int, screen_height: int,
         duration: float = 0.5) -> dict:
    """执行鼠标拖动

    Args:
        x1: 起始归一化 X 坐标
        y1: 起始归一化 Y 坐标
        x2: 结束归一化 X 坐标
        y2: 结束归一化 Y 坐标
        screen_width: 屏幕宽度
        screen_height: 屏幕高度
        duration: 拖动时长 [0.1, 5.0]

    Returns:
        {"ok": bool, "action": str, "from": (x1, y1), "to": (x2, y2)}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    if not 0.1 <= duration <= 5.0:
        raise ValueError(f"duration 必须在 [0.1, 5.0] 范围内，当前: {duration}")

    px1, py1 = normalize_to_pixel(x1, y1, screen_width, screen_height)
    px2, py2 = normalize_to_pixel(x2, y2, screen_width, screen_height)

    pyautogui.moveTo(px1, py1)
    pyautogui.dragTo(px2, py2, duration=duration)

    return {"ok": True, "action": "drag", "from": (x1, y1), "to": (x2, y2)}