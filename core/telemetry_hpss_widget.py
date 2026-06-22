"""
core/telemetry_hpss_widget.py

PySide6 QWidget: mirrored Harmonic / Percussive energy timeline.
Receives H/P ratios from TelemetryFrame.harmonic / .percussive (C++ DSP).
Python maintains a rolling display history of scalar values only.
"""

from __future__ import annotations

from collections import deque
from typing import Deque

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import QWidget

_W, _H    = 300, 130
_HISTORY  = 80
_BG       = QColor("#0a0a14")
_HARM_COL = QColor("#44aaff")
_PERC_COL = QColor("#ff6644")
_GRID_COL = QColor("#1a1a2e")
_TEXT_COL = QColor("#88aacc")
_MID_COL  = QColor("#2a2a3e")


class TelemetryHpssWidget(QWidget):
    """Mirrored H (top) / P (bottom) timeline, updated from TelemetryFrame."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(_W, _H)
        self._harm_hist: Deque[float] = deque(maxlen=_HISTORY)
        self._perc_hist: Deque[float] = deque(maxlen=_HISTORY)

    def update_frame(self, frame) -> None:
        """Append H/P values to rolling histories and schedule a repaint."""
        self._harm_hist.append(float(frame.harmonic))
        self._perc_hist.append(float(frame.percussive))
        self.update()

    def _draw_curve(self, painter: QPainter, history: Deque[float],
                    mirror: bool, colour: QColor) -> None:
        """Draw a timeline curve from a rolling history of scalar values."""
        vals = list(history)
        if len(vals) < 2:
            return
        mid  = _H // 2
        step = max(1, _W // len(vals))
        painter.setPen(QPen(colour, 2))
        prev_x = prev_y = None
        for i, v in enumerate(vals):
            x   = i * step
            amp = int(v * (mid - 6))
            y   = (mid - amp) if not mirror else (mid + amp)
            if prev_x is not None:
                painter.drawLine(prev_x, prev_y, x, y)
            prev_x, prev_y = x, y

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.fillRect(0, 0, _W, _H, _BG)

        mid = _H // 2

        # Grid
        painter.setPen(QPen(_GRID_COL, 1))
        for yg in (mid // 2, mid + mid // 2):
            painter.drawLine(0, yg, _W, yg)

        # Centre separator
        painter.setPen(QPen(_MID_COL, 2))
        painter.drawLine(0, mid, _W, mid)

        # H / P curves
        self._draw_curve(painter, self._harm_hist, mirror=False, colour=_HARM_COL)
        self._draw_curve(painter, self._perc_hist, mirror=True,  colour=_PERC_COL)

        # Labels
        h_val = self._harm_hist[-1] if self._harm_hist else 0.0
        p_val = self._perc_hist[-1] if self._perc_hist else 0.0
        painter.setPen(QPen(_HARM_COL, 1))
        painter.drawText(2, 2, _W - 4, 14, Qt.AlignLeft,  f"H")
        painter.drawText(2, 2, _W - 4, 14, Qt.AlignRight, f"{h_val:.0%}")
        painter.setPen(QPen(_PERC_COL, 1))
        painter.drawText(2, _H - 16, _W - 4, 14, Qt.AlignLeft,  f"P")
        painter.drawText(2, _H - 16, _W - 4, 14, Qt.AlignRight, f"{p_val:.0%}")

        painter.end()
