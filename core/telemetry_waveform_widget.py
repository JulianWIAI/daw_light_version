"""
core/telemetry_waveform_widget.py

PySide6 QWidget: rolling oscilloscope waveform + RMS bar.
Receives a TelemetryFrame from TelemetryManager; all DSP is in C++.

Rendering is done entirely with QPainter — no canvas.delete().
The widget calls update() to schedule a repaint; Qt batches and minimises
actual screen refreshes automatically.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QPen, QColor, QLinearGradient
from PySide6.QtWidgets import QWidget

_W, _H     = 480, 130
_RMS_H     = 14
_BG        = QColor("#0a0a14")
_WAVE_COL  = QColor("#00d4ff")
_RMS_COL   = QColor("#ff6040")
_GRID_COL  = QColor("#1a1a2e")
_TEXT_COL  = QColor("#88aacc")


class TelemetryWaveformWidget(QWidget):
    """Oscilloscope waveform + RMS loudness bar, updated from TelemetryFrame."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(_W, _H)
        self._samples = np.zeros(_W, dtype=np.float32)
        self._rms     = 0.0

    def update_frame(self, frame) -> None:
        """Receive a new TelemetryFrame and schedule a repaint."""
        wave = np.asarray(frame.waveform, dtype=np.float32)
        n    = len(wave)
        if n >= _W:
            step          = n // _W
            self._samples = wave[::step][:_W]
        elif n > 0:
            self._samples = wave
        self._rms = float(frame.rms)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        plot_h = _H - _RMS_H
        mid    = plot_h // 2

        # Background
        painter.fillRect(0, 0, _W, _H, _BG)

        # Grid lines
        painter.setPen(QPen(_GRID_COL, 1))
        for y in (mid // 2, mid, mid + mid // 2):
            painter.drawLine(0, y, _W, y)
        for x in range(0, _W, _W // 10):
            painter.drawLine(x, 0, x, plot_h)

        # Waveform polyline
        samples = self._samples
        n       = len(samples)
        if n >= 2:
            peak = float(np.abs(samples).max()) or 1.0
            painter.setPen(QPen(_WAVE_COL, 1))
            prev_x = prev_y = None
            for i in range(n):
                x = int(i * _W / n)
                y = int(mid - (float(samples[i]) / peak) * (mid - 4))
                if prev_x is not None:
                    painter.drawLine(prev_x, prev_y, x, y)
                prev_x, prev_y = x, y

        # RMS bar
        rms_norm = min(self._rms * 10.0, 1.0)
        bar_w    = int(rms_norm * _W)
        painter.fillRect(0, plot_h, _W, _RMS_H, QColor("#0d0d1a"))
        if bar_w > 0:
            painter.fillRect(0, plot_h, bar_w, _RMS_H, _RMS_COL)
        painter.setPen(QPen(_TEXT_COL, 1))
        painter.setFont(self.font())
        painter.drawText(4, plot_h + _RMS_H - 2, f"RMS {rms_norm:.0%}")

        painter.end()
