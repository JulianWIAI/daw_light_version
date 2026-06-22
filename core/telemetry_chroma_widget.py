"""
core/telemetry_chroma_widget.py

PySide6 QWidget: 12-bin pitch-class chroma histogram.
Receives pre-computed chroma values from TelemetryFrame.chroma (C++ DSP).
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import QWidget

_W, _H    = 300, 150
_BG       = QColor("#0a0a14")
_GRID_COL = QColor("#1a1a2e")
_TEXT_COL = QColor("#88aacc")

_NOTE_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
_NOTE_COLS  = [
    QColor("#ff4444"), QColor("#ff7744"), QColor("#ffaa44"), QColor("#ffdd44"),
    QColor("#bbff44"), QColor("#44ff88"), QColor("#44ffcc"), QColor("#44ccff"),
    QColor("#4488ff"), QColor("#8844ff"), QColor("#cc44ff"), QColor("#ff44cc"),
]


class TelemetryChromaWidget(QWidget):
    """12-bin pitch-class histogram, updated from TelemetryFrame.chroma."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(_W, _H)
        self._chroma = np.zeros(12, dtype=np.float32)

    def update_frame(self, frame) -> None:
        """Store chroma values and schedule a repaint."""
        self._chroma = np.asarray(frame.chroma, dtype=np.float32)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)

        painter.fillRect(0, 0, _W, _H, _BG)

        n        = 12
        bw       = _W // n
        pad      = 2
        label_h  = 16
        top_h    = 14   # reserved for dominant-note label
        bar_area = _H - label_h - top_h

        max_val  = float(self._chroma.max()) or 1.0
        dom      = int(np.argmax(self._chroma))

        # Dominant note label
        painter.setPen(QPen(_NOTE_COLS[dom], 1))
        painter.drawText(0, 0, _W, top_h,
                         Qt.AlignHCenter | Qt.AlignVCenter,
                         f"Dominant: {_NOTE_NAMES[dom]}")

        for i, (name, colour) in enumerate(zip(_NOTE_NAMES, _NOTE_COLS)):
            x0 = i * bw + pad
            w  = bw - 2 * pad

            # Background track
            painter.fillRect(x0, top_h, w, bar_area, _GRID_COL)

            # Bar
            val   = float(self._chroma[i]) / max_val
            bar_h = int(val * (bar_area - 4))
            if bar_h > 0:
                y0 = top_h + bar_area - bar_h
                painter.fillRect(x0, y0, w, bar_h, colour)

            # Percentage above bar (only when tall enough)
            if val > 0.08:
                painter.setPen(QPen(colour, 1))
                painter.drawText(x0, top_h + bar_area - bar_h - 1,
                                 w, 12, Qt.AlignHCenter, f"{val:.0%}")

            # Note name below bar
            painter.setPen(QPen(_TEXT_COL, 1))
            painter.drawText(x0, top_h + bar_area, w, label_h,
                             Qt.AlignHCenter | Qt.AlignVCenter, name)

        painter.end()
