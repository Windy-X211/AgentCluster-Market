"""keyboard.py —— 键盘输入模块

使用 pyautogui 进行键盘控制，支持：
- 单键输入
- 组合键输入
- 文字输入（含中文适配，剪贴板粘贴方案）
"""
from __future__ import annotations

import re
import time
from typing import Any

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


# ==============================================================
# 按键映射表
# ==============================================================
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
    """标准化按键名（大小写不敏感）

    Args:
        key: 按键名

    Returns:
        标准化后的按键名
    """
    return key.lower().strip()


def is_valid_key(key: str) -> bool:
    """检查按键名是否合法

    Args:
        key: 按键名

    Returns:
        是否合法
    """
    return normalize_key(key) in KEY_MAP


def is_modifier(key: str) -> bool:
    """检查是否为修饰键

    Args:
        key: 按键名

    Returns:
        是否为修饰键
    """
    return normalize_key(key) in MODIFIER_KEYS


def to_pyautogui_key(key: str) -> str:
    """转换为 pyautogui 按键名

    Args:
        key: 标准化按键名

    Returns:
        pyautogui 按键名
    """
    return KEY_MAP[normalize_key(key)]


def press_key(key: str) -> dict:
    """执行单键按下-释放事件

    Args:
        key: 按键名

    Returns:
        {"ok": bool, "action": str, "key": str}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    if not is_valid_key(key):
        raise ValueError(f"非法按键: {key}")

    pyautogui.press(to_pyautogui_key(key))
    return {"ok": True, "action": "key_press", "key": key}


def press_hotkey(keys: list[str]) -> dict:
    """执行多键组合事件

    Args:
        keys: 按键名列表，长度 [2, 5]，必须含修饰键

    Returns:
        {"ok": bool, "action": str, "keys": list[str]}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    if not (2 <= len(keys) <= 5):
        raise ValueError(f"组合键数量必须在 [2, 5] 范围内，当前: {len(keys)}")

    normalized = [normalize_key(k) for k in keys]

    # 检查合法性
    for k in normalized:
        if k not in KEY_MAP:
            raise ValueError(f"非法按键: {k}")

    # 检查重复
    if len(set(normalized)) != len(normalized):
        raise ValueError("组合键不能包含重复按键")

    # 检查修饰键
    if not any(is_modifier(k) for k in normalized):
        raise ValueError("组合键必须包含至少一个修饰键 (ctrl/alt/shift/win)")

    # 修饰键先按下、后释放
    pyautogui.hotkey(*[KEY_MAP[k] for k in normalized])

    return {"ok": True, "action": "hotkey", "keys": normalized}


def is_chinese(text: str) -> bool:
    """检测文本是否包含中文

    Args:
        text: 待检测文本

    Returns:
        是否包含中文
    """
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def type_ascii(text: str, interval: float = 0.0) -> dict:
    """逐字符键入 ASCII 文本

    Args:
        text: 待输入文本，长度 <= 1000
        interval: 字符间隔 [0.0, 1.0]

    Returns:
        {"ok": bool, "input_method": str}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    if not text:
        raise ValueError("文本不能为空")

    if len(text) > 1000:
        raise ValueError(f"文本长度不能超过 1000，当前: {len(text)}")

    if not 0.0 <= interval <= 1.0:
        raise ValueError(f"interval 必须在 [0.0, 1.0] 范围内，当前: {interval}")

    # 检查是否全 ASCII
    if not all(ord(c) < 128 for c in text):
        raise ValueError("type_ascii 仅支持纯 ASCII 文本")

    pyautogui.write(text, interval=interval)
    return {"ok": True, "input_method": "typed"}


def type_chinese(text: str) -> dict:
    """使用剪贴板粘贴方式输入中文

    Args:
        text: 待输入文本，长度 <= 1000

    Returns:
        {"ok": bool, "input_method": str, "clipboard_restored": bool}
    """
    if not _PYAUTOGUI_OK:
        raise RuntimeError("pyautogui 库未安装，请执行 pip install pyautogui")

    if not _PYPERCLIP_OK:
        raise RuntimeError("pyperclip 库未安装，请执行 pip install pyperclip")

    if not text:
        raise ValueError("文本不能为空")

    if len(text) > 1000:
        raise ValueError(f"文本长度不能超过 1000，当前: {len(text)}")

    clipboard_restored = True
    original_clipboard = ""

    try:
        # 1. 保存当前剪贴板
        original_clipboard = pyperclip.paste()
    except Exception:
        clipboard_restored = False

    try:
        # 2. 写入文本到剪贴板
        pyperclip.copy(text)

        # 3. 发送 Ctrl+V 粘贴
        pyautogui.hotkey("ctrl", "v")

    finally:
        # 4. 恢复原剪贴板
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


def type_text(text: str, interval: float = 0.0) -> dict:
    """文字输入分发

    根据文本内容选择输入方式：
    - 全 ASCII → typed（逐字符键入）
    - 含中文 → pasted（剪贴板粘贴）
    - 混排 → pasted（剪贴板粘贴）

    Args:
        text: 待输入文本，长度 <= 1000
        interval: 字符间隔（仅 ASCII 模式有效）

    Returns:
        {"ok": bool, "input_method": str, "clipboard_restored": bool?}
    """
    if not text:
        raise ValueError("文本不能为空")

    if len(text) > 1000:
        raise ValueError(f"文本长度不能超过 1000，当前: {len(text)}")

    if all(ord(c) < 128 for c in text):
        return type_ascii(text, interval)
    else:
        return type_chinese(text)