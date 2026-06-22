"""
core/telemetry_dashboard.py

TelemetryDashboard — the single polling container that drives all five
telemetry panels at 30 FPS from the C++ TelemetryAnalyzer.

Architecture:
  - One self.after(33, self._update) loop on the Tk main thread.
  - Inside _update: one call to analyzer.get_frame() (GIL released in C++,
    always instant — no DSP on this thread).
  - Each panel receives the frame via update_from_frame().  No canvas.delete()
    is ever called inside panel updates — all drawing uses pre-allocated IDs.

Usage:
    # Wire up after building your AudioEngine:
    dashboard = TelemetryDashboard(root, analyzer=engine.telemetry_analyzer)
    dashboard.show()
    # From the audio callback / master-bus render:
    engine.telemetry_analyzer.push(mono_chunk)
"""

from __future__ import annotations

from typing import Optional

import tkinter as tk
from tkinter import ttk

try:
    import daw_processors as _dp
    _HAS_CPP = hasattr(_dp, 'TelemetryAnalyzer')
except ImportError:
    _HAS_CPP = False

from core.telemetry_waveform_panel  import TelemetryWaveformPanel
from core.telemetry_band_panel      import TelemetryBandPanel
from core.telemetry_chroma_panel    import TelemetryChromaPanel
from core.telemetry_waterfall_panel import TelemetryWaterfallPanel
from core.telemetry_hpss_panel      import TelemetryHpssPanel

_BG        = '#0a0a14'
_TITLE_BG  = '#111126'
_TEXT_COL  = '#88aacc'
_FPS       = 30
_FRAME_MS  = 1000 // _FPS   # ~33 ms per display tick


class TelemetryDashboard(tk.Toplevel):
    """Floating dashboard window containing all five telemetry panels.

    Polls the C++ TelemetryAnalyzer at 30 FPS.  All five panels are refreshed
    from a single get_frame() call — the Python UI thread does zero DSP.
    """

    def __init__(self, parent: tk.Misc,
                 analyzer: Optional[object] = None) -> None:
        """
        Parameters
        ----------
        parent:
            Tk parent widget (usually the main application window).
        analyzer:
            A daw_processors.TelemetryAnalyzer instance.  If None, the
            dashboard opens but displays zeros until set via attach_analyzer().
        """
        super().__init__(parent)
        self.title('Telemetry Dashboard')
        self.resizable(False, False)
        self.configure(bg=_BG)
        self.protocol('WM_DELETE_WINDOW', self.hide)

        self._analyzer = analyzer
        self._running  = False

        self._build_ui()

    # ── public API ────────────────────────────────────────────────────────────

    def attach_analyzer(self, analyzer: object) -> None:
        """Attach or replace the C++ analyzer at any time."""
        self._analyzer = analyzer

    def show(self) -> None:
        """Make the window visible and start the polling loop."""
        self.deiconify()
        if not self._running:
            self._running = True
            self.after(_FRAME_MS, self._update)

    def hide(self) -> None:
        """Hide the window and pause the polling loop."""
        self._running = False
        self.withdraw()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Lay out panels in a 2-row grid inside the Toplevel."""
        # Title bar.
        tk.Label(self, text='  Telemetry  ', bg=_TITLE_BG,
                 fg=_TEXT_COL, font=('Consolas', 9, 'bold'),
                 anchor='w').grid(row=0, column=0, columnspan=2,
                                  sticky='ew', padx=0, pady=0)

        # Row 1: Waveform (left, wider) + HPSS (right).
        self._waveform_panel = TelemetryWaveformPanel(self)
        self._waveform_panel.grid(row=1, column=0, padx=4, pady=4, sticky='nsew')

        self._hpss_panel = TelemetryHpssPanel(self)
        self._hpss_panel.grid(row=1, column=1, padx=4, pady=4, sticky='nsew')

        # Row 2: Frequency bands + Chroma histogram + Waterfall.
        bottom_frame = tk.Frame(self, bg=_BG)
        bottom_frame.grid(row=2, column=0, columnspan=2, padx=4, pady=4)

        self._band_panel = TelemetryBandPanel(bottom_frame)
        self._band_panel.pack(side=tk.LEFT, padx=(0, 6))

        self._chroma_panel = TelemetryChromaPanel(bottom_frame)
        self._chroma_panel.pack(side=tk.LEFT, padx=(0, 6))

        self._waterfall_panel = TelemetryWaterfallPanel(bottom_frame)
        self._waterfall_panel.pack(side=tk.LEFT)

        # Status bar at the bottom.
        self._status_var = tk.StringVar(value='No analyzer attached.')
        tk.Label(self, textvariable=self._status_var,
                 bg=_TITLE_BG, fg=_TEXT_COL,
                 font=('Consolas', 8), anchor='w').grid(
            row=3, column=0, columnspan=2, sticky='ew')

    # ── 30-FPS polling loop ───────────────────────────────────────────────────

    def _update(self) -> None:
        """Single polling tick: query C++, delegate to each panel, reschedule."""
        if not self._running:
            return  # loop was paused via hide()

        if self._analyzer is not None:
            # get_frame() releases the GIL in C++ — always instant.
            frame = self._analyzer.get_frame()

            # Dispatch the same frame to all panels — zero extra DSP.
            self._waveform_panel.update_from_frame(frame)
            self._hpss_panel.update_from_frame(frame)
            self._band_panel.update_from_frame(frame)
            self._chroma_panel.update_from_frame(frame)
            self._waterfall_panel.update_from_frame(frame)

            self._status_var.set(
                f'tick #{frame.tick}  |  RMS {frame.rms:.3f}  '
                f'|  H {frame.harmonic:.0%}  P {frame.percussive:.0%}')
        else:
            self._status_var.set('No analyzer attached — call attach_analyzer().')

        # Reschedule at fixed interval.
        self.after(_FRAME_MS, self._update)
