# coding=utf-8
"""试听预览窗口：和插件里同一套字幕画法，逐字高亮 + 进度条。"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt5.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .subtitle import paint_capsule


class SubtitlePreview(QWidget):
    def __init__(self, segments: List[Dict[str, object]], duration_ms: int,
                 engine=None, title: str = "桌宠配音 · 字幕同步预览", parent=None):
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._segments = segments or []
        self._duration = max(1, int(duration_ms))
        self._engine = engine
        self._title = title
        self._t0 = time.monotonic()
        self.setFixedSize(620, 250)
        self.setWindowTitle("TTS 字幕预览")
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)
        self._timer.start()
        self._move_to_corner()

    def _move_to_corner(self):
        try:
            from PyQt5.QtWidgets import QApplication
            scr = QApplication.primaryScreen().availableGeometry()
            self.move(scr.center().x() - self.width() // 2, scr.bottom() - self.height() - 80)
        except Exception:
            pass

    def closeEvent(self, e):
        self._timer.stop()
        super().closeEvent(e)

    # ------------------------------------------------------------------
    def _clock(self) -> float:
        if self._engine is not None:
            c = self._engine.clock_ms()
            return c if c >= 0 else 0.0
        return (time.monotonic() - self._t0) * 1000.0

    def _current(self):
        clock = self._clock()
        for seg in self._segments:
            s = float(seg.get("start_ms", 0))
            e = float(seg.get("end_ms", s))
            if s <= clock < e:
                local = clock - s
                spoken = 0
                for ch in seg.get("chars") or []:
                    if float(ch.get("t1", 0)) <= local:
                        spoken += 1
                    else:
                        break
                return seg, spoken, clock
        if self._segments and clock >= float(self._segments[-1].get("end_ms", 0)):
            last = self._segments[-1]
            return last, len(str(last.get("text", "") or "")), clock
        return (self._segments[0], 0, clock) if self._segments else (None, 0, clock)

    # ------------------------------------------------------------------
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(26, 28, 38, 255)))
        p.drawRoundedRect(QRectF(0, 0, w, h), 16, 16)

        # 顶部信息
        f = QFont()
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(QPen(QColor(180, 186, 200, 220)))
        p.drawText(QRectF(18, 12, w - 36, 22), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        seg, spoken, clock = self._current()
        status = ""
        if seg:
            status = "第 %d/%d 句 · %s" % (
                int(seg.get("i", 0)) + 1,
                len(self._segments),
                str(seg.get("aligned", "estimated")),
            )
        p.setPen(QPen(QColor(140, 146, 160, 200)))
        p.drawText(QRectF(18, 12, w - 36, 22), Qt.AlignRight | Qt.AlignVCenter, status)

        # 语音文字（整句，带高亮）
        if seg:
            text = str(seg.get("text", ""))
            font = QFont()
            font.setPointSize(11)
            paint_capsule(
                p, QRectF(10, 30, w - 20, h - 64), text, spoken, font,
                max_chars=42, max_width=w - 44,
            )

        # 进度条
        bar = QRectF(18, h - 28, w - 36, 6)
        p.setBrush(QBrush(QColor(60, 64, 78, 255)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(bar, 3, 3)
        prog = max(0.0, min(1.0, clock / self._duration))
        p.setBrush(QBrush(QColor(255, 210, 74, 230)))
        p.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * prog, bar.height()), 3, 3)

        # 时间
        f2 = QFont()
        f2.setPointSize(8)
        p.setFont(f2)
        p.setPen(QPen(QColor(150, 156, 170, 200)))
        p.drawText(
            QRectF(18, h - 52, w - 36, 18),
            Qt.AlignRight | Qt.AlignVCenter,
            "%d.%02ds / %d.%02ds" % (clock // 1000, int(clock % 1000) // 10, self._duration // 1000, int(self._duration % 1000) // 10),
        )
        p.end()
