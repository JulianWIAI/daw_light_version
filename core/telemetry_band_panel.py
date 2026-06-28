"""
core/telemetry_band_panel.py

7-band frequency bar panel, updated from TelemetryFrame.bands.
All DSP is in C++; this panel only queries pre-computed values.

Optimisation: 7 bar rectangles are created once in __init__ and repositioned
with canvas.coords() each frame.  No canvas.delete('all') is ever called.

Benchmark overlay: call set_benchmark(targets, tolerances) to show target
lines and tolerance zones per band; bars turn green/red based on proximity to
the acoustic reference profile.
"""

import tkinter as tk
from typing import List

_BG       = '#0a0a14'
_GRID_COL = '#1a1a2e'
_TEXT_COL = '#88aacc'

_CANVAS_W = 420
_CANVAS_H = 200

# Labels and colours matching the 7 frequency bands in TelemetryAnalyzer.
_BANDS = [
    ('Sub\nBass', '#6644ff'),
    ('Bass',      '#4488ff'),
    ('Low\nMid',  '#44ccff'),
    ('Mid',       '#44ffaa'),
    ('Hi\nMid',   '#aaff44'),
    ('High',      '#ffcc44'),
    ('Brill.',    '#ff6644'),
]

_COL_IN  = '#00FF88'   # bar color when within benchmark tolerance
_COL_OUT = '#FF3333'   # bar color when outside benchmark tolerance
_ZONE_IN  = '#1a4a1a'  # tolerance zone fill when in range
_ZONE_OUT = '#4a1a1a'  # tolerance zone fill when out of range
_TARGET_LINE = '#ffffff'


class TelemetryBandPanel(tk.Frame):
    """7-band vertical bar graph with optional benchmark overlay."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=_BG)

        self._cw = _CANVAS_W
        self._ch = _CANVAS_H

        canvas = tk.Canvas(self, width=_CANVAS_W, height=_CANVAS_H,
                           bg=_BG, highlightthickness=0)
        canvas.pack(fill=tk.BOTH)
        self._canvas = canvas

        n   = len(_BANDS)
        bw  = _CANVAS_W // n
        pad = 4

        self._bar_ids:          List[int] = []
        self._pct_ids:          List[int] = []
        self._overlay_rect_ids: List[int] = []
        self._target_line_ids:  List[int] = []

        self._has_benchmark      = False
        self._bench_targets:    List[float] = []
        self._bench_tolerances: List[float] = []

        # ── Layer 1: static background tracks and band labels ─────────────────
        for i, (label, _colour) in enumerate(_BANDS):
            x0 = i * bw + pad
            x1 = (i + 1) * bw - pad
            canvas.create_rectangle(x0, _CANVAS_H - 20, x1, 20,
                                    fill=_GRID_COL, outline='')
            canvas.create_text((x0 + x1) // 2, _CANVAS_H - 10,
                               text=label.split('\n')[0],
                               fill=_TEXT_COL, font=('Consolas', 7))

        # ── Layer 2: tolerance zone rectangles (hidden until set_benchmark) ───
        for i in range(n):
            x0 = i * bw + pad
            x1 = (i + 1) * bw - pad
            rect_id = canvas.create_rectangle(
                x0, _CANVAS_H - 20, x1, _CANVAS_H - 20,
                fill=_ZONE_IN, outline='', state='hidden')
            self._overlay_rect_ids.append(rect_id)

        # ── Layer 3: live bars ────────────────────────────────────────────────
        for i, (_label, colour) in enumerate(_BANDS):
            x0 = i * bw + pad
            x1 = (i + 1) * bw - pad
            bar_id = canvas.create_rectangle(
                x0, _CANVAS_H - 20, x1, _CANVAS_H - 20,
                fill=colour, outline='')
            self._bar_ids.append(bar_id)

        # ── Layer 4: target lines (hidden until set_benchmark) ────────────────
        for i in range(n):
            x0 = i * bw + pad
            x1 = (i + 1) * bw - pad
            line_id = canvas.create_line(
                x0, _CANVAS_H - 20, x1, _CANVAS_H - 20,
                fill=_TARGET_LINE, width=1, dash=(3, 2), state='hidden')
            self._target_line_ids.append(line_id)

        # ── Layer 5: percentage labels (always on top) ────────────────────────
        for i, (_label, colour) in enumerate(_BANDS):
            x0 = i * bw + pad
            x1 = (i + 1) * bw - pad
            pct_id = canvas.create_text(
                (x0 + x1) // 2, 12,
                text='0%', fill=colour, font=('Consolas', 7))
            self._pct_ids.append(pct_id)

        self._bw  = bw
        self._pad = pad

    # ── Benchmark API ─────────────────────────────────────────────────────────

    def set_benchmark(self, targets: List[float], tolerances: List[float]) -> None:
        """Activate overlay. targets and tolerances are 7-element lists in [0, 1]."""
        self._bench_targets    = targets
        self._bench_tolerances = tolerances
        self._has_benchmark    = True
        for rid in self._overlay_rect_ids:
            self._canvas.itemconfig(rid, state='normal')
        for lid in self._target_line_ids:
            self._canvas.itemconfig(lid, state='normal')

    def clear_benchmark(self) -> None:
        """Remove overlay and restore default bar colours."""
        self._bench_targets    = []
        self._bench_tolerances = []
        self._has_benchmark    = False
        for rid in self._overlay_rect_ids:
            self._canvas.itemconfig(rid, state='hidden')
        for lid in self._target_line_ids:
            self._canvas.itemconfig(lid, state='hidden')
        for i, (_label, colour) in enumerate(_BANDS):
            self._canvas.itemconfig(self._bar_ids[i],  fill=colour)
            self._canvas.itemconfig(self._pct_ids[i],  fill=colour)

    # ── public update API ─────────────────────────────────────────────────────

    def update_from_frame(self, frame) -> None:
        """Refresh all 7 bars from TelemetryFrame.bands.  No delete() called."""
        bands    = frame.bands
        n        = len(_BANDS)
        bw       = self._bw
        pad      = self._pad
        usable_h = self._ch - 40

        for i in range(n):
            val   = float(bands[i])
            bar_h = int(val * usable_h)
            x0    = i * bw + pad
            x1    = (i + 1) * bw - pad
            y0    = self._ch - 20 - bar_h
            y1    = self._ch - 20

            self._canvas.coords(self._bar_ids[i], x0, y0, x1, y1)

            if self._has_benchmark and i < len(self._bench_targets):
                target = self._bench_targets[i]
                tol    = self._bench_tolerances[i]
                in_range = abs(val - target) <= tol

                # Color bar and label.
                bar_col = _COL_IN if in_range else _COL_OUT
                self._canvas.itemconfig(self._bar_ids[i], fill=bar_col)
                self._canvas.itemconfig(self._pct_ids[i], fill=bar_col)

                # Tolerance zone rectangle.
                zy_top = self._ch - 20 - int(min(target + tol, 1.0) * usable_h)
                zy_bot = self._ch - 20 - int(max(target - tol, 0.0) * usable_h)
                zone_col = _ZONE_IN if in_range else _ZONE_OUT
                self._canvas.coords(self._overlay_rect_ids[i], x0, zy_top, x1, zy_bot)
                self._canvas.itemconfig(self._overlay_rect_ids[i], fill=zone_col)

                # Target line.
                ty = self._ch - 20 - int(target * usable_h)
                self._canvas.coords(self._target_line_ids[i], x0, ty, x1, ty)
            else:
                self._canvas.itemconfig(self._bar_ids[i], fill=_BANDS[i][1])
                self._canvas.itemconfig(self._pct_ids[i], fill=_BANDS[i][1])

            self._canvas.itemconfig(self._pct_ids[i], text=f'{val:.0%}')
