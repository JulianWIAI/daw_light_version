"""
core/telemetry_hpss_panel.py

Mirrored Harmonic / Percussive energy timeline panel.
The H/P ratio comes directly from TelemetryFrame.harmonic/.percussive;
this panel only maintains a rolling display history and redraws two polylines.

Optimisation: both polylines are pre-created and updated with canvas.coords()
each frame — no canvas.delete('all').

Benchmark overlay: call set_benchmark(hp_target, hp_tolerance) to show a
shaded reference zone and a dashed target line in both H and P halves.
The polylines turn green/red based on whether the current values fall within
the acoustic reference range.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, List

import tkinter as tk

_BG          = '#0a0a14'
_HARM_COL    = '#44aaff'
_PERC_COL    = '#ff6644'
_GRID_COL    = '#1a1a2e'
_TEXT_COL    = '#88aacc'
_MIRROR_LINE = '#2a2a3e'

_COL_IN  = '#00FF88'   # line color when within benchmark tolerance
_COL_OUT = '#FF3333'   # line color when outside benchmark tolerance
_ZONE_IN  = '#1a3a1a'  # zone fill color when in range
_ZONE_OUT = '#3a1a1a'  # zone fill color when out of range

_CANVAS_W = 480
_CANVAS_H = 200
_HISTORY  = 80   # rolling display ticks kept for the timeline


class TelemetryHpssPanel(tk.Frame):
    """Mirrored harmonic (top) / percussive (bottom) energy timeline."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=_BG)

        self._cw  = _CANVAS_W
        self._ch  = _CANVAS_H
        self._mid = _CANVAS_H // 2

        canvas = tk.Canvas(self, width=_CANVAS_W, height=_CANVAS_H,
                           bg=_BG, highlightthickness=0)
        canvas.pack(fill=tk.BOTH)
        self._canvas = canvas

        # ── Static grid and separator lines ───────────────────────────────────
        canvas.create_line(0, self._mid, _CANVAS_W, self._mid,
                           fill=_MIRROR_LINE, width=2)
        for yg in (self._mid // 2, self._mid + self._mid // 2):
            canvas.create_line(0, yg, _CANVAS_W, yg, fill=_GRID_COL)

        canvas.create_text(6, self._mid - 2, text='H',
                           anchor='sw', fill=_HARM_COL, font=('Consolas', 8, 'bold'))
        canvas.create_text(6, self._mid + 2, text='P',
                           anchor='nw', fill=_PERC_COL, font=('Consolas', 8, 'bold'))

        # ── Benchmark overlay items (created before polylines so they sit behind) ──
        self._h_zone_id = canvas.create_rectangle(
            0, self._mid, _CANVAS_W, self._mid,
            fill=_ZONE_IN, outline='', state='hidden')
        self._p_zone_id = canvas.create_rectangle(
            0, self._mid, _CANVAS_W, self._mid,
            fill=_ZONE_IN, outline='', state='hidden')
        self._h_line_id = canvas.create_line(
            0, self._mid, _CANVAS_W, self._mid,
            fill='#aaffcc', width=1, dash=(4, 2), state='hidden')
        self._p_line_id = canvas.create_line(
            0, self._mid, _CANVAS_W, self._mid,
            fill='#aaffcc', width=1, dash=(4, 2), state='hidden')

        # ── Live polylines (on top of zones) ─────────────────────────────────
        self._harm_line_id = canvas.create_line(
            0, self._mid, _CANVAS_W, self._mid,
            fill=_HARM_COL, width=2, smooth=True)
        self._perc_line_id = canvas.create_line(
            0, self._mid, _CANVAS_W, self._mid,
            fill=_PERC_COL, width=2, smooth=True)

        # ── Live value labels (updated each frame, on top of everything) ──────
        self._harm_lbl_id = canvas.create_text(
            _CANVAS_W - 4, 6, text='H 0%',
            anchor='ne', fill=_HARM_COL, font=('Consolas', 8))
        self._perc_lbl_id = canvas.create_text(
            _CANVAS_W - 4, _CANVAS_H - 6, text='P 0%',
            anchor='se', fill=_PERC_COL, font=('Consolas', 8))

        # ── Rolling history deques ────────────────────────────────────────────
        self._harm_hist: Deque[float] = deque(maxlen=_HISTORY)
        self._perc_hist: Deque[float] = deque(maxlen=_HISTORY)

        # ── Benchmark state ───────────────────────────────────────────────────
        self._has_benchmark   = False
        self._hp_target       = 0.5
        self._hp_tolerance    = 0.1

    # ── Benchmark API ─────────────────────────────────────────────────────────

    def set_benchmark(self, hp_target: float, hp_tolerance: float) -> None:
        """Show H/P reference zones. hp_target is the harmonic fraction [0, 1]."""
        self._hp_target    = hp_target
        self._hp_tolerance = hp_tolerance
        self._has_benchmark = True
        self._reposition_overlay()
        for item_id in (self._h_zone_id, self._h_line_id,
                        self._p_zone_id, self._p_line_id):
            self._canvas.itemconfig(item_id, state='normal')

    def clear_benchmark(self) -> None:
        """Remove overlay and restore default polyline colours."""
        self._has_benchmark = False
        for item_id in (self._h_zone_id, self._h_line_id,
                        self._p_zone_id, self._p_line_id):
            self._canvas.itemconfig(item_id, state='hidden')
        self._canvas.itemconfig(self._harm_line_id, fill=_HARM_COL)
        self._canvas.itemconfig(self._perc_line_id, fill=_PERC_COL)

    def _reposition_overlay(self) -> None:
        """Compute overlay geometry from stored target/tolerance (called once per set_benchmark)."""
        mid     = self._mid
        amp_max = mid - 6   # same scale used by _build_polyline_coords

        h_t   = self._hp_target
        h_tol = self._hp_tolerance
        p_t   = 1.0 - h_t

        # Harmonic zone (upper half — y decreases from mid as amplitude grows).
        hy_target = mid - int(h_t * amp_max)
        hy_top    = mid - int(min(h_t + h_tol, 1.0) * amp_max)
        hy_bot    = mid - int(max(h_t - h_tol, 0.0) * amp_max)
        self._canvas.coords(self._h_zone_id,  0, hy_top, _CANVAS_W, hy_bot)
        self._canvas.coords(self._h_line_id,  0, hy_target, _CANVAS_W, hy_target)

        # Percussive zone (lower half — y increases from mid as amplitude grows).
        py_target = mid + int(p_t * amp_max)
        py_top    = mid + int(max(p_t - h_tol, 0.0) * amp_max)
        py_bot    = mid + int(min(p_t + h_tol, 1.0) * amp_max)
        self._canvas.coords(self._p_zone_id,  0, py_top, _CANVAS_W, py_bot)
        self._canvas.coords(self._p_line_id,  0, py_target, _CANVAS_W, py_target)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _build_polyline_coords(self, history: Deque[float],
                               mirror: bool) -> List[int]:
        """Convert a rolling history of values to a flat [x, y, ...] list."""
        vals = list(history)
        if len(vals) < 2:
            return [0, self._mid, self._cw, self._mid]
        step = max(1, self._cw // len(vals))
        coords: List[int] = []
        for i, v in enumerate(vals):
            x   = i * step
            amp = int(v * (self._mid - 6))
            y   = (self._mid - amp) if not mirror else (self._mid + amp)
            coords += [x, y]
        return coords

    # ── public update API ─────────────────────────────────────────────────────

    def update_from_frame(self, frame) -> None:
        """Refresh both polylines from TelemetryFrame.harmonic / .percussive."""
        h = float(frame.harmonic)
        p = float(frame.percussive)

        self._harm_hist.append(h)
        self._perc_hist.append(p)

        harm_coords = self._build_polyline_coords(self._harm_hist, mirror=False)
        perc_coords = self._build_polyline_coords(self._perc_hist, mirror=True)

        self._canvas.coords(self._harm_line_id, *harm_coords)
        self._canvas.coords(self._perc_line_id, *perc_coords)

        if self._has_benchmark:
            h_in = abs(h - self._hp_target) <= self._hp_tolerance
            p_t  = 1.0 - self._hp_target
            p_in = abs(p - p_t) <= self._hp_tolerance
            self._canvas.itemconfig(self._harm_line_id,
                                    fill=_COL_IN if h_in else _COL_OUT)
            self._canvas.itemconfig(self._perc_line_id,
                                    fill=_COL_IN if p_in else _COL_OUT)
            zone_h_col = _ZONE_IN if h_in else _ZONE_OUT
            zone_p_col = _ZONE_IN if p_in else _ZONE_OUT
            self._canvas.itemconfig(self._h_zone_id, fill=zone_h_col)
            self._canvas.itemconfig(self._p_zone_id, fill=zone_p_col)
        else:
            self._canvas.itemconfig(self._harm_line_id, fill=_HARM_COL)
            self._canvas.itemconfig(self._perc_line_id, fill=_PERC_COL)

        self._canvas.itemconfig(self._harm_lbl_id, text=f'H {h:.0%}')
        self._canvas.itemconfig(self._perc_lbl_id, text=f'P {p:.0%}')
