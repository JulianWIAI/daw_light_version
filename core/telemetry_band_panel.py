"""
core/telemetry_band_panel.py

7-band frequency bar panel, updated from TelemetryFrame.bands.
All DSP is in C++; this panel only queries pre-computed values.

Optimisation: 7 bar rectangles are created once in __init__ and repositioned
with canvas.coords() each frame.  No canvas.delete('all') is ever called.
"""

import tkinter as tk

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


class TelemetryBandPanel(tk.Frame):
    """7-band vertical bar graph, updated from TelemetryFrame.bands."""

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

        self._bar_ids:  list = []
        self._pct_ids:  list = []

        for i, (label, colour) in enumerate(_BANDS):
            x0 = i * bw + pad
            x1 = (i + 1) * bw - pad

            # Static background track.
            canvas.create_rectangle(x0, _CANVAS_H - 20, x1, 20,
                                    fill=_GRID_COL, outline='')
            # Band label at bottom (static).
            canvas.create_text((x0 + x1) // 2, _CANVAS_H - 10,
                               text=label.split('\n')[0],
                               fill=_TEXT_COL, font=('Consolas', 7))

            # Live bar — starts at zero height.
            bar_id = canvas.create_rectangle(
                x0, _CANVAS_H - 20, x1, _CANVAS_H - 20,
                fill=colour, outline='')
            self._bar_ids.append(bar_id)

            # Percentage label above the bar (updated each frame).
            pct_id = canvas.create_text(
                (x0 + x1) // 2, 12,
                text='0%', fill=colour, font=('Consolas', 7))
            self._pct_ids.append(pct_id)

        # Store bar geometry for coords() updates.
        self._bw  = bw
        self._pad = pad

    # ── public update API ─────────────────────────────────────────────────────

    def update_from_frame(self, frame) -> None:
        """Refresh all 7 bars from TelemetryFrame.bands.  No delete() called."""
        bands = frame.bands   # numpy array of shape (7,)
        n     = len(_BANDS)
        bw    = self._bw
        pad   = self._pad
        usable_h = self._ch - 40  # space between label row and top margin

        for i in range(n):
            val    = float(bands[i])
            bar_h  = int(val * usable_h)
            x0     = i * bw + pad
            x1     = (i + 1) * bw - pad
            y0     = self._ch - 20 - bar_h
            y1     = self._ch - 20

            self._canvas.coords(self._bar_ids[i], x0, y0, x1, y1)
            self._canvas.itemconfig(self._pct_ids[i], text=f'{val:.0%}')
