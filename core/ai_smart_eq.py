"""
ai_smart_eq.py -- Smart EQ / Compressor AI Plugin
==================================================
Analyses the audio currently playing on a track using librosa and scikit-learn,
classifies it (Vocal / Drums / Bass / Guitar / Synth / Piano), then auto-generates
an optimal DynamicEQ + MultibandCompressor preset for that track type.

Architecture (critical for audio thread safety):
    - SmartEQPlugin.process() runs on the DAW background render thread.
      It silently copies a short audio snapshot into a ring buffer and returns
      the audio UNCHANGED.  No AI code runs on the audio thread.

    - When the user clicks "Analyse", a SmartEQWorker QThread is spawned.
      It reads the snapshot, runs librosa + sklearn, and emits analysis_done
      (a Python dict) back onto the GUI thread via Qt's Signal/Slot mechanism.

    - When the user clicks "Apply to Chain", the plugin finds or creates
      DynamicEQPlugin and MultibandCompressorPlugin instances in the chain
      and calls their _apply_params() to push the new settings to the live
      C++ processors.  This is safe because it only modifies Python-level
      parameter attributes + calls pybind11 setters (which are GIL-held).

Dependencies (all optional — plugin degrades gracefully if absent):
    pip install librosa scikit-learn soundfile
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional, Dict, Any, List

import numpy as np

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QScrollArea, QFrame,
)

from .fx_plugin_base import FxPluginBase

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Optional dependency guard
# ─────────────────────────────────────────────────────────────────────────────

try:
    import librosa
    _LIBROSA_OK = True
except ImportError:
    _LIBROSA_OK = False
    logger.warning("librosa not found. pip install librosa")

try:
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.preprocessing import StandardScaler
    _SKLEARN_OK = True
except ImportError:
    _SKLEARN_OK = False
    logger.warning("scikit-learn not found. pip install scikit-learn")

# ─────────────────────────────────────────────────────────────────────────────
# Hard-coded training data for the KNN classifier
# Each row: [spectral_centroid, spectral_flatness, rms, zcr, lf_energy_ratio]
# These are representative normalised values for each instrument class.
# ─────────────────────────────────────────────────────────────────────────────

_TRAIN_X = np.array([
    # Vocal  (bright, non-flat, mid RMS, mid ZCR, low LF ratio)
    [2500, 0.04, 0.08, 0.07, 0.30], [2800, 0.05, 0.07, 0.08, 0.28],
    [2300, 0.06, 0.09, 0.06, 0.32], [3000, 0.04, 0.06, 0.09, 0.25],
    [2600, 0.05, 0.08, 0.07, 0.31],
    # Drums  (very flat, high ZCR, high transient RMS, mixed LF)
    [3500, 0.22, 0.14, 0.16, 0.45], [3200, 0.25, 0.16, 0.18, 0.42],
    [4000, 0.20, 0.12, 0.14, 0.48], [3800, 0.24, 0.15, 0.15, 0.44],
    [3300, 0.21, 0.13, 0.17, 0.46],
    # Bass   (low centroid, low flatness, high LF)
    [380,  0.02, 0.13, 0.02, 0.82], [420,  0.03, 0.11, 0.02, 0.80],
    [350,  0.02, 0.14, 0.01, 0.85], [450,  0.03, 0.12, 0.02, 0.78],
    [400,  0.02, 0.10, 0.02, 0.83],
    # Guitar (mid centroid, somewhat flat, low-mid LF)
    [2000, 0.14, 0.06, 0.06, 0.22], [1800, 0.13, 0.07, 0.07, 0.24],
    [2200, 0.15, 0.05, 0.05, 0.20], [1900, 0.12, 0.06, 0.06, 0.23],
    [2100, 0.14, 0.07, 0.07, 0.21],
    # Synth  (varied centroid, very flat/noise-like)
    [3000, 0.38, 0.05, 0.05, 0.20], [2800, 0.42, 0.06, 0.04, 0.18],
    [3200, 0.36, 0.04, 0.05, 0.22], [2600, 0.40, 0.05, 0.06, 0.19],
    [3100, 0.39, 0.05, 0.05, 0.21],
    # Piano  (mid centroid, low flatness, moderate LF)
    [1500, 0.07, 0.07, 0.04, 0.36], [1600, 0.08, 0.08, 0.04, 0.34],
    [1400, 0.06, 0.06, 0.05, 0.38], [1700, 0.08, 0.07, 0.03, 0.35],
    [1550, 0.07, 0.07, 0.04, 0.37],
], dtype=np.float32)

_TRAIN_Y = (
    ["Vocal"] * 5 + ["Drums"] * 5 + ["Bass"] * 5 +
    ["Guitar"] * 5 + ["Synth"] * 5 + ["Piano"] * 5
)

# ─────────────────────────────────────────────────────────────────────────────
# EQ + Compressor presets per track type
# eq_bands: list of (freq_hz, gain_db, q, static_gain_db)
# comp: dict with threshold_db, ratio, attack_ms, release_ms
# ─────────────────────────────────────────────────────────────────────────────

_PRESETS: Dict[str, Dict[str, Any]] = {
    "Vocal": {
        "eq_bands": [
            (80,   -24.0, 0.7, -3.0),   # high-pass roll-off
            (200,   -2.0, 1.5,  0.0),   # mud cut
            (3000,  +2.0, 1.5,  0.0),   # presence boost
            (8000,  +1.5, 1.0,  0.0),   # air
        ],
        "comp": {"threshold_db": -18.0, "ratio": 3.0,
                 "attack_ms": 10.0, "release_ms": 80.0},
    },
    "Drums": {
        "eq_bands": [
            (60,   +2.0, 1.0, +1.0),    # kick body
            (300,  -3.0, 1.5,  0.0),    # boxiness cut
            (5000, +2.0, 1.2,  0.0),    # snap
            (12000,+1.0, 0.8,  0.0),    # air/cymbal
        ],
        "comp": {"threshold_db": -12.0, "ratio": 4.0,
                 "attack_ms": 5.0, "release_ms": 50.0},
    },
    "Bass": {
        "eq_bands": [
            (60,   +1.5, 1.2, +1.0),    # sub boost
            (400,  -2.0, 1.5,  0.0),    # mud cut
            (1000, +1.0, 2.0,  0.0),    # definition
            (5000, +0.5, 1.0,  0.0),    # presence
        ],
        "comp": {"threshold_db": -15.0, "ratio": 4.0,
                 "attack_ms": 30.0, "release_ms": 150.0},
    },
    "Guitar": {
        "eq_bands": [
            (200,  -2.0, 1.5,  0.0),    # mud cut
            (800,  -1.0, 2.0,  0.0),    # nasal reduction
            (2000, +1.5, 1.5,  0.0),    # bite
            (6000, +1.0, 1.0,  0.0),    # shimmer
        ],
        "comp": {"threshold_db": -20.0, "ratio": 2.5,
                 "attack_ms": 15.0, "release_ms": 100.0},
    },
    "Synth": {
        "eq_bands": [
            (100,  +1.0, 1.2,  0.0),    # sub warmth
            (800,  -1.5, 2.0,  0.0),    # nasal cut
            (5000, -0.5, 1.0,  0.0),    # tame highs slightly
            (8000, +1.0, 0.8,  0.0),    # air
        ],
        "comp": {"threshold_db": -20.0, "ratio": 2.0,
                 "attack_ms": 50.0, "release_ms": 200.0},
    },
    "Piano": {
        "eq_bands": [
            (250,  -1.5, 1.5,  0.0),    # boxy cut
            (2500, +1.0, 1.5,  0.0),    # presence
            (8000, +1.0, 1.0,  0.0),    # brilliance
        ],
        "comp": {"threshold_db": -18.0, "ratio": 2.5,
                 "attack_ms": 20.0, "release_ms": 120.0},
    },
}

# Fallback when classification fails.
_PRESETS["Other"] = _PRESETS["Guitar"]


# ─────────────────────────────────────────────────────────────────────────────
# Background analysis worker (QThread)
# ─────────────────────────────────────────────────────────────────────────────

class SmartEQWorker(QThread):
    """
    Runs librosa feature extraction + sklearn classification off the audio thread.

    Emits analysis_done(dict) on the GUI thread when complete, or
    analysis_failed(str) with an error message.
    """

    # Carry results as a plain Python dict — fully GUI-thread-safe via Qt signals.
    analysis_done   = Signal(dict)
    analysis_failed = Signal(str)

    def __init__(self, audio: np.ndarray, sample_rate: int) -> None:
        super().__init__()
        # Store copies so the audio thread can keep rendering without us reading
        # half-updated data.
        self._audio = audio.copy()
        self._sr    = sample_rate

    def run(self) -> None:
        """Entry point — this runs in the worker QThread, not the GUI thread."""
        if not _LIBROSA_OK or not _SKLEARN_OK:
            self.analysis_failed.emit(
                "Missing libraries. Run: pip install librosa scikit-learn"
            )
            return

        try:
            result = self._analyse()
            self.analysis_done.emit(result)
        except Exception as exc:
            logger.exception("SmartEQWorker failed")
            self.analysis_failed.emit(str(exc))

    def _analyse(self) -> dict:
        # Convert to mono for librosa.
        if self._audio.ndim == 2:
            mono = self._audio.mean(axis=1).astype(np.float32)
        else:
            mono = self._audio.astype(np.float32)

        sr = self._sr

        # ── Feature extraction ────────────────────────────────────────────────
        # Spectral centroid: centre of mass of the spectrum (Hz).
        centroid = float(np.mean(librosa.feature.spectral_centroid(y=mono, sr=sr)))

        # Spectral flatness: close to 1 = noise-like; close to 0 = tonal.
        flatness = float(np.mean(librosa.feature.spectral_flatness(y=mono)))

        # Root mean square energy (overall loudness proxy).
        rms = float(np.mean(librosa.feature.rms(y=mono)))

        # Zero-crossing rate (proxy for noisiness / percussion).
        zcr = float(np.mean(librosa.feature.zero_crossing_rate(mono)))

        # Low-frequency energy ratio: fraction of energy below 300 Hz.
        S = np.abs(librosa.stft(mono))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        lf_mask = freqs < 300.0
        lf_energy = float(S[lf_mask].sum()) / (float(S.sum()) + 1e-9)

        # MFCCs — not used in KNN but stored for display.
        mfcc = librosa.feature.mfcc(y=mono, sr=sr, n_mfcc=13)
        mfcc_mean = mfcc.mean(axis=1).tolist()

        # ── sklearn classification ────────────────────────────────────────────
        feat_vec = np.array([[centroid, flatness, rms, zcr, lf_energy]],
                            dtype=np.float32)

        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(_TRAIN_X)
        feat_scaled    = scaler.transform(feat_vec)

        clf = KNeighborsClassifier(n_neighbors=5, weights="distance")
        clf.fit(X_train_scaled, _TRAIN_Y)

        track_type   = clf.predict(feat_scaled)[0]
        # Confidence = fraction of the 5 nearest neighbours that agreed.
        probs = clf.predict_proba(feat_scaled)[0]
        confidence = float(np.max(probs))

        # ── Build result ──────────────────────────────────────────────────────
        preset = _PRESETS.get(track_type, _PRESETS["Other"])
        return {
            "track_type":  track_type,
            "confidence":  confidence,
            "centroid_hz": centroid,
            "flatness":    flatness,
            "rms":         rms,
            "zcr":         zcr,
            "lf_ratio":    lf_energy,
            "mfcc_mean":   mfcc_mean,
            "eq_bands":    preset["eq_bands"],
            "comp":        preset["comp"],
        }


# ─────────────────────────────────────────────────────────────────────────────
# FX Rack Plugin
# ─────────────────────────────────────────────────────────────────────────────

class SmartEQPlugin(FxPluginBase):
    """
    AI-assisted EQ / Compressor insert-slot plugin.

    Audio thread role:
        process() copies a short snapshot into a thread-safe deque and returns
        the audio UNCHANGED.  No librosa / sklearn code runs on the audio thread.

    GUI thread role:
        The parameter widget has an "Analyse" button that launches SmartEQWorker.
        On completion the worker emits analysis_done → _on_analysis_done() which
        updates the UI labels.  "Apply to Chain" then patches the DynamicEQ and
        MultibandCompressor plugins that live in the same insert chain.
    """

    DISPLAY_NAME = "Smart EQ / Compressor"

    # How many seconds of audio to buffer for analysis.
    _BUFFER_SECONDS = 5

    def __init__(self) -> None:
        super().__init__()
        self._chain_ref = None       # set by FxRackWidget after insertion
        self._sample_rate: int = 44100
        # Thread-safe rolling buffer — audio thread appends, GUI thread reads.
        self._audio_buffer: deque = deque()
        self._buffer_lock  = threading.Lock()
        self._last_result: Optional[dict] = None
        self._worker: Optional[SmartEQWorker] = None

        # UI references (set in create_parameter_widget)
        self._status_lbl:  Optional[QLabel] = None
        self._result_lbl:  Optional[QLabel] = None
        self._apply_btn:   Optional[QPushButton] = None
        self._analyse_btn: Optional[QPushButton] = None

    # ── Called by FxRackWidget after plugin insertion ─────────────────────────

    def set_chain(self, chain) -> None:
        """Receive a reference to the AudioFxChain this plugin lives in."""
        self._chain_ref = chain

    # ── FxPluginBase interface ────────────────────────────────────────────────

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """
        Pass-through: copies a snapshot for the next analysis request.
        The audio thread NEVER waits for AI results here.
        """
        self._sample_rate = sample_rate
        # Append this block to the rolling buffer (thread-safe list append).
        with self._buffer_lock:
            self._audio_buffer.append(audio.copy())
            # Discard oldest chunks when the buffer is full.
            max_chunks = max(1, (sample_rate * self._BUFFER_SECONDS) // max(1, len(audio)))
            while len(self._audio_buffer) > max_chunks:
                self._audio_buffer.popleft()
        return audio  # pure pass-through

    # ── UI ───────────────────────────────────────────────────────────────────

    def create_parameter_widget(self) -> QWidget:
        # Palette constants.
        _C = {
            "abyss": "#060A18", "deep": "#0A0E22",
            "cyan": "#00E5FF", "purple": "#9945FF",
            "text": "#C8E6FF", "text_dim": "#3D5A80",
            "gold": "#FFD700", "pink": "#FF2D9E",
        }

        def _grp(title: str) -> QGroupBox:
            g = QGroupBox(title)
            g.setStyleSheet(
                f"QGroupBox {{ border:1px solid rgba(153,69,255,0.3);"
                f" border-radius:6px; margin-top:10px; padding-top:6px;"
                f" color:{_C['text_dim']}; font-size:9px; background:{_C['abyss']}; }}"
                f"QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 5px;"
                f" color:rgba(153,69,255,0.9); }}"
            )
            return g

        def _btn(text: str, color: str) -> QPushButton:
            b = QPushButton(text)
            b.setFixedHeight(28)
            b.setStyleSheet(
                f"QPushButton {{ background:{_C['deep']};"
                f" border:1px solid {color}; border-radius:5px;"
                f" color:{color}; font-size:10px; font-weight:bold; }}"
                f"QPushButton:hover {{ background:rgba(0,229,255,0.08); }}"
                f"QPushButton:disabled {{ color:{_C['text_dim']};"
                f" border-color:{_C['text_dim']}; }}"
            )
            return b

        root = QWidget()
        root.setStyleSheet(f"background:{_C['abyss']};")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # ── Analysis trigger ──────────────────────────────────────────────────
        ana_grp = _grp("ANALYSIS")
        ana_lay = QVBoxLayout(ana_grp)

        if not (_LIBROSA_OK and _SKLEARN_OK):
            warn = QLabel("⚠  librosa + scikit-learn required.\npip install librosa scikit-learn")
            warn.setStyleSheet(
                f"color:{_C['gold']}; font-size:9px; background:transparent;"
            )
            ana_lay.addWidget(warn)

        self._analyse_btn = _btn("⚡ ANALYSE TRACK", _C["cyan"])
        self._analyse_btn.setEnabled(_LIBROSA_OK and _SKLEARN_OK)
        self._analyse_btn.clicked.connect(self._start_analysis)
        ana_lay.addWidget(self._analyse_btn)

        self._status_lbl = QLabel("Ready — click Analyse to classify this track.")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        ana_lay.addWidget(self._status_lbl)
        lay.addWidget(ana_grp)

        # ── Results display ───────────────────────────────────────────────────
        res_grp = _grp("SUGGESTIONS")
        res_lay = QVBoxLayout(res_grp)

        self._result_lbl = QLabel("No analysis yet.")
        self._result_lbl.setWordWrap(True)
        self._result_lbl.setStyleSheet(
            f"color:{_C['text']}; font-size:9px; background:transparent;"
            f" padding:4px;"
        )
        res_lay.addWidget(self._result_lbl)

        self._apply_btn = _btn("✓ APPLY TO CHAIN", _C["purple"])
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply_to_chain)
        res_lay.addWidget(self._apply_btn)

        lay.addWidget(res_grp)
        lay.addStretch()
        return root

    # ── Background worker lifecycle ───────────────────────────────────────────

    def _start_analysis(self) -> None:
        """Gather buffered audio and launch the background QThread worker."""
        with self._buffer_lock:
            if not self._audio_buffer:
                self._status_lbl.setText("No audio captured yet — play the track first.")
                return
            # Concatenate all buffered chunks.
            audio = np.concatenate(list(self._audio_buffer), axis=0)

        if self._analyse_btn:
            self._analyse_btn.setEnabled(False)
        if self._status_lbl:
            self._status_lbl.setText("Analysing… (running in background thread)")

        self._worker = SmartEQWorker(audio, self._sample_rate)
        # Connect worker signals to GUI-thread slots via Qt's thread-safe mechanism.
        self._worker.analysis_done.connect(self._on_analysis_done)
        self._worker.analysis_failed.connect(self._on_analysis_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_finished(self) -> None:
        """Re-enable the analyse button when the worker thread exits."""
        if self._analyse_btn:
            self._analyse_btn.setEnabled(_LIBROSA_OK and _SKLEARN_OK)

    def _on_analysis_done(self, result: dict) -> None:
        """Receive classification + preset from the worker thread (GUI thread)."""
        self._last_result = result
        t   = result["track_type"]
        c   = result["confidence"]
        eq  = result["eq_bands"]
        cmp = result["comp"]

        # Format EQ bands for display.
        eq_lines = "\n".join(
            f"  {freq:5.0f} Hz  {gain:+.1f} dB  Q={q:.1f}"
            for freq, gain, q, _sg in eq
        )
        comp_line = (
            f"  Thr {cmp['threshold_db']:.0f} dB | "
            f"Ratio {cmp['ratio']:.1f}:1 | "
            f"Atk {cmp['attack_ms']:.0f} ms | "
            f"Rel {cmp['release_ms']:.0f} ms"
        )
        text = (
            f"Track type: {t}  (confidence {c:.0%})\n\n"
            f"EQ bands:\n{eq_lines}\n\n"
            f"Compressor:\n{comp_line}"
        )
        if self._result_lbl:
            self._result_lbl.setText(text)
        if self._apply_btn:
            self._apply_btn.setEnabled(True)
        if self._status_lbl:
            self._status_lbl.setText("Analysis complete.")

    def _on_analysis_failed(self, msg: str) -> None:
        """Show error in the UI label."""
        if self._status_lbl:
            self._status_lbl.setText(f"Analysis failed: {msg}")
        if self._analyse_btn:
            self._analyse_btn.setEnabled(_LIBROSA_OK and _SKLEARN_OK)

    # ── Apply preset to chain ─────────────────────────────────────────────────

    def _apply_to_chain(self) -> None:
        """
        Push the last analysis result into DynamicEQPlugin and
        MultibandCompressorPlugin instances in the same insert chain.
        Creates the plugins if they don't exist yet.
        """
        if self._last_result is None or self._chain_ref is None:
            return

        from .fx_plugins_cpp import DynamicEQPlugin, MultibandCompressorPlugin

        eq_data   = self._last_result["eq_bands"]
        comp_data = self._last_result["comp"]

        # ── Find or create DynamicEQPlugin ────────────────────────────────────
        eq_plugin = next(
            (p for p in self._chain_ref.plugins if isinstance(p, DynamicEQPlugin)),
            None,
        )
        if eq_plugin is None:
            eq_plugin = DynamicEQPlugin()
            eq_plugin._on_changed = self._on_changed
            self._chain_ref.add_plugin(eq_plugin)

        # Update band definitions.
        eq_plugin.num_bands = len(eq_data)
        eq_plugin.band_defs = [
            {
                "freq_hz":        float(freq),
                "q":              float(q),
                "static_gain_db": float(sg),
                "threshold_db":   -30.0,
                "ratio":          2.0,
                "attack_ms":      8.0,
                "release_ms":     60.0,
                "enabled":        True,
            }
            for freq, gain, q, sg in eq_data
        ]
        # Overwrite static_gain_db with the suggested gain for immediate effect.
        for i, (freq, gain, q, _) in enumerate(eq_data):
            eq_plugin.band_defs[i]["static_gain_db"] = float(gain)

        if eq_plugin._processor is not None:
            try:
                eq_plugin._apply_params(eq_plugin._processor)
            except Exception as exc:
                logger.warning("Failed to push EQ params: %s", exc)

        # ── Find or create MultibandCompressorPlugin ──────────────────────────
        comp_plugin = next(
            (p for p in self._chain_ref.plugins
             if isinstance(p, MultibandCompressorPlugin)),
            None,
        )
        if comp_plugin is None:
            comp_plugin = MultibandCompressorPlugin()
            comp_plugin._on_changed = self._on_changed
            self._chain_ref.add_plugin(comp_plugin)

        # Apply the same comp settings to all 4 bands with slight per-band variation.
        ratios    = [comp_data["ratio"] * 0.75, comp_data["ratio"],
                     comp_data["ratio"],         comp_data["ratio"] * 1.25]
        attacks   = [comp_data["attack_ms"] * 2, comp_data["attack_ms"],
                     comp_data["attack_ms"],      comp_data["attack_ms"] * 0.5]
        releases  = [comp_data["release_ms"] * 2, comp_data["release_ms"],
                     comp_data["release_ms"],       comp_data["release_ms"] * 0.6]

        for i in range(4):
            comp_plugin.bands[i]["threshold_db"] = comp_data["threshold_db"]
            comp_plugin.bands[i]["ratio"]         = ratios[i]
            comp_plugin.bands[i]["attack_ms"]     = attacks[i]
            comp_plugin.bands[i]["release_ms"]    = releases[i]

        if comp_plugin._processor is not None:
            try:
                comp_plugin._apply_params(comp_plugin._processor)
            except Exception as exc:
                logger.warning("Failed to push comp params: %s", exc)

        # Fire the parameter-changed callback to trigger a re-render.
        self._notify()

        if self._status_lbl:
            self._status_lbl.setText(
                "✓ Applied: DynamicEQ + MultibandCompressor updated."
            )
