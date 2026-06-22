"""
core/telemetry_manager.py

TelemetryManager — owns the TelemetryAnalyzer, five QDockWidget panels,
and the 30-FPS QTimer polling loop.

Uses the C++ TelemetryAnalyzer when available; falls back to a pure-Python
numpy DSP implementation so telemetry always works even before the C++
extension is compiled.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional

import numpy as np

from PySide6.QtCore import Qt, QTimer, QObject
from PySide6.QtWidgets import QDockWidget, QMainWindow

from .telemetry_waveform_widget   import TelemetryWaveformWidget
from .telemetry_band_widget       import TelemetryBandWidget
from .telemetry_chroma_widget     import TelemetryChromaWidget
from .telemetry_waterfall_widget  import TelemetryWaterfallWidget
from .telemetry_hpss_widget       import TelemetryHpssWidget

logger = logging.getLogger(__name__)

# ── C++ analyzer (optional) ────────────────────────────────────────────────────
try:
    import daw_processors as _dp
    _HAS_ANALYZER = hasattr(_dp, "TelemetryAnalyzer")
except ImportError:
    _HAS_ANALYZER = False


# ── Pure-Python DSP fallback ───────────────────────────────────────────────────

class _PyFrame:
    """Mirrors the C++ TelemetryFrame attribute interface."""
    __slots__ = ("rms", "bands", "chroma", "harmonic", "percussive", "waveform")

    def __init__(self) -> None:
        self.rms:        float      = 0.0
        self.bands:      list       = [0.0] * 7
        self.chroma:     list       = [0.0] * 12
        self.harmonic:   float      = 0.0
        self.percussive: float      = 0.0
        self.waveform               = np.zeros(480, dtype=np.float32)


class _TelemetryAnalyzerPy:
    """
    Pure-Python telemetry DSP.  Implements the same push/get_frame/start/stop
    interface as the C++ TelemetryAnalyzer so TelemetryManager needs no
    special-casing.

    DSP pipeline (runs inline inside push(), no background thread needed):
        accumulate samples → Hanning-windowed FFT →
        7-band energy → 12-bin chroma → HPSS scalars → publish frame
    """

    _FFT_SIZE    = 2048
    _WAVE_POINTS = 480
    _HP_FRAMES   = 8
    _BANDS_HZ    = [
        (20,   60),   # sub-bass
        (60,   250),  # bass
        (250,  500),  # low-mid
        (500,  2000), # mid
        (2000, 4000), # upper-mid
        (4000, 8000), # presence
        (8000, 20000),# air
    ]

    def __init__(self, sample_rate: int = 44100) -> None:
        self._sr     = sample_rate
        self._accum  = np.zeros(self._FFT_SIZE, dtype=np.float32)
        self._pos    = 0
        self._wave   = np.zeros(self._WAVE_POINTS, dtype=np.float32)
        self._window = np.hanning(self._FFT_SIZE).astype(np.float32)
        self._freqs  = np.fft.rfftfreq(self._FFT_SIZE, d=1.0 / sample_rate).astype(np.float32)
        self._hpss_q: deque = deque(maxlen=self._HP_FRAMES)
        self._lock   = threading.Lock()
        self._frame  = _PyFrame()

        # Pre-compute chroma bin mapping (vectorised).
        valid_mask = (self._freqs >= 27.5) & (self._freqs <= 4186.0) & (self._freqs > 0)
        self._chroma_idx = np.where(valid_mask)[0]
        f_valid = self._freqs[self._chroma_idx]
        self._chroma_pc  = (np.round(12.0 * np.log2(f_valid / 440.0) + 57.0)
                            .astype(int) % 12)

    # ── Public interface ───────────────────────────────────────────────────────

    def start(self) -> None: pass
    def stop(self)  -> None: pass

    def push(self, mono: np.ndarray) -> None:
        """Accumulate samples; run DSP pipeline each time the FFT buffer fills."""
        mono = np.asarray(mono, dtype=np.float32)
        n    = len(mono)

        # Rolling waveform — keep the last WAVE_POINTS samples.
        if n >= self._WAVE_POINTS:
            self._wave[:] = mono[-self._WAVE_POINTS:]
        else:
            self._wave = np.roll(self._wave, -n)
            self._wave[-n:] = mono

        # Fill FFT accumulator; process whenever it's full.
        i = 0
        while i < n:
            space = self._FFT_SIZE - self._pos
            take  = min(space, n - i)
            self._accum[self._pos:self._pos + take] = mono[i:i + take]
            self._pos += take
            i         += take
            if self._pos >= self._FFT_SIZE:
                self._process()
                self._pos = 0

    def get_frame(self) -> _PyFrame:
        with self._lock:
            return self._frame

    # ── Internal DSP ──────────────────────────────────────────────────────────

    def _process(self) -> None:
        # RMS
        rms = float(np.sqrt(np.mean(self._accum ** 2)))

        # FFT magnitude²
        spectrum = np.abs(np.fft.rfft(self._accum * self._window)).astype(np.float32)
        mag2     = spectrum ** 2

        # 7 frequency bands (replicates the C++ band_mean / global_mean formula)
        global_mean = float(mag2.mean()) or 1.0
        bands = []
        for lo, hi in self._BANDS_HZ:
            mask = (self._freqs >= lo) & (self._freqs < hi)
            bm   = float(mag2[mask].mean()) if mask.any() else 0.0
            bands.append(min(bm / global_mean, 3.0) / 3.0)

        # 12-bin chroma (vectorised)
        chroma = np.zeros(12, dtype=np.float32)
        np.add.at(chroma, self._chroma_pc, mag2[self._chroma_idx])

        # HPSS scalar indicators
        self._hpss_q.append(spectrum)
        if len(self._hpss_q) >= 2:
            stacked = np.array(list(self._hpss_q))   # shape: (frames, bins)
            s_max   = stacked.max() or 1.0
            s_norm  = stacked / s_max
            # harmonic:   mean of per-frame medians (replicates C++ axis=1 median)
            harm = float(np.median(s_norm, axis=1).mean())
            # percussive: mean of per-bin medians (replicates C++ axis=0 median)
            perc = float(np.median(s_norm, axis=0).mean())
            total = harm + perc or 1.0
            harm  /= total
            perc  /= total
        else:
            harm = perc = 0.0

        frame            = _PyFrame()
        frame.rms        = rms
        frame.bands      = bands
        frame.chroma     = chroma.tolist()
        frame.harmonic   = harm
        frame.percussive = perc
        frame.waveform   = self._wave.copy()

        with self._lock:
            self._frame = frame


# ── TelemetryManager ──────────────────────────────────────────────────────────

class TelemetryManager(QObject):
    """
    Manages the lifecycle of the TelemetryAnalyzer and all five
    telemetry QDockWidgets.

    Uses the C++ TelemetryAnalyzer when available, falls back to the
    pure-Python _TelemetryAnalyzerPy so telemetry always works.

    - All docks start hidden; call toggle() to show/hide as a group.
    - Each dock is individually movable, floatable, and closable.
    - The 30-FPS QTimer only runs while the panels are visible.
    """

    _SAMPLE_RATE = 44100

    def __init__(self, main_window: QMainWindow) -> None:
        super().__init__(main_window)
        self._window  = main_window
        self._visible = False

        # Panel widgets (created once, reused across show/hide cycles).
        self._waveform  = TelemetryWaveformWidget()
        self._bands     = TelemetryBandWidget()
        self._chroma    = TelemetryChromaWidget()
        self._waterfall = TelemetryWaterfallWidget()
        self._hpss      = TelemetryHpssWidget()

        self._docks: list = []

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._poll)

        # Try C++ analyzer first, fall back to Python implementation.
        self._analyzer = None
        if _HAS_ANALYZER:
            try:
                self._analyzer = _dp.TelemetryAnalyzer(self._SAMPLE_RATE)
                self._analyzer.start()
                logger.info("TelemetryManager: C++ TelemetryAnalyzer started.")
            except Exception as exc:
                logger.warning("TelemetryManager: C++ analyzer failed (%s); using Python fallback.", exc)
                self._analyzer = None

        if self._analyzer is None:
            self._analyzer = _TelemetryAnalyzerPy(self._SAMPLE_RATE)
            self._analyzer.start()
            logger.info("TelemetryManager: Python DSP fallback active.")

    # ── Public API ────────────────────────────────────────────────────────────

    def setup_docks(self) -> None:
        """Add all five telemetry QDockWidgets to the main window."""
        specs = [
            ("📈 Waveform",   self._waveform,  Qt.BottomDockWidgetArea),
            ("🎚 Freq Bands", self._bands,      Qt.BottomDockWidgetArea),
            ("🎵 Chroma",     self._chroma,     Qt.BottomDockWidgetArea),
            ("🌊 Waterfall",  self._waterfall,  Qt.RightDockWidgetArea),
            ("⚡ H/P Split",  self._hpss,       Qt.RightDockWidgetArea),
        ]
        prev_bottom_dock = None
        for title, widget, area in specs:
            dock = QDockWidget(title, self._window)
            dock.setWidget(widget)
            dock.setAllowedAreas(Qt.AllDockWidgetAreas)
            dock.setFeatures(
                QDockWidget.DockWidgetMovable   |
                QDockWidget.DockWidgetFloatable |
                QDockWidget.DockWidgetClosable
            )
            self._window.addDockWidget(area, dock)
            dock.hide()
            if area == Qt.BottomDockWidgetArea and prev_bottom_dock is not None:
                self._window.tabifyDockWidget(prev_bottom_dock, dock)
            if area == Qt.BottomDockWidgetArea:
                prev_bottom_dock = dock
            self._docks.append(dock)

        logger.debug("TelemetryManager: %d dock widgets created.", len(self._docks))

    def toggle(self) -> None:
        """Show all hidden docks, or hide all visible ones."""
        if self._visible:
            for dock in self._docks:
                dock.hide()
            self._timer.stop()
            self._visible = False
        else:
            for dock in self._docks:
                dock.show()
                dock.raise_()
            self._timer.start()   # always runs — analyzer is never None
            self._visible = True

    def push_audio(self, mono: np.ndarray) -> None:
        """Push a mono float32 chunk to the analyzer (audio-thread safe)."""
        try:
            self._analyzer.push(mono)
        except Exception:
            pass

    def shutdown(self) -> None:
        """Stop the polling timer and DSP thread. Call from closeEvent."""
        self._timer.stop()
        try:
            self._analyzer.stop()
        except Exception:
            pass

    # ── Internal 30-FPS polling loop ──────────────────────────────────────────

    def _poll(self) -> None:
        """Query the analyzer once and push the frame to every panel."""
        try:
            frame = self._analyzer.get_frame()
        except Exception as exc:
            logger.debug("TelemetryManager._poll: %s", exc)
            return

        self._waveform .update_frame(frame)
        self._bands    .update_frame(frame)
        self._chroma   .update_frame(frame)
        self._waterfall.update_frame(frame)
        self._hpss     .update_frame(frame)
