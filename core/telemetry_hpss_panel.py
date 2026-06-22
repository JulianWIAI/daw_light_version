"""
core/telemetry_hpss_panel.py

Mirrored Harmonic / Percussive energy timeline panel.
The H/P ratio comes directly from TelemetryFrame.harmonic/.percussive;
this panel only maintains a rolling display history and redraws two polylines.

Optimisation: both polylines are pre-created and updated with canvas.coords()
each frame — no canvas.delete('all').
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

        # Static grid and separator lines.
        canvas.create_line(0, self._mid, _CANVAS_W, self._mid,
                           fill=_MIRROR_LINE, width=2)
        for yg in (self._mid // 2, self._mid + self._mid // 2):
            canvas.create_line(0, yg, _CANVAS_W, yg, fill=_GRID_COL)

        canvas.create_text(6, self._mid - 2, text='H',
                           anchor='sw', fill=_HARM_COL, font=('Consolas', 8, 'bold'))
        canvas.create_text(6, self._mid + 2, text='P',
                           anchor='nw', fill=_PERC_COL, font=('Consolas', 8, 'bold'))

        # Pre-create both polylines with two stub points (avoids 0-point error).
        self._harm_line_id = canvas.create_line(
            0, self._mid, _CANVAS_W, self._mid,
            fill=_HARM_COL, width=2, smooth=True)
        self._perc_line_id = canvas.create_line(
            0, self._mid, _CANVAS_W, self._mid,
            fill=_PERC_COL, width=2, smooth=True)

        # Live value labels (updated each frame).
        self._harm_lbl_id = canvas.create_text(
            _CANVAS_W - 4, 6, text='H 0%',
            anchor='ne', fill=_HARM_COL, font=('Consolas', 8))
        self._perc_lbl_id = canvas.create_text(
            _CANVAS_W - 4, _CANVAS_H - 6, text='P 0%',
            anchor='se', fill=_PERC_COL, font=('Consolas', 8))

        # Rolling history deques (Python-side only — very cheap).
        self._harm_hist: Deque[float] = deque(maxlen=_HISTORY)
        self._perc_hist: Deque[float] = deque(maxlen=_HISTORY)

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

        # Update polylines in-place — no canvas.delete('all').
        self._canvas.coords(self._harm_line_id, *harm_coords)
        self._canvas.coords(self._perc_line_id, *perc_coords)

        self._canvas.itemconfig(self._harm_lbl_id, text=f'H {h:.0%}')
        self._canvas.itemconfig(self._perc_lbl_id, text=f'P {p:.0%}')
