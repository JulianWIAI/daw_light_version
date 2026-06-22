"""
core/telemetry_band_widget.py

PySide6 QWidget: 7-band frequency bar graph.
Receives pre-computed band values from TelemetryFrame.bands (C++ DSP).
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPainter, QPen, QColor, QFont
from PySide6.QtWidgets import QWidget

_W, _H    = 300, 150
_BG       = QColor("#0a0a14")
_GRID_COL = QColor("#1a1a2e")
_TEXT_COL = QColor("#88aacc")

_BANDS = [
    ("Sub\nBass", QColor("#6644ff")),
    ("Bass",      QColor("#4488ff")),
    ("Lo\nMid",   QColor("#44ccff")),
    ("Mid",       QColor("#44ffaa")),
    ("Hi\nMid",   QColor("#aaff44")),
    ("High",      QColor("#ffcc44")),
    ("Brill.",    QColor("#ff6644")),
]


class TelemetryBandWidget(QWidget):
    """7-band frequency bar graph, updated from TelemetryFrame.bands."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(_W, _H)
        self._bands = np.zeros(7, dtype=np.float32)

    def update_frame(self, frame) -> None:
        """Store band values and schedule a repaint."""
        self._bands = np.asarray(frame.bands, dtype=np.float32)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)

        painter.fillRect(0, 0, _W, _H, _BG)

        n        = len(_BANDS)
        bw       = _W // n
        pad      = 3
        label_h  = 20
        bar_area = _H - label_h

        for i, (label, colour) in enumerate(_BANDS):
            x0    = i * bw + pad
            x1    = (i + 1) * bw - pad
            w     = x1 - x0

            # Background track
            painter.fillRect(x0, 0, w, bar_area, _GRID_COL)

            # Bar — grows from bottom up
            val   = float(self._bands[i])
            bar_h = int(val * (bar_area - 4))
            if bar_h > 0:
                painter.fillRect(x0, bar_area - bar_h, w, bar_h, colour)

            # Percentage text above bar
            painter.setPen(QPen(colour, 1))
            painter.drawText(x0, 12, w, 12, Qt.AlignHCenter, f"{val:.0%}")

            # Label below bar
            painter.setPen(QPen(_TEXT_COL, 1))
            painter.drawText(x0, bar_area, w, label_h,
                             Qt.AlignHCenter | Qt.AlignVCenter,
                             label.split("\n")[0])

        painter.end()
