"""
core/telemetry_waterfall_widget.py

PySide6 QWidget: scrolling chromagram waterfall (heatmap).
Uses a QImage pixel buffer updated with numpy for efficient pixel manipulation.

On each frame: shift the image left by COL_W pixels (numpy roll), write the
new chroma column on the right.  The entire image is drawn in one
painter.drawImage() call — no per-cell canvas operations.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor, QImage
from PySide6.QtWidgets import QWidget

_N_ROWS  = 12
_N_COLS  = 100
_COL_W   = 4    # pixels per waterfall column
_ROW_H   = 12   # pixels per pitch-class row
_LABEL_W = 28   # space for note name labels on the left

_PLOT_W  = _N_COLS * _COL_W
_PLOT_H  = _N_ROWS * _ROW_H
_TOT_W   = _LABEL_W + _PLOT_W
_TOT_H   = _PLOT_H

_BG       = QColor("#0a0a14")
_TEXT_COL = QColor("#88aacc")

_NOTE_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]


def _energy_rgb(v: float):
    """Map energy [0, 1] to (r, g, b) tuple — cold-to-hot plasma ramp."""
    v = max(0.0, min(1.0, v))
    if v < 0.25:
        return (0, 0, int(64 + v * 4 * 191))
    elif v < 0.5:
        t = (v - 0.25) * 4
        return (0, int(t * 200), 255)
    elif v < 0.75:
        t = (v - 0.5) * 4
        return (int(t * 255), 200, int(255 * (1 - t)))
    else:
        t = (v - 0.75) * 4
        return (255, int(200 * (1 - t)), 0)


class TelemetryWaterfallWidget(QWidget):
    """Scrolling pitch-class waterfall using a QImage pixel buffer."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(_TOT_W, _TOT_H)

        # QImage pixel buffer for the waterfall (RGB888 = 3 bytes per pixel).
        self._img = QImage(_PLOT_W, _PLOT_H, QImage.Format_RGB888)
        self._img.fill(Qt.black)

        # Numpy view into QImage memory — modifications are reflected immediately.
        # Shape: (height, width, 3) = (_PLOT_H, _PLOT_W, 3)
        ptr       = self._img.bits()
        self._arr = np.frombuffer(ptr, dtype=np.uint8).reshape(_PLOT_H, _PLOT_W, 3)

    def update_frame(self, frame) -> None:
        """Shift the pixel buffer left, write new chroma column, repaint."""
        chroma = np.asarray(frame.chroma, dtype=np.float32)
        max_c  = float(chroma.max()) or 1.0

        # Shift image left by COL_W pixels (discard leftmost column).
        self._arr[:, :-_COL_W, :] = self._arr[:, _COL_W:, :]

        # Write new rightmost column.
        col_x = _PLOT_W - _COL_W
        for ri in range(_N_ROWS):
            row_idx = _N_ROWS - 1 - ri   # C at bottom
            e       = float(chroma[row_idx]) / max_c
            r, g, b = _energy_rgb(e)
            y0 = ri * _ROW_H
            y1 = y0 + _ROW_H
            self._arr[y0:y1, col_x:, :] = (r, g, b)

        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)

        # Background (label area)
        painter.fillRect(0, 0, _TOT_W, _TOT_H, _BG)

        # Waterfall image
        painter.drawImage(_LABEL_W, 0, self._img)

        # Note name labels on the left (static text)
        painter.setPen(_TEXT_COL)
        for ri in range(_N_ROWS):
            row_idx = _N_ROWS - 1 - ri
            y       = ri * _ROW_H
            painter.drawText(0, y, _LABEL_W, _ROW_H,
                             Qt.AlignHCenter | Qt.AlignVCenter,
                             _NOTE_NAMES[row_idx])

        painter.end()
