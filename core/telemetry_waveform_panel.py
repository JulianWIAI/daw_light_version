"""
core/telemetry_waveform_panel.py

Rolling oscilloscope waveform + RMS bar panel.
Renders purely from TelemetryFrame data — all DSP lives in C++.

Optimisation: the polyline and RMS rectangle are created once in __init__
and updated with canvas.coords() on every frame, avoiding canvas.delete('all').
"""

import tkinter as tk
import numpy as np

_BG       = '#0a0a14'
_WAVE_COL = '#00d4ff'
_RMS_COL  = '#ff6040'
_GRID_COL = '#1a1a2e'
_TEXT_COL = '#88aacc'

_CANVAS_W = 480
_CANVAS_H = 180
_RMS_H    = 18   # height of the RMS strip at the bottom


class TelemetryWaveformPanel(tk.Frame):
    """Oscilloscope waveform and RMS bar, updated from TelemetryFrame.waveform / .rms."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=_BG)

        self._cw     = _CANVAS_W
        self._ch     = _CANVAS_H
        self._plot_h = _CANVAS_H - _RMS_H
        self._mid    = self._plot_h // 2
        self._n_pts  = _CANVAS_W  # one waveform point per pixel column

        canvas = tk.Canvas(self, width=_CANVAS_W, height=_CANVAS_H,
                           bg=_BG, highlightthickness=0)
        canvas.pack(fill=tk.BOTH)
        self._canvas = canvas

        # Static grid lines — created once, never moved.
        for y in (self._mid // 2, self._mid, self._mid + self._mid // 2):
            canvas.create_line(0, y, _CANVAS_W, y, fill=_GRID_COL)
        for x in range(0, _CANVAS_W, _CANVAS_W // 10):
            canvas.create_line(x, 0, x, self._plot_h, fill=_GRID_COL)

        # Waveform polyline: pre-allocated with all points at mid-line.
        flat = []
        for i in range(self._n_pts):
            flat += [i, self._mid]
        self._wave_id = canvas.create_line(*flat, fill=_WAVE_COL, width=1,
                                           smooth=False)

        # RMS bar background + fill — created once, resized each frame.
        self._rms_bg_id  = canvas.create_rectangle(
            0, self._plot_h, _CANVAS_W, _CANVAS_H, fill='#0d0d1a', outline='')
        self._rms_bar_id = canvas.create_rectangle(
            0, self._plot_h, 0, _CANVAS_H, fill=_RMS_COL, outline='')
        self._rms_lbl_id = canvas.create_text(
            4, self._plot_h + _RMS_H // 2,
            text='RMS 0%', anchor='w', fill=_TEXT_COL, font=('Consolas', 8))

    # ── public update API ─────────────────────────────────────────────────────

    def update_from_frame(self, frame) -> None:
        """Refresh display from a TelemetryFrame.  No canvas.delete() called."""
        waveform = np.asarray(frame.waveform, dtype=np.float32)
        rms_val  = float(frame.rms)

        # Downsample waveform to canvas width via slicing.
        n = len(waveform)
        if n >= self._n_pts:
            step    = n // self._n_pts
            samples = waveform[::step][:self._n_pts]
        else:
            samples = waveform

        peak = float(np.abs(samples).max()) if len(samples) else 1.0
        if peak < 1e-9:
            peak = 1.0

        # Build flat coordinate list [x0,y0, x1,y1, ...].
        coords: list = []
        for i, s in enumerate(samples):
            x = i
            y = int(self._mid - (float(s) / peak) * (self._mid - 4))
            coords += [x, y]

        if len(coords) >= 4:
            self._canvas.coords(self._wave_id, *coords)

        # Resize RMS bar.
        rms_norm = min(rms_val * 10.0, 1.0)
        bar_w    = int(rms_norm * self._cw)
        self._canvas.coords(self._rms_bar_id,
                            0, self._plot_h, bar_w, self._ch)
        self._canvas.itemconfig(self._rms_lbl_id,
                                text=f'RMS {rms_norm:.0%}')
