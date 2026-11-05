"""keymap.py —— 按键映射表

定义标准化按键名到 pyautogui 按键名的映射。
"""
from __future__ import annotations

KEY_MAP: dict[str, str] = {
    # 字母
    "a": "a", "b": "b", "c": "c", "d": "d", "e": "e", "f": "f", "g": "g",
    "h": "h", "i": "i", "j": "j", "k": "k", "l": "l", "m": "m", "n": "n",
    "o": "o", "p": "p", "q": "q", "r": "r", "s": "s", "t": "t", "u": "u",
    "v": "v", "w": "w", "x": "x", "y": "y", "z": "z",
    # 数字
    "0": "0", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
    "7": "7", "8": "8", "9": "9",
    # 功能键
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5", "f6": "f6",
    "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
    # 控制键
    "enter": "enter", "esc": "esc", "tab": "tab", "backspace": "backspace",
    "delete": "delete", "insert": "insert", "space": "space",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
    "capslock": "capslock", "numlock": "numlock", "scrolllock": "scrolllock",
    # 方向键
    "up": "up", "down": "down", "left": "left", "right": "right",
    # 修饰键
    "ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "win",
    "command": "command",
    # 标点符号
    "period": ".", "comma": ",", "semicolon": ";", "slash": "/",
    "backslash": "\\", "equals": "=", "minus": "-", "plus": "+",
    "bracketleft": "[", "bracketright": "]", "quote": "'",
}

MODIFIER_KEYS = {"ctrl", "alt", "shift", "win", "command"}


def normalize_key(key: str) -> str:
    """标准化按键名（大小写不敏感）"""
    return key.lower().strip()


def is_valid_key(key: str) -> bool:
    """检查按键名是否合法"""
    return normalize_key(key) in KEY_MAP


def is_modifier(key: str) -> bool:
    """检查是否为修饰键"""
    return normalize_key(key) in MODIFIER_KEYS


def to_pyautogui_key(key: str) -> str:
    """转换为 pyautogui 按键名"""
    return KEY_MAP[normalize_key(key)]