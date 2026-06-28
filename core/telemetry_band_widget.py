"""
core/telemetry_band_widget.py

PySide6 QWidget: 7-band frequency bar graph.
Receives pre-computed band values from TelemetryFrame.bands (C++ DSP).

Benchmark overlay: call set_benchmark(targets, tolerances) to draw dashed
target lines, shaded tolerance zones, and green/red bar coloring against an
acoustic reference profile.
"""

from __future__ import annotations

from typing import List

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import QWidget

_W, _H       = 300, 162   # +12 px for the Total Energy strip at the bottom
_ENERGY_H    = 12         # height of the Total Energy strip
_ENERGY_REF  = 0.60       # band mean that equals 100 % Total Energy
_ENERGY_LIMIT = 1.20      # ratio above which the strip turns red (120 %)
_BG          = QColor("#0a0a14")
_GRID_COL = QColor("#1a1a2e")
_TEXT_COL = QColor("#88aacc")
_COL_IN   = QColor("#00FF88")
_COL_OUT  = QColor("#FF3333")

_BANDS = [
    ("Sub",   QColor("#6644ff")),
    ("Bass",  QColor("#4488ff")),
    ("Lo",    QColor("#44ccff")),
    ("Mid",   QColor("#44ffaa")),
    ("Hi",    QColor("#aaff44")),
    ("High",  QColor("#ffcc44")),
    ("Brill.",QColor("#ff6644")),
]

# Ballistic smoothing coefficients (applied per 33 ms frame at 30 fps).
# Attack: fraction of gap closed per frame on a rising edge  → ~66 ms to reach 96%.
# Release: fraction of gap closed per frame on a falling edge → ~660 ms to reach 96%.
_ATK = 0.8
_REL = 0.15


class TelemetryBandWidget(QWidget):
    """7-band frequency bar graph with optional acoustic benchmark overlay."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(_W, _H)
        self._bands    = np.zeros(7, dtype=np.float32)
        self._smoothed = np.zeros(7, dtype=np.float32)
        self._total_energy: float = 0.0

        self._has_benchmark      = False
        self._bench_targets:    List[float] = []
        self._bench_tolerances: List[float] = []

    def set_total_energy(self, value: float) -> None:
        """Store the mean band energy (0-1) for the Total Energy strip overlay."""
        self._total_energy = value

    # ── Benchmark API ─────────────────────────────────────────────────────────

    def set_benchmark(self, targets: List[float], tolerances: List[float]) -> None:
        """Activate overlay.  targets and tolerances are 7-element lists in [0,1]."""
        self._bench_targets    = targets
        self._bench_tolerances = tolerances
        self._has_benchmark    = True
        self.update()

    def clear_benchmark(self) -> None:
        """Remove overlay and restore default bar colours."""
        self._bench_targets    = []
        self._bench_tolerances = []
        self._has_benchmark    = False
        self.update()

    # ── Public update API ─────────────────────────────────────────────────────

    def update_frame(self, frame) -> None:
        """Apply ballistic smoothing to band values and schedule a repaint."""
        raw    = np.asarray(frame.bands, dtype=np.float32)
        rising = raw > self._smoothed
        self._smoothed = np.where(
            rising,
            _ATK * raw + (1.0 - _ATK) * self._smoothed,
            _REL * raw + (1.0 - _REL) * self._smoothed,
        )
        self._bands = self._smoothed
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        painter.fillRect(0, 0, _W, _H, _BG)

        n        = len(_BANDS)
        bw       = _W // n
        pad      = 3
        label_h  = 20
        bar_area = _H - label_h - _ENERGY_H  # usable height for bars (130 px)

        for i, (label, default_colour) in enumerate(_BANDS):
            x0 = i * bw + pad
            x1 = (i + 1) * bw - pad
            w  = x1 - x0

            # Background track
            painter.fillRect(x0, 0, w, bar_area, _GRID_COL)

            val   = float(self._bands[i])
            bar_h = int(val * (bar_area - 4))

            # ── Benchmark overlay (drawn before bar so bar appears on top) ──
            if self._has_benchmark and i < len(self._bench_targets):
                target   = self._bench_targets[i]
                tol      = self._bench_tolerances[i]
                in_range = abs(val - target) <= tol

                # Tolerance zone rectangle
                zy_top = bar_area - int(min(target + tol, 1.0) * (bar_area - 4))
                zy_bot = bar_area - int(max(target - tol, 0.0) * (bar_area - 4))
                zone_col = QColor("#1a4a1a") if in_range else QColor("#4a1a1a")
                painter.fillRect(x0, zy_top, w, max(1, zy_bot - zy_top), zone_col)

                bar_colour = _COL_IN if in_range else _COL_OUT
            else:
                bar_colour = default_colour

            # Live bar (on top of zone)
            if bar_h > 0:
                painter.fillRect(x0, bar_area - bar_h, w, bar_h, bar_colour)

            # Target line (on top of bar)
            if self._has_benchmark and i < len(self._bench_targets):
                target = self._bench_targets[i]
                ty     = bar_area - int(target * (bar_area - 4))
                pen = QPen(QColor("#ffffff"), 1, Qt.DashLine)
                painter.setPen(pen)
                painter.drawLine(x0, ty, x1, ty)

            # Percentage label
            painter.setPen(QPen(bar_colour, 1))
            painter.drawText(x0, 2, w, 12, Qt.AlignHCenter, f"{val:.0%}")

            # Band label at bottom
            painter.setPen(QPen(_TEXT_COL, 1))
            painter.drawText(x0, bar_area, w, label_h,
                             Qt.AlignHCenter | Qt.AlignVCenter, label)

        # ── Total Energy strip (bottom 12 px) ────────────────────────────────
        ratio   = self._total_energy / _ENERGY_REF    # 1.0 = 100 %, 1.2 = 120 %
        fill_w  = int(min(ratio, 2.0) / 2.0 * _W)    # full-width at 200 %
        if ratio < 1.0:
            strip_col = QColor("#1a3a1a")              # dim green — below nominal
        elif ratio < _ENERGY_LIMIT:
            strip_col = QColor("#7a5500")              # amber — hot but safe
        else:
            strip_col = QColor("#7a0000")              # red — compensation active

        strip_y = _H - _ENERGY_H
        painter.fillRect(0, strip_y, _W, _ENERGY_H, QColor("#08080f"))
        if fill_w > 0:
            painter.fillRect(0, strip_y, fill_w, _ENERGY_H, strip_col)

        label_col = _COL_OUT if ratio >= _ENERGY_LIMIT else _TEXT_COL
        painter.setPen(QPen(label_col, 1))
        painter.drawText(0, strip_y, _W, _ENERGY_H,
                         Qt.AlignHCenter | Qt.AlignVCenter,
                         f"Total Energy  {ratio * 100:.0f}%")

        painter.end()
