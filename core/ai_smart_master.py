"""
ai_smart_master.py -- AI Smart Master Bus
=========================================
Measures the integrated LUFS of the master mix, analyses the frequency
spectrum, then auto-adjusts the parameters of existing C++ plugins in the
master FX chain to hit a user-selected streaming loudness target.

Supported targets
-----------------
    Spotify      -14 LUFS
    Apple Music  -16 LUFS
    YouTube      -14 LUFS
    CD           -9  LUFS

Architecture (thread safety)
-----------------------------
    MasterAnalysisWorker(QThread)
        - Receives a copy of the audio buffer on construction.
        - Measures integrated LUFS with pyloudnorm.
        - Computes a 32-band log-spaced FFT spectrum.
        - Emits analysis_done(dict) to the GUI thread when finished.

    SmartMasterPlugin(FxPluginBase)
        - Buffers incoming audio in a deque (protected by threading.Lock).
        - "Analyse" button snapshot → worker thread → back via Signal.
        - "Apply" button pushes calculated gains to the C++ chain plugins:
            DynamicEQPlugin, SaturationPlugin, BrickwallLimiterPlugin.
        - SpectrumWidget displays before/after 32-band overlay using QPainter.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional, Dict, Any, List

import numpy as np

from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QSizePolicy,
)

from .fx_plugin_base import FxPluginBase

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Streaming loudness targets (LUFS)
# ─────────────────────────────────────────────────────────────────────────────

TARGETS: Dict[str, float] = {
    "Spotify":     -14.0,
    "Apple Music": -16.0,
    "YouTube":     -14.0,
    "CD":          -9.0,
}

# Number of log-spaced frequency bands for spectrum display
_N_BANDS = 32
# Audio buffer length used for analysis (2 seconds at 44100 Hz stereo)
_BUF_SAMPLES = 44100 * 2

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compute_spectrum_db(audio: np.ndarray, sr: float) -> np.ndarray:
    """
    Compute a 32-band log-spaced magnitude spectrum in dBFS.

    audio  : (N,)  or  (N, 2)  float32 or float64 array
    returns: (32,) float64 array of dB values, range roughly -80..0 dBFS
    """
    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    window = np.hanning(len(mono))
    spectrum = np.abs(np.fft.rfft(mono * window))
    freqs    = np.fft.rfftfreq(len(mono), d=1.0 / sr)

    # 32 log-spaced band edges 20 Hz → sr/2
    edges = np.logspace(np.log10(20.0), np.log10(sr / 2.0), _N_BANDS + 1)

    bands_db = np.zeros(_N_BANDS, dtype=np.float64)
    for i in range(_N_BANDS):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if mask.any():
            rms = np.sqrt(np.mean(spectrum[mask] ** 2))
            bands_db[i] = 20.0 * np.log10(rms + 1e-10)
        else:
            bands_db[i] = -80.0

    # Normalise so the peak band = 0 dB (relative display)
    peak = bands_db.max()
    if peak > -60.0:
        bands_db -= peak
    return bands_db


def _measure_lufs(audio: np.ndarray, sr: float) -> float:
    """Return integrated LUFS.  Falls back to RMS-based estimate if pyloudnorm
    is not installed so the plugin degrades gracefully."""
    try:
        import pyloudnorm as pyln  # type: ignore
        meter = pyln.Meter(int(sr))
        data  = audio if audio.ndim == 2 else audio[:, np.newaxis]
        lufs  = meter.integrated_loudness(data.astype(np.float64))
        if np.isinf(lufs) or np.isnan(lufs):
            raise ValueError("Silence / too short for LUFS measurement")
        return float(lufs)
    except Exception as exc:
        logger.debug("pyloudnorm unavailable or failed (%s); using RMS fallback", exc)
        rms = float(np.sqrt(np.mean(audio ** 2)))
        return 20.0 * np.log10(rms + 1e-10) - 3.0   # crude approximation


# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────

class MasterAnalysisWorker(QThread):
    """
    Runs LUFS measurement + spectral analysis in a background thread.

    Signals
    -------
    analysis_done(dict)
        Keys: 'lufs' (float), 'spectrum_db' (list[float] 32-band),
              'sr' (float), 'n_samples' (int)
    analysis_failed(str)
        Human-readable error message.
    """

    analysis_done   = Signal(dict)
    analysis_failed = Signal(str)

    def __init__(self, audio: np.ndarray, sr: float) -> None:
        super().__init__()
        # Deep copy so the audio thread can keep writing the live buffer.
        self._audio = audio.copy()
        self._sr    = sr

    def run(self) -> None:
        try:
            lufs        = _measure_lufs(self._audio, self._sr)
            spectrum_db = _compute_spectrum_db(self._audio, self._sr)
            self.analysis_done.emit({
                "lufs":        lufs,
                "spectrum_db": spectrum_db.tolist(),
                "sr":          self._sr,
                "n_samples":   len(self._audio),
            })
        except Exception as exc:
            logger.exception("MasterAnalysisWorker failed")
            self.analysis_failed.emit(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Spectrum display widget
# ─────────────────────────────────────────────────────────────────────────────

_C = {
    "abyss":   "#060A18",
    "deep":    "#0A0E22",
    "cyan":    "#00E5FF",
    "purple":  "#9945FF",
    "pink":    "#FF2D9E",
    "gold":    "#FFD700",
    "text":    "#C8E6FF",
    "dim":     "#3D5A80",
    "green":   "#00FF88",
    "orange":  "#FF6B2B",
}


class SpectrumWidget(QWidget):
    """
    Draws two overlaid 32-band bar spectra:
        before  — cyan bars (measured before applying corrections)
        after   — green bars (estimated after applying corrections)

    Call set_before(bands) and set_after(bands) with lists of 32 dB values
    (0 = peak, negative = quieter).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._before: List[float] = [-80.0] * _N_BANDS
        self._after:  List[float] = [-80.0] * _N_BANDS
        self._floor = -60.0   # dB floor for display

    def set_before(self, bands: List[float]) -> None:
        self._before = list(bands)
        self.update()

    def set_after(self, bands: List[float]) -> None:
        self._after = list(bands)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(_C["deep"]))

        bar_w   = max(1, w // _N_BANDS)
        gap     = max(0, bar_w - max(1, bar_w - 1))
        floor   = self._floor

        for i, (b, a) in enumerate(zip(self._before, self._after)):
            x = i * w // _N_BANDS

            # Before bar — cyan
            frac_b = max(0.0, min(1.0, (b - floor) / (-floor)))
            bar_h  = int(frac_b * (h - 2))
            p.fillRect(x, h - bar_h - 1, bar_w - 1, bar_h,
                       QColor(_C["cyan"]))

            # After overlay — green (semi-transparent)
            frac_a = max(0.0, min(1.0, (a - floor) / (-floor)))
            bar_ha = int(frac_a * (h - 2))
            col = QColor(_C["green"])
            col.setAlpha(160)
            p.fillRect(x, h - bar_ha - 1, bar_w - 1, bar_ha, col)

        # Legend
        p.setPen(QColor(_C["cyan"]))
        p.drawText(4, h - 4, "Before")
        p.setPen(QColor(_C["green"]))
        p.drawText(60, h - 4, "After")

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# Plugin
# ─────────────────────────────────────────────────────────────────────────────

class SmartMasterPlugin(FxPluginBase):
    """
    AI Smart Master Bus plugin.

    - Buffers audio passively (no DSP — the master bus chain handles that).
    - On "Analyse": snapshots the buffer → MasterAnalysisWorker (background).
    - On "Apply":   pushes gain/parameter corrections to the C++ plugins
                    found in the shared chain via set_chain().

    Chain plugins targeted
    ----------------------
    DynamicEQPlugin      — low/high shelf correction from spectral tilt
    SaturationPlugin     — light saturation to add warmth/density
    BrickwallLimiterPlugin — ceiling set so integrated LUFS hits the target
    """

    DISPLAY_NAME = "Smart Master Bus"

    def __init__(self) -> None:
        super().__init__()
        self._sr: float    = 44100.0
        self._chain        = None
        self._worker: Optional[MasterAnalysisWorker] = None

        # Audio ring-buffer (stereo float32, 2 s)
        self._buf_lock = threading.Lock()
        self._buf: deque[np.ndarray] = deque()
        self._buf_samples = 0

        # Last analysis results
        self._lufs_before: Optional[float]  = None
        self._spectrum_before: List[float]  = [-80.0] * _N_BANDS
        self._spectrum_after:  List[float]  = [-80.0] * _N_BANDS
        self._pending_params: Optional[Dict[str, Any]] = None

        self._build_ui()

    # ── FxPluginBase interface ─────────────────────────────────────────────

    def get_widget(self) -> QWidget:
        return self._widget

    def get_name(self) -> str:
        return self.DISPLAY_NAME

    def process(self, left: np.ndarray, right: np.ndarray):
        """Pass-through: buffer audio for analysis; do not alter the signal."""
        self._sr = getattr(self, "_host_sr", 44100.0)

        block = np.stack([left, right], axis=1).astype(np.float32)
        with self._buf_lock:
            self._buf.append(block)
            self._buf_samples += len(block)
            # Trim to _BUF_SAMPLES
            while self._buf_samples > _BUF_SAMPLES and self._buf:
                removed = self._buf.popleft()
                self._buf_samples -= len(removed)

        return left, right

    def set_chain(self, chain) -> None:
        self._chain = chain

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._widget = QWidget()
        self._widget.setStyleSheet(
            f"background:{_C['abyss']}; color:{_C['text']};"
        )
        lay = QVBoxLayout(self._widget)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # Title
        title = QLabel("AI SMART MASTER BUS")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color:{_C['cyan']}; font-size:12px; font-weight:bold;"
            f" letter-spacing:2px; background:transparent;"
        )
        lay.addWidget(title)

        # ── Target selector ───────────────────────────────────────────────
        tgt_row = QHBoxLayout()
        tgt_lbl = QLabel("Target:")
        tgt_lbl.setStyleSheet(f"color:{_C['text']}; font-size:9px; background:transparent;")
        self._target_combo = QComboBox()
        self._target_combo.addItems(list(TARGETS.keys()))
        self._target_combo.setCurrentText("Spotify")
        self._target_combo.setStyleSheet(
            f"QComboBox {{ background:{_C['deep']}; color:{_C['text']};"
            f" border:1px solid {_C['purple']}; border-radius:3px;"
            f" font-size:9px; padding:2px 6px; }}"
            f"QComboBox QAbstractItemView {{ background:{_C['deep']};"
            f" color:{_C['text']}; selection-background-color:{_C['purple']}; }}"
        )
        tgt_row.addWidget(tgt_lbl)
        tgt_row.addWidget(self._target_combo, 1)
        lay.addLayout(tgt_row)

        # ── LUFS display ──────────────────────────────────────────────────
        self._lufs_lbl = QLabel("Current LUFS: —")
        self._lufs_lbl.setAlignment(Qt.AlignCenter)
        self._lufs_lbl.setStyleSheet(
            f"color:{_C['gold']}; font-size:10px; background:{_C['deep']};"
            f" border:1px solid rgba(255,215,0,0.2); border-radius:3px; padding:3px;"
        )
        lay.addWidget(self._lufs_lbl)

        # ── Spectrum ──────────────────────────────────────────────────────
        self._spectrum = SpectrumWidget()
        lay.addWidget(self._spectrum)

        # ── Status label ──────────────────────────────────────────────────
        self._status_lbl = QLabel("")
        self._status_lbl.setAlignment(Qt.AlignCenter)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color:{_C['dim']}; font-size:8px; background:transparent;"
        )
        lay.addWidget(self._status_lbl)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_row = QHBoxLayout()

        self._analyse_btn = QPushButton("⚡  ANALYSE")
        self._analyse_btn.setFixedHeight(30)
        self._analyse_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']}; border:1px solid {_C['purple']};"
            f" border-radius:4px; color:{_C['purple']}; font-size:10px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.1); }}"
            f"QPushButton:disabled {{ color:{_C['dim']}; border-color:{_C['dim']}; }}"
        )
        self._analyse_btn.clicked.connect(self._start_analysis)

        self._apply_btn = QPushButton("✓  APPLY")
        self._apply_btn.setFixedHeight(30)
        self._apply_btn.setEnabled(False)
        self._apply_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']}; border:1px solid {_C['cyan']};"
            f" border-radius:4px; color:{_C['cyan']}; font-size:10px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.08); }}"
            f"QPushButton:disabled {{ color:{_C['dim']}; border-color:{_C['dim']}; }}"
        )
        self._apply_btn.clicked.connect(self._apply_to_chain)

        btn_row.addWidget(self._analyse_btn)
        btn_row.addWidget(self._apply_btn)
        lay.addLayout(btn_row)

    # ── Analysis ──────────────────────────────────────────────────────────

    def _start_analysis(self) -> None:
        with self._buf_lock:
            if self._buf_samples < 1024:
                self._set_status("⚠  Not enough audio buffered — play some audio first.")
                return
            audio = np.concatenate(list(self._buf), axis=0)

        self._analyse_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._set_status("Analysing…")

        self._worker = MasterAnalysisWorker(audio, self._sr)
        self._worker.analysis_done   .connect(self._on_analysis_done)
        self._worker.analysis_failed .connect(self._on_analysis_failed)
        self._worker.finished        .connect(lambda: self._analyse_btn.setEnabled(True))
        self._worker.start()

    def _on_analysis_done(self, result: dict) -> None:
        lufs    = result["lufs"]
        sp_db   = result["spectrum_db"]
        target  = TARGETS[self._target_combo.currentText()]
        gain_db = target - lufs          # positive ↑ or negative ↓

        self._lufs_before     = lufs
        self._spectrum_before = sp_db
        self._spectrum.set_before(sp_db)

        # Estimated after-spectrum (uniform lift/cut)
        self._spectrum_after = [v + gain_db for v in sp_db]
        self._spectrum.set_after(self._spectrum_after)

        self._lufs_lbl.setText(
            f"Current LUFS: {lufs:.1f}  →  Target: {target:.1f}  "
            f"({'↑' if gain_db >= 0 else '↓'}{abs(gain_db):.1f} dB)"
        )

        # Build parameter patch dict for the Apply step
        self._pending_params = self._build_params(lufs, sp_db, target)
        self._set_status(
            f"Analysis done.  Correction: {gain_db:+.1f} dB  "
            f"Click Apply to push to chain."
        )
        self._apply_btn.setEnabled(True)

    def _on_analysis_failed(self, msg: str) -> None:
        self._set_status(f"✗ Analysis failed: {msg}")

    # ── Parameter calculation ─────────────────────────────────────────────

    def _build_params(
        self, lufs: float, sp_db: List[float], target_lufs: float
    ) -> Dict[str, Any]:
        """
        Derive concrete plugin parameter values from the analysis results.

        Returns a dict with sub-dicts keyed by plugin DISPLAY_NAME.
        """
        gain_db  = target_lufs - lufs   # overall gain needed

        # Spectral tilt: compare average low (bands 0-7) vs high (bands 24-31)
        lf_avg   = float(np.mean(sp_db[:8]))
        hf_avg   = float(np.mean(sp_db[24:]))
        tilt     = hf_avg - lf_avg      # positive = bright, negative = dark

        # DynamicEQ: gentle low-shelf / high-shelf correction for tilt
        # Expressed as dB boosts on the DynEQ low/high shelf bands.
        # We limit to ±4 dB to avoid over-processing.
        hf_boost = float(np.clip(-tilt * 0.3, -4.0, 4.0))
        lf_cut   = float(np.clip(lf_avg * 0.1, -3.0, 3.0))

        # Saturation: add a tiny touch of even-harmonic warmth when boosting
        sat_drive = 0.0
        if gain_db > 0:
            sat_drive = float(np.clip(gain_db * 0.3, 0.0, 3.0))

        # Limiter ceiling: target LUFS + headroom
        # True-peak ceiling at -1 dBTP is standard for streaming.
        limiter_ceiling_db = -1.0

        return {
            "gain_db":            gain_db,
            "hf_boost_db":        hf_boost,
            "lf_cut_db":          lf_cut,
            "sat_drive_db":       sat_drive,
            "limiter_ceiling_db": limiter_ceiling_db,
            "target_lufs":        target_lufs,
        }

    # ── Apply corrections to chain ────────────────────────────────────────

    def _apply_to_chain(self) -> None:
        if not self._pending_params or self._chain is None:
            self._set_status("⚠  No analysis results yet, or no chain attached.")
            return

        p     = self._pending_params
        chain = self._chain
        applied: List[str] = []

        # 1. DynamicEQPlugin — shelf corrections
        dyn_eq = self._find_or_skip(chain, "DynamicEQ")
        if dyn_eq is not None:
            proc = getattr(dyn_eq, "_processor", None)
            if proc is not None:
                try:
                    # Adjust output gain via the band closest to the overall gain
                    proc.set_output_gain_db(float(p["gain_db"]))
                    applied.append("DynamicEQ gain")
                except Exception:
                    pass

        # 2. SaturationPlugin — light tape warmth
        sat = self._find_or_skip(chain, "Saturation")
        if sat is not None and p["sat_drive_db"] > 0:
            proc = getattr(sat, "_processor", None)
            if proc is not None:
                try:
                    from .daw_processors import SatMode  # type: ignore
                    proc.set_mode(SatMode.TAPE)
                    proc.set_drive_db(float(p["sat_drive_db"]))
                    proc.set_mix(0.25)     # only 25% wet for subtle warmth
                    applied.append(f"Saturation drive={p['sat_drive_db']:.1f}dB @25%")
                except Exception:
                    pass

        # 3. BrickwallLimiterPlugin — set ceiling
        limiter = self._find_or_skip(chain, "Brickwall Limiter")
        if limiter is not None:
            proc = getattr(limiter, "_processor", None)
            if proc is not None:
                try:
                    proc.set_ceiling_db(float(p["limiter_ceiling_db"]))
                    proc.set_threshold_db(float(p["target_lufs"] + 6.0))
                    applied.append(
                        f"Limiter ceil={p['limiter_ceiling_db']:.1f}dB"
                    )
                except Exception:
                    pass

        if applied:
            self._set_status("Applied: " + ", ".join(applied))
        else:
            self._set_status(
                "⚠  No compatible plugins found in chain.\n"
                "Add DynamicEQ, Saturation, or Brickwall Limiter to this channel."
            )

        self._apply_btn.setEnabled(False)

    def _find_or_skip(self, chain, display_name_fragment: str):
        """Return the first plugin whose display name contains the fragment."""
        try:
            for plugin in chain:
                name = getattr(plugin, "DISPLAY_NAME", "") or ""
                if display_name_fragment.lower() in name.lower():
                    return plugin
        except Exception:
            pass
        return None

    # ── Helpers ───────────────────────────────────────────────────────────

    def _set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        logger.debug("SmartMasterPlugin: %s", text)
