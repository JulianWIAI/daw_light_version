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
from PySide6.QtWidgets import (
    QDockWidget, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox,
)

from .telemetry_waveform_widget   import TelemetryWaveformWidget
from .telemetry_band_widget       import TelemetryBandWidget
from .telemetry_chroma_widget     import TelemetryChromaWidget
from .telemetry_waterfall_widget  import TelemetryWaterfallWidget
from .telemetry_hpss_widget       import TelemetryHpssWidget
from .benchmark_store             import scan_benchmarks, load_benchmark
from .telemetry_noise_gate        import TelemetryNoiseGate
from .telemetry_noise_floor       import TelemetryNoiseFloorCalibrator

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

    # ── Silence-detection RMS thresholds (mirror C++ TelemetryAnalyzer constants) ──
    # EPSILON_RMS : below this the buffer is all-zeros (Python gate closed) — zero frame
    # SILENCE_RMS : genuine ambient noise (-60 dBFS)  — calibrate floor, zero frame
    # SIGNAL_RMS  : active DAW signal    (-50 dBFS)   — subtract floor, full analysis
    _EPSILON_RMS: float = 1e-8
    _SILENCE_RMS: float = 1e-3   # -60 dBFS
    _SIGNAL_RMS:  float = 3.16e-3  # -50 dBFS

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

        # Noise-floor calibrator: accumulates silence-period spectra and
        # subtracts the per-bin floor from active-signal spectra before
        # band/chroma analysis.  n_bins = FFT_SIZE // 2 + 1.
        self._noise_floor = TelemetryNoiseFloorCalibrator(
            n_bins = self._FFT_SIZE // 2 + 1,
        )

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
        # ── RMS for this accumulation window ────────────────────────────────────
        rms = float(np.sqrt(np.mean(self._accum ** 2)))

        # ── FFT magnitude spectrum (raw, before any noise subtraction) ───────────
        spectrum = np.abs(np.fft.rfft(self._accum * self._window)).astype(np.float32)

        # ── Silence detection (mirrors the C++ TelemetryAnalyzer logic) ─────────
        #
        #   rms < EPSILON_RMS  →  all-zeros from Python gate (gate closed)
        #       Publish a zeroed frame; skip noise-floor calibration.
        #
        #   EPSILON_RMS ≤ rms < SILENCE_RMS  →  genuine ambient system noise
        #       Calibrate the noise floor from this spectrum; publish zero frame.
        #
        #   rms ≥ SIGNAL_RMS  →  active DAW signal
        #       Subtract the calibrated floor before band/chroma analysis.
        #
        # The 10 dB gap (SILENCE_RMS → SIGNAL_RMS) provides hysteresis so the
        # display does not flicker on signals near the gate threshold.
        # ─────────────────────────────────────────────────────────────────────────

        is_zeros  = rms < self._EPSILON_RMS
        is_noise  = (not is_zeros) and (rms < self._SILENCE_RMS)
        is_signal = rms >= self._SIGNAL_RMS

        if is_noise:
            # Genuine silence: calibrate per-bin noise floor from this spectrum.
            # Deliberately skipped when is_zeros=True (gate-pushed zeros would
            # corrupt the calibration by driving the floor toward zero).
            self._noise_floor.add_silence_frame(spectrum)

        if is_zeros or is_noise:
            # Publish a zeroed frame — all display panels decay to clean baseline.
            with self._lock:
                self._frame = _PyFrame()
            return

        if not is_signal:
            # Transition band (SILENCE_RMS ≤ rms < SIGNAL_RMS): keep the
            # previous frame visible to avoid flicker; do not recalculate.
            return

        # ── Active signal: subtract noise floor before analysis ──────────────────
        # subtract() is a no-op (returns unchanged copy) until calibration is done.
        clean_spectrum = self._noise_floor.subtract(spectrum)
        mag2 = clean_spectrum ** 2

        # 7 frequency bands (replicates the C++ band_mean / global_mean formula)
        global_mean = float(mag2.mean()) or 1.0
        bands = []
        for lo, hi in self._BANDS_HZ:
            mask = (self._freqs >= lo) & (self._freqs < hi)
            bm   = float(mag2[mask].mean()) if mask.any() else 0.0
            bands.append(min(bm / global_mean, 3.0) / 3.0)

        # 12-bin chroma (vectorised, on denoised spectrum)
        chroma = np.zeros(12, dtype=np.float32)
        np.add.at(chroma, self._chroma_pc, mag2[self._chroma_idx])

        # HPSS scalar indicators (on denoised spectrum)
        self._hpss_q.append(clean_spectrum)
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


# ── Limiter + gain-compensation constants ─────────────────────────────────────

_LIMIT_THRESH   = 0.891   # -1 dBFS brickwall ceiling for telemetry input
_LIMIT_REL      = 0.93    # per-push() release EMA coefficient (~1 s recovery at ~43 calls/s)
_ENERGY_REF     = 0.60    # band mean that defines "100 % Total Energy"
_ENERGY_LIMIT   = 1.20    # ratio above which gain compensation fires (120 %)
_COMP_COOLDOWN  = 60      # minimum poll frames between compensations (~2 s at 30 fps)


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

        # Input limiter state (written from audio thread, read same thread).
        self._limiter_gain: float = 1.0

        # Gain-compensation callback and cooldown counter.
        self._overload_cb   = None
        self._comp_cooldown = 0

        # Noise gate: chunk-level gate applied in push_audio() before the
        # audio reaches the analyzer.  During its CALIBRATING phase all audio
        # passes through unchanged so the downstream analyzer can measure the
        # ambient noise floor.  After calibration, silent chunks become
        # all-zeros so the telemetry display decays to a clean baseline.
        self._noise_gate = TelemetryNoiseGate(sample_rate=self._SAMPLE_RATE)

        # Panel widgets (created once, reused across show/hide cycles).
        self._waveform  = TelemetryWaveformWidget()
        self._bands     = TelemetryBandWidget()
        self._chroma    = TelemetryChromaWidget()
        self._waterfall = TelemetryWaterfallWidget()
        self._hpss      = TelemetryHpssWidget()

        # Benchmark index: display name → pathlib.Path
        self._bench_path_map = {name: path for name, path in scan_benchmarks()}

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

    def _build_bands_container(self) -> QWidget:
        """
        Wrap the Freq Bands widget in a container that adds a "Target Profile"
        combo box above it.  The combo lists all discovered benchmark JSON files;
        selecting one pushes targets+tolerances to both _bands and _hpss.
        """
        _BG  = "#0a0a14"
        _DIM = "#3d5a80"
        _GRN = "#44ffaa"

        container = QWidget()
        container.setStyleSheet(f"background:{_BG};")
        vlay = QVBoxLayout(container)
        vlay.setContentsMargins(4, 4, 4, 2)
        vlay.setSpacing(3)

        # ── Profile selector row ──────────────────────────────────────────────
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(4)

        lbl = QLabel("Target profile:")
        lbl.setStyleSheet(f"color:{_DIM}; font-size:9px; background:transparent;")
        ctrl_row.addWidget(lbl)

        self._profile_combo = QComboBox()
        self._profile_combo.setStyleSheet(
            f"QComboBox {{ background:{_BG}; color:{_GRN}; border:1px solid {_DIM};"
            f" border-radius:3px; font-size:9px; padding:1px 4px; }}"
            f"QComboBox QAbstractItemView {{ background:{_BG}; color:{_GRN};"
            f" selection-background-color:#1a1a40; }}"
        )
        self._profile_combo.addItem("— None —")
        for name in sorted(self._bench_path_map.keys()):
            self._profile_combo.addItem(name)
        self._profile_combo.currentTextChanged.connect(self._on_profile_changed)
        ctrl_row.addWidget(self._profile_combo, stretch=1)

        vlay.addLayout(ctrl_row)
        vlay.addWidget(self._bands)

        return container

    def _on_profile_changed(self, name: str) -> None:
        """Load the selected benchmark and push targets to both telemetry panels."""
        if name == "— None —" or name not in self._bench_path_map:
            self._bands.clear_benchmark()
            self._hpss.clear_benchmark()
            return
        try:
            profile = load_benchmark(self._bench_path_map[name])
            self._bands.set_benchmark(profile.freq_targets, profile.freq_tolerances)
            self._hpss.set_benchmark(profile.hp_ratio_target, profile.hp_ratio_tolerance)
        except Exception as exc:
            logger.warning("benchmark load failed: %s", exc)
            self._bands.clear_benchmark()
            self._hpss.clear_benchmark()

    def setup_docks(self) -> None:
        """Add all five telemetry QDockWidgets to the main window."""
        bands_container = self._build_bands_container()

        specs = [
            ("📈 Waveform",   self._waveform,    Qt.BottomDockWidgetArea),
            ("🎚 Freq Bands", bands_container,   Qt.BottomDockWidgetArea),
            ("🎵 Chroma",     self._chroma,       Qt.BottomDockWidgetArea),
            ("🌊 Waterfall",  self._waterfall,    Qt.RightDockWidgetArea),
            ("⚡ H/P Split",  self._hpss,         Qt.RightDockWidgetArea),
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

    def set_gain_compensation_callback(self, cb) -> None:
        """Register cb(gain_mult: float) — called on the Qt main thread when
        Total Energy exceeds 120 % of the nominal reference level."""
        self._overload_cb = cb

    def push_audio(self, mono: np.ndarray) -> None:
        """Apply input limiter + noise gate, then push a mono float32 chunk to
        the analyzer.

        Signal chain
        ------------
        1. Input limiter (instant attack, ~1 s release, −1 dBFS ceiling).
        2. TelemetryNoiseGate:
               CALIBRATING  → audio passes through so the analyzer builds its
                              spectral noise-floor baseline.
               CLOSED/CLOSING → all-zeros forwarded; the analyzer publishes
                              zeroed frames and all display panels decay to
                              a clean baseline.
               OPEN/OPENING → audio passes through (possibly gain-ramped);
                              the analyzer subtracts the calibrated noise
                              floor before computing bands and chroma.
        3. Push the gated chunk to the analyzer (C++ or Python fallback).

        Audio-thread safe; no heap allocation on the hot path.
        """
        mono = np.asarray(mono, dtype=np.float32)
        if not mono.size:
            return

        # ── 1. Input limiter ───────────────────────────────────────────────────
        peak = float(np.max(np.abs(mono)))
        if peak > _LIMIT_THRESH:
            # Instant attack: snap gain down so no sample exceeds the ceiling.
            self._limiter_gain = min(self._limiter_gain, _LIMIT_THRESH / peak)
        # Exponential release toward unity (every push, not only on peaks).
        self._limiter_gain = _LIMIT_REL * self._limiter_gain + (1.0 - _LIMIT_REL)
        mono = mono * self._limiter_gain

        # ── 2. Noise gate ──────────────────────────────────────────────────────
        # During the CALIBRATING phase the gate returns mono unchanged so the
        # downstream analyzer sees the real ambient noise and can self-calibrate.
        # After calibration, silent chunks are replaced with zeros.
        gated = self._noise_gate.process(mono)

        # ── 3. Push to analyzer ────────────────────────────────────────────────
        try:
            self._analyzer.push(gated)
        except Exception:
            pass

    def reset_noise_gate(self) -> None:
        """Reset the noise gate and noise-floor calibration.

        Call this when the audio device changes, a new project is loaded, or
        the acoustic environment changes significantly (e.g. moving from a
        quiet room to a noisy one) so the gate re-calibrates from scratch.
        """
        self._noise_gate.reset()
        # Reset the Python-fallback noise floor calibrator if active.
        if isinstance(self._analyzer, _TelemetryAnalyzerPy):
            self._analyzer._noise_floor.reset()
        logger.info(
            "TelemetryManager: noise gate and noise-floor calibration reset "
            "(state: %s).", self._noise_gate.state_name
        )

    def shutdown(self) -> None:
        """Stop the polling timer and DSP thread. Call from closeEvent."""
        self._timer.stop()
        try:
            self._analyzer.stop()
        except Exception:
            pass

    # ── Internal 30-FPS polling loop ──────────────────────────────────────────

    def _poll(self) -> None:
        """Query the analyzer once, push the frame to every panel, and run
        the gain-compensation check."""
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

        # ── Total Energy readout + gain compensation ──────────────────────────
        bands = frame.bands
        total_energy = float(np.mean(bands)) if len(bands) else 0.0
        self._bands.set_total_energy(total_energy)

        if self._comp_cooldown > 0:
            self._comp_cooldown -= 1
        elif self._overload_cb is not None:
            energy_ratio = total_energy / _ENERGY_REF
            if energy_ratio > _ENERGY_LIMIT:
                gain_mult = _ENERGY_LIMIT / energy_ratio   # proportional pull-back
                self._overload_cb(gain_mult)
                self._comp_cooldown = _COMP_COOLDOWN
                logger.info(
                    "TelemetryManager: gain compensation %.0f%% applied "
                    "(energy %.0f%% > %.0f%% limit)",
                    gain_mult * 100, energy_ratio * 100, _ENERGY_LIMIT * 100,
                )
