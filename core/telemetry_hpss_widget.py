"""
core/telemetry_hpss_widget.py

PySide6 QWidget: mirrored Harmonic / Percussive energy timeline.
Receives H/P ratios from TelemetryFrame.harmonic / .percussive (C++ DSP).

Benchmark overlay: call set_benchmark(hp_target, hp_tolerance) to draw shaded
reference zones and dashed target lines in the H and P halves.  The live curves
turn green/red based on whether the current values fall within the reference range.
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
_COL_IN   = QColor("#00FF88")
_COL_OUT  = QColor("#FF3333")

# Ballistic smoothing — same constants as TelemetryBandWidget.
_ATK = 0.8
_REL = 0.15


class TelemetryHpssWidget(QWidget):
    """Mirrored H (top) / P (bottom) timeline with optional benchmark overlay."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(_W, _H)
        self._harm_hist: Deque[float] = deque(maxlen=_HISTORY)
        self._perc_hist: Deque[float] = deque(maxlen=_HISTORY)

        self._smooth_harm = 0.0
        self._smooth_perc = 0.0

        self._has_benchmark   = False
        self._hp_target       = 0.5
        self._hp_tolerance    = 0.1

    # ── Benchmark API ─────────────────────────────────────────────────────────

    def set_benchmark(self, hp_target: float, hp_tolerance: float) -> None:
        """Show reference zones.  hp_target is the harmonic fraction [0, 1]."""
        self._hp_target    = hp_target
        self._hp_tolerance = hp_tolerance
        self._has_benchmark = True
        self.update()

    def clear_benchmark(self) -> None:
        """Remove overlay and restore default curve colours."""
        self._has_benchmark = False
        self.update()

    # ── Public update API ─────────────────────────────────────────────────────

    def update_frame(self, frame) -> None:
        """Apply ballistic smoothing to H/P values, append to histories, and repaint."""
        h_raw = float(frame.harmonic)
        p_raw = float(frame.percussive)

        if h_raw > self._smooth_harm:
            self._smooth_harm = _ATK * h_raw + (1.0 - _ATK) * self._smooth_harm
        else:
            self._smooth_harm = _REL * h_raw + (1.0 - _REL) * self._smooth_harm

        if p_raw > self._smooth_perc:
            self._smooth_perc = _ATK * p_raw + (1.0 - _ATK) * self._smooth_perc
        else:
            self._smooth_perc = _REL * p_raw + (1.0 - _REL) * self._smooth_perc

        self._harm_hist.append(self._smooth_harm)
        self._perc_hist.append(self._smooth_perc)
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def _draw_curve(self, painter: QPainter, history: Deque[float],
                    mirror: bool, colour: QColor) -> None:
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

        mid     = _H // 2
        amp_max = mid - 6

        # ── Benchmark zones (drawn first, behind everything) ──────────────────
        if self._has_benchmark:
            h_t   = self._hp_target
            h_tol = self._hp_tolerance
            p_t   = 1.0 - h_t

            h_val = float(self._harm_hist[-1]) if self._harm_hist else 0.0
            p_val = float(self._perc_hist[-1]) if self._perc_hist else 0.0
            h_in  = abs(h_val - h_t) <= h_tol
            p_in  = abs(p_val - p_t) <= h_tol

            # Harmonic zone (upper half)
            hy_top = mid - int(min(h_t + h_tol, 1.0) * amp_max)
            hy_bot = mid - int(max(h_t - h_tol, 0.0) * amp_max)
            painter.fillRect(0, hy_top, _W, max(1, hy_bot - hy_top),
                             QColor("#1a4a1a") if h_in else QColor("#4a1a1a"))

            # Percussive zone (lower half)
            py_top = mid + int(max(p_t - h_tol, 0.0) * amp_max)
            py_bot = mid + int(min(p_t + h_tol, 1.0) * amp_max)
            painter.fillRect(0, py_top, _W, max(1, py_bot - py_top),
                             QColor("#1a4a1a") if p_in else QColor("#4a1a1a"))

        # ── Grid ──────────────────────────────────────────────────────────────
        painter.setPen(QPen(_GRID_COL, 1))
        for yg in (mid // 2, mid + mid // 2):
            painter.drawLine(0, yg, _W, yg)

        # Centre separator
        painter.setPen(QPen(_MID_COL, 2))
        painter.drawLine(0, mid, _W, mid)

        # ── Live curves ───────────────────────────────────────────────────────
        if self._has_benchmark:
            h_val = float(self._harm_hist[-1]) if self._harm_hist else 0.0
            p_val = float(self._perc_hist[-1]) if self._perc_hist else 0.0
            h_col = _COL_IN if abs(h_val - self._hp_target) <= self._hp_tolerance else _COL_OUT
            p_col = _COL_IN if abs(p_val - (1.0 - self._hp_target)) <= self._hp_tolerance else _COL_OUT
        else:
            h_col = _HARM_COL
            p_col = _PERC_COL

        self._draw_curve(painter, self._harm_hist, mirror=False, colour=h_col)
        self._draw_curve(painter, self._perc_hist, mirror=True,  colour=p_col)

        # ── Target lines (on top of curves) ───────────────────────────────────
        if self._has_benchmark:
            dash_pen = QPen(QColor("#aaffcc"), 1, Qt.DashLine)
            painter.setPen(dash_pen)
            hy = mid - int(self._hp_target * amp_max)
            painter.drawLine(0, hy, _W, hy)
            py = mid + int((1.0 - self._hp_target) * amp_max)
            painter.drawLine(0, py, _W, py)

        # ── Labels ────────────────────────────────────────────────────────────
        h_val = self._harm_hist[-1] if self._harm_hist else 0.0
        p_val = self._perc_hist[-1] if self._perc_hist else 0.0
        painter.setPen(QPen(h_col, 1))
        painter.drawText(2, 2, _W - 4, 14, Qt.AlignLeft,  "H")
        painter.drawText(2, 2, _W - 4, 14, Qt.AlignRight, f"{h_val:.0%}")
        painter.setPen(QPen(p_col, 1))
        painter.drawText(2, _H - 16, _W - 4, 14, Qt.AlignLeft,  "P")
        painter.drawText(2, _H - 16, _W - 4, 14, Qt.AlignRight, f"{p_val:.0%}")

        painter.end()
