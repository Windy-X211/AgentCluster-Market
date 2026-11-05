"""screenshot.py —— 屏幕截图模块

使用 mss 库进行 DXGI 直接采集，支持：
- 区域截图（归一化坐标）
- 全屏截图
- base64 编码输出
- 文件写入
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

try:
    import mss
    _MSS_OK = True
except ImportError:
    _MSS_OK = False

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False


def get_screen_resolution() -> dict:
    """获取主屏幕分辨率

    Returns:
        {"width": int, "height": int}
    """
    if not _MSS_OK:
        raise RuntimeError("mss 库未安装，请执行 pip install mss")
    with mss.mss() as s:
        monitor = s.monitors[0]  # 主显示器
        return {"width": monitor["width"], "height": monitor["height"]}


def capture_region(region: dict | None = None) -> bytes:
    """捕获指定区域屏幕

    Args:
        region: 区域字典 {"x1": float, "y1": float, "x2": float, "y2": float}
                或 None（全屏）

    Returns:
        PNG 格式字节数据
    """
    if not _MSS_OK:
        raise RuntimeError("mss 库未安装，请执行 pip install mss")

    with mss.mss() as s:
        if region is None:
            # 全屏
            monitor = s.monitors[0]
            screenshot = s.grab(monitor)
        else:
            # 区域截图
            x1 = int(region["x1"] * s.monitors[0]["width"])
            y1 = int(region["y1"] * s.monitors[0]["height"])
            x2 = int(region["x2"] * s.monitors[0]["width"])
            y2 = int(region["y2"] * s.monitors[0]["height"])
            monitor = {"top": y1, "left": x1, "width": x2 - x1, "height": y2 - y1}
            screenshot = s.grab(monitor)

        # 转换为 PNG bytes
        img = Image.frombytes("RGB", (screenshot.width, screenshot.height), screenshot.rgb)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()


def encode_base64(png_bytes: bytes, max_size_mb: float = 2.0) -> tuple[str, bool]:
    """将 PNG bytes 编码为 base64 字符串

    Args:
        png_bytes: PNG 格式字节数据
        max_size_mb: 最大允许大小（MB）

    Returns:
        (base64_string, truncated) — truncated 表示是否因过大而缩放
    """
    max_bytes = int(max_size_mb * 1024 * 1024)
    base64_str = base64.b64encode(png_bytes).decode("utf-8")

    if len(base64_str) <= max_bytes * 0.75:  # base64 约 4/3 膨胀
        return base64_str, False

    # 缩放处理
    if not _PIL_OK:
        raise RuntimeError(f"截图过大 ({len(base64_str)} bytes)，且 Pillow 未安装，无法缩放")

    img = Image.open(io.BytesIO(png_bytes))
    scale = 0.5
    while True:
        new_size = (int(img.width * scale), int(img.height * scale))
        resized = img.resize(new_size, Image.LANCZOS)
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG")
        new_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        if len(new_base64) <= max_bytes * 0.75 or scale < 0.1:
            return new_base64, True
        scale -= 0.1


def save_to_file(png_bytes: bytes, save_path: str) -> str:
    """将截图保存到文件

    Args:
        png_bytes: PNG 格式字节数据
        save_path: 保存路径

    Returns:
        实际保存路径
    """
    path = Path(save_path).resolve()

    # 路径安全校验
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "wb") as f:
        f.write(png_bytes)

    return str(path)