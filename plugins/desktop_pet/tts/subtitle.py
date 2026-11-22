# coding=utf-8
"""字幕渲染：与 pet_window 现有底部胶囊同款，额外支持逐字高亮。"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPen

BG_COLOR = QColor(16, 16, 20, 176)
TEXT_COLOR = QColor(255, 255, 255, 246)
HILITE_COLOR = QColor(255, 210, 74, 255)


def wrap_cjk(text: str, fm: QFontMetrics, max_width: float) -> List[Tuple[str, int]]:
    """按像素宽度折行，返回 [(行文本, 该行首字符在全文中的下标)]。"""
    out: List[Tuple[str, int]] = []
    if not text:
        return out
    line = ""
    line_start = 0
    for i, ch in enumerate(text):
        cand = line + ch
        if fm.horizontalAdvance(cand) <= max_width or not line:
            line = cand
            continue
        out.append((line, line_start))
        line_start = i
        line = ch
    if line:
        out.append((line, line_start))
    return out


def paint_capsule(
    painter: QPainter,
    area: QRectF,
    text: str,
    spoken_chars: Optional[int],
    font: QFont,
    max_chars: int = 42,
    max_width: float = 360.0,
) -> QRectF:
    """在 area 底部画字幕胶囊；返回实际绘制矩形。"""
    text = " ".join(str(text or "").split())
    if not text:
        return QRectF()

    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    if spoken_chars is not None:
        spoken_chars = max(0, min(int(spoken_chars), len(text)))

    painter.save()
    painter.setFont(font)
    fm = painter.fontMetrics()

    lines = wrap_cjk(text, fm, max_width - 26.0)[:3]
    if not lines:
        painter.restore()
        return QRectF()

    lineh = fm.height()
    tw = max(fm.horizontalAdvance(l) for l, _ in lines)
    bw = max(96.0, min(area.width() * 0.92, tw + 26.0))
    bh = len(lines) * lineh + 15.0
    x = area.left() + (area.width() - bw) * 0.5
    y = area.bottom() - bh - 8.0

    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(BG_COLOR))
    painter.drawRoundedRect(QRectF(x, y, bw, bh), 8.0, 8.0)

    ty = y + 7.0 + fm.ascent()
    for line, start_idx in lines:
        lw = fm.horizontalAdvance(line)
        lx = x + (bw - lw) * 0.5

        painter.setPen(QPen(TEXT_COLOR))
        painter.drawText(QPointF(lx, ty), line)

        if spoken_chars is not None and spoken_chars > start_idx:
            n_spoken = min(len(line), spoken_chars - start_idx)
            if n_spoken > 0:
                painter.setPen(QPen(HILITE_COLOR))
                painter.drawText(QPointF(lx, ty), line[:n_spoken])

        ty += lineh

    painter.restore()
    return QRectF(x, y, bw, bh)


def render_frame(
    width: int,
    height: int,
    text: str,
    spoken_chars: Optional[int],
    title: str = "",
    footer: str = "",
    max_chars: int = 42,
    bg: Optional[QColor] = None,
) -> QImage:
    """离屏渲染一帧预览图（用于无窗口环境下检查字幕排版）。"""
    img = QImage(int(width), int(height), QImage.Format_ARGB32)
    img.fill(bg or QColor(28, 30, 40, 255))

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    # 背景柔光
    grad_rect = QRectF(0, 0, width, height)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(46, 50, 68, 255)))
    p.drawRoundedRect(grad_rect.adjusted(8, 8, -8, -8), 14, 14)

    if title:
        f = QFont()
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(QPen(QColor(190, 196, 210, 220)))
        p.drawText(QRectF(20, 14, width - 40, 24), Qt.AlignLeft | Qt.AlignVCenter, title)
    if footer:
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        p.setPen(QPen(QColor(150, 156, 170, 200)))
        p.drawText(QRectF(20, height - 30, width - 40, 20), Qt.AlignLeft | Qt.AlignVCenter, footer)

    font = QFont()
    font.setPointSize(11)
    paint_capsule(
        p,
        QRectF(12, 34, width - 24, height - 60),
        text,
        spoken_chars,
        font,
        max_chars=max_chars,
        max_width=width - 44,
    )
    p.end()
    return img
