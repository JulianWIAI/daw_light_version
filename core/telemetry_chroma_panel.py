"""
core/telemetry_chroma_panel.py

12-bin pitch-class chroma histogram panel, updated from TelemetryFrame.chroma.
All DSP (FFT + chroma mapping) is computed in C++.

Optimisation: 12 bar rectangles and their percentage labels are pre-allocated
in __init__; each frame uses canvas.coords() / canvas.itemconfig() only.
"""

import tkinter as tk
import numpy as np

_BG       = '#0a0a14'
_GRID_COL = '#1a1a2e'
_TEXT_COL = '#88aacc'

_CANVAS_W = 380
_CANVAS_H = 180

_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F',
               'F#', 'G', 'G#', 'A', 'A#', 'B']

_NOTE_COLOURS = [
    '#ff4444', '#ff7744', '#ffaa44', '#ffdd44',
    '#bbff44', '#44ff88', '#44ffcc', '#44ccff',
    '#4488ff', '#8844ff', '#cc44ff', '#ff44cc',
]


class TelemetryChromaPanel(tk.Frame):
    """12-bin pitch-class histogram, updated from TelemetryFrame.chroma."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=_BG)

        self._cw = _CANVAS_W
        self._ch = _CANVAS_H

        canvas = tk.Canvas(self, width=_CANVAS_W, height=_CANVAS_H,
                           bg=_BG, highlightthickness=0)
        canvas.pack(fill=tk.BOTH)
        self._canvas = canvas

        n   = 12
        bw  = _CANVAS_W // n
        pad = 3

        self._bar_ids: list = []
        self._pct_ids: list = []
        self._dom_id: int   = 0

        for i, (name, colour) in enumerate(zip(_NOTE_NAMES, _NOTE_COLOURS)):
            x0 = i * bw + pad
            x1 = (i + 1) * bw - pad

            # Static background track.
            canvas.create_rectangle(x0, 20, x1, _CANVAS_H - 20,
                                    fill=_GRID_COL, outline='')
            # Static note name label at bottom.
            canvas.create_text((x0 + x1) // 2, _CANVAS_H - 10,
                               text=name, fill=_TEXT_COL, font=('Consolas', 8))

            # Live bar — zero height initially.
            bar_id = canvas.create_rectangle(
                x0, _CANVAS_H - 20, x1, _CANVAS_H - 20,
                fill=colour, outline='')
            self._bar_ids.append(bar_id)

            # Percentage label above bar (hidden when bar is tiny).
            pct_id = canvas.create_text(
                (x0 + x1) // 2, _CANVAS_H - 24,
                text='', fill=colour, font=('Consolas', 7))
            self._pct_ids.append(pct_id)

        # Dominant note label at top centre.
        self._dom_id = canvas.create_text(
            _CANVAS_W // 2, 10, text='Dominant: —',
            fill='#ffffff', font=('Consolas', 9, 'bold'))

        self._bw  = bw
        self._pad = pad

    # ── public update API ─────────────────────────────────────────────────────

    def update_from_frame(self, frame) -> None:
        """Refresh all 12 chroma bars from TelemetryFrame.chroma."""
        chroma   = np.asarray(frame.chroma, dtype=np.float32)
        max_val  = float(chroma.max()) if chroma.max() > 0 else 1.0
        bw       = self._bw
        pad      = self._pad
        usable_h = self._ch - 40

        for i, colour in enumerate(_NOTE_COLOURS):
            val    = float(chroma[i]) / max_val
            bar_h  = int(val * usable_h)
            x0     = i * bw + pad
            x1     = (i + 1) * bw - pad
            y0     = self._ch - 20 - bar_h
            y1     = self._ch - 20

            self._canvas.coords(self._bar_ids[i], x0, y0, x1, y1)
            # Only show the percentage label when the bar is tall enough.
            pct_text = f'{val:.0%}' if val > 0.05 else ''
            self._canvas.itemconfig(self._pct_ids[i], text=pct_text,
                                    state='normal' if pct_text else 'hidden')
            if pct_text:
                self._canvas.coords(self._pct_ids[i],
                                    (x0 + x1) // 2, y0 - 4)

        # Update dominant note label.
        dom = int(np.argmax(chroma))
        self._canvas.itemconfig(self._dom_id,
                                text=f'Dominant: {_NOTE_NAMES[dom]}',
                                fill=_NOTE_COLOURS[dom])
