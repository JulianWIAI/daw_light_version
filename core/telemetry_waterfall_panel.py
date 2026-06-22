"""
core/telemetry_waterfall_panel.py

Scrolling chromagram waterfall (heatmap) panel.
Columns scroll left as new frames arrive; rows are the 12 pitch classes.

Optimisation strategy:
  - Pre-allocate N_COLS × N_ROWS canvas rectangles tagged 'wf'.
  - Each frame: canvas.move('wf', -COL_W, 0) shifts everything left in ONE call.
  - Only the N_ROWS=12 leftmost cells are deleted and N_ROWS new cells are
    created on the right.  Net cost: 1 move + 12 deletes + 12 creates per frame,
    vs. 1200 itemconfig calls for a full per-cell colour update.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, List

import numpy as np
import tkinter as tk

_BG       = '#0a0a14'
_TEXT_COL = '#88aacc'

_N_ROWS  = 12
_N_COLS  = 100
_COL_W   = 4     # canvas pixels per waterfall column
_ROW_H   = 12    # canvas pixels per pitch-class row
_LABEL_W = 28    # width reserved for note labels on the left

_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F',
               'F#', 'G', 'G#', 'A', 'A#', 'B']

_WF_TAG = 'wf'  # canvas tag applied to every waterfall cell


def _energy_colour(v: float) -> str:
    """Map energy in [0, 1] to a cold-to-hot colour string."""
    v = max(0.0, min(1.0, v))
    if v < 0.25:
        r, g, b = 0, 0, int(64 + v * 4 * 191)
    elif v < 0.5:
        t = (v - 0.25) * 4
        r, g, b = 0, int(t * 200), 255
    elif v < 0.75:
        t = (v - 0.5) * 4
        r, g, b = int(t * 255), 200, int(255 * (1 - t))
    else:
        t = (v - 0.75) * 4
        r, g, b = 255, int(200 * (1 - t)), 0
    return f'#{r:02x}{g:02x}{b:02x}'


class TelemetryWaterfallPanel(tk.Frame):
    """Scrolling pitch-class waterfall, updated from TelemetryFrame.chroma."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=_BG)

        plot_w = _N_COLS * _COL_W
        plot_h = _N_ROWS * _ROW_H

        canvas = tk.Canvas(self,
                           width=_LABEL_W + plot_w, height=plot_h,
                           bg=_BG, highlightthickness=0)
        canvas.pack(fill=tk.BOTH)
        self._canvas = canvas

        # Static note labels on the left side.
        for ri in range(_N_ROWS):
            row_idx = _N_ROWS - 1 - ri  # C at bottom
            y = ri * _ROW_H + _ROW_H // 2
            canvas.create_text(_LABEL_W // 2, y,
                               text=_NOTE_NAMES[row_idx],
                               fill=_TEXT_COL, font=('Consolas', 7))

        # Pre-fill the waterfall with N_COLS black columns.
        # Each entry in _col_ids is a list of N_ROWS canvas item IDs.
        self._col_ids: Deque[List[int]] = deque()
        for ci in range(_N_COLS):
            col_ids = self._create_column(ci, [0.0] * _N_ROWS)
            self._col_ids.append(col_ids)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _create_column(self, ci: int, energies: List[float]) -> List[int]:
        """Create N_ROWS rectangles for column index ci.  Returns ID list."""
        ids: List[int] = []
        x0 = _LABEL_W + ci * _COL_W
        x1 = x0 + _COL_W
        for ri in range(_N_ROWS):
            y0 = ri * _ROW_H
            y1 = y0 + _ROW_H
            col = _energy_colour(energies[ri])
            cid = self._canvas.create_rectangle(
                x0, y0, x1, y1, fill=col, outline='', tags=_WF_TAG)
            ids.append(cid)
        return ids

    # ── public update API ─────────────────────────────────────────────────────

    def update_from_frame(self, frame) -> None:
        """Scroll left, drop leftmost column, append new rightmost column.
        Cost: 1 canvas.move() + 12 deletes + 12 creates — no full redraw.
        """
        chroma = np.asarray(frame.chroma, dtype=np.float32)
        max_c  = float(chroma.max()) or 1.0

        # Shift all tagged cells left by one column width.
        self._canvas.move(_WF_TAG, -_COL_W, 0)

        # Delete the leftmost column (now off-screen to the left).
        old_col = self._col_ids.popleft()
        for cid in old_col:
            self._canvas.delete(cid)

        # Build per-row energy values for the new rightmost column.
        # Row 0 is the highest pitch class; C is at the bottom.
        energies = [float(chroma[_N_ROWS - 1 - ri]) / max_c
                    for ri in range(_N_ROWS)]

        # New column always placed at the fixed rightmost pixel position.
        new_ids = self._create_column(_N_COLS - 1, energies)
        self._col_ids.append(new_ids)
