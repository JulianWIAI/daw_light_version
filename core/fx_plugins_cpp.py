"""
fx_plugins_cpp.py -- C++ daw_processors FX rack plugins.
=========================================================
Wraps the six C++ dynamics processors (BrickwallLimiter, MultibandCompressor,
DynamicEQ, DeEsser, TransientShaper, GateExpander) as FxPluginBase subclasses.

Import strategy:
  daw_processors is a compiled .pyd extension.  It lives in the core/ package
  directory alongside this file.  We attempt a relative package import first
  (from . import daw_processors) and fall back to a plain import.  If neither
  succeeds, _CPP_AVAILABLE is False and every plugin's process() returns the
  audio unchanged with a one-time warning.

Threading:
  process() is called from AudioFilePlayer's background render thread.
  Each plugin instance owns its own C++ processor object, so no shared state
  is accessed and no locking is required.
"""

from __future__ import annotations

import logging
import sys
import os
from typing import Optional

import numpy as np

from .fx_plugin_base import FxPluginBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# One-time import of the compiled C++ extension
# ---------------------------------------------------------------------------

_dp = None        # daw_processors module (or None if unavailable)
_CPP_AVAILABLE = False

def _ensure_cpp():
    """
    Try to import daw_processors.  Sets the module-level _dp and
    _CPP_AVAILABLE variables once and caches the result.
    """
    global _dp, _CPP_AVAILABLE
    if _CPP_AVAILABLE:
        return True  # already loaded

    # 1. Try relative import (works when .pyd is inside the core/ package).
    try:
        from . import daw_processors as _mod
        _dp = _mod
        _CPP_AVAILABLE = True
        return True
    except ImportError:
        pass

    # 2. Ensure the core/ directory is on sys.path and try a plain import.
    _core_dir = os.path.dirname(__file__)
    if _core_dir not in sys.path:
        sys.path.insert(0, _core_dir)
    try:
        import daw_processors as _mod  # type: ignore[import]
        _dp = _mod
        _CPP_AVAILABLE = True
        return True
    except ImportError:
        logger.warning(
            "daw_processors C++ extension not found. "
            "Build it with: cd cpp_processors && python setup.py build_ext --inplace"
        )
        return False


# ---------------------------------------------------------------------------
# Shared UI helpers (duplicated from fx_plugins_pedalboard to avoid coupling)
# ---------------------------------------------------------------------------

_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "cyan":     "#00E5FF",
    "text_dim": "#3D5A80",
}


def _param_row(parent, label: str, lo: int, hi: int, init: int):
    """Return (container, slider, value_label)."""
    from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSlider
    from PySide6.QtCore import Qt
    container = QWidget(parent)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(6)

    name_lbl = QLabel(label)
    name_lbl.setFixedWidth(90)
    name_lbl.setStyleSheet(
        f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
    )
    slider = QSlider(Qt.Horizontal)
    slider.setRange(lo, hi)
    slider.setValue(init)
    slider.setStyleSheet(
        "QSlider::groove:horizontal { height:4px; background:rgba(0,229,255,0.15);"
        " border-radius:2px; }"
        "QSlider::handle:horizontal { width:12px; height:12px; margin:-4px 0;"
        " background:#00E5FF; border-radius:6px; }"
        "QSlider::sub-page:horizontal { background:rgba(0,229,255,0.4);"
        " border-radius:2px; }"
    )
    val_lbl = QLabel(str(init))
    val_lbl.setFixedWidth(40)
    val_lbl.setAlignment(Qt.AlignRight)
    val_lbl.setStyleSheet(
        f"color:{_C['cyan']}; font-size:9px; background:transparent;"
    )
    row.addWidget(name_lbl)
    row.addWidget(slider)
    row.addWidget(val_lbl)
    return container, slider, val_lbl


def _group_box(title: str):
    from PySide6.QtWidgets import QGroupBox
    g = QGroupBox(title)
    g.setStyleSheet(
        f"QGroupBox {{ border:1px solid rgba(153,69,255,0.3); border-radius:6px;"
        f" margin-top:10px; padding-top:6px; color:{_C['text_dim']};"
        f" font-size:9px; letter-spacing:1px; background:{_C['abyss']}; }}"
        f"QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 5px;"
        f" color:rgba(153,69,255,0.9); }}"
    )
    return g


def _base_widget():
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    w = QWidget()
    w.setStyleSheet(f"background:{_C['abyss']};")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(8, 8, 8, 8)
    lay.setSpacing(4)
    return w, lay


# ---------------------------------------------------------------------------
# Base for all C++ plugins
# ---------------------------------------------------------------------------

class _CppPlugin(FxPluginBase):
    """
    Shared process() implementation for C++ daw_processors plugins.

    Subclasses must implement:
        _make_processor(sample_rate) -- create and return the C++ object.

    The processor is created lazily on the first process() call so that
    GUI-only instantiation (e.g. just building the parameter widget) does
    not trigger a C++ construction.
    """

    def __init__(self) -> None:
        super().__init__()
        self._processor = None          # C++ processor instance (lazy init)
        self._processor_sr: int = 0     # sample rate the processor was last prepared for

    def _make_processor(self, sample_rate: int):
        """Instantiate and return the C++ processor at the given sample rate."""
        raise NotImplementedError

    def _get_processor(self, sample_rate: int):
        """
        Return the C++ processor, creating or re-preparing it if the sample
        rate has changed since the last call.
        """
        if not _ensure_cpp():
            return None
        if self._processor is None or self._processor_sr != sample_rate:
            self._processor = self._make_processor(sample_rate)
            self._processor_sr = sample_rate
        return self._processor

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """
        Split stereo audio into left/right 1-D arrays, pass through the C++
        processor, and recombine into (samples, channels) format.
        """
        proc = self._get_processor(sample_rate)
        if proc is None:
            return audio  # C++ unavailable -- pass through unchanged

        try:
            # Ensure stereo float32 before splitting.
            if audio.ndim == 1:
                audio = np.column_stack([audio, audio])
            elif audio.shape[1] == 1:
                audio = np.repeat(audio, 2, axis=1)

            left  = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
            right = np.ascontiguousarray(audio[:, 1], dtype=np.float32)

            out_l, out_r = proc.process_block(left, right)

            return np.column_stack([
                np.asarray(out_l, dtype=np.float32),
                np.asarray(out_r, dtype=np.float32),
            ])
        except Exception as exc:
            logger.warning("%s.process() failed: %s", self.DISPLAY_NAME, exc)
            return audio


# ---------------------------------------------------------------------------
# Brickwall Limiter
# ---------------------------------------------------------------------------

class BrickwallLimiterPlugin(_CppPlugin):
    """
    Look-ahead true-peak brickwall limiter (-0.1 dBFS ceiling by default).
    Uses Catmull-Rom interpolation to detect inter-sample peaks.
    """

    DISPLAY_NAME = "Brickwall Limiter"

    def __init__(self) -> None:
        super().__init__()
        self.ceiling_db:    float = -0.1   # dBFS output ceiling
        self.lookahead_ms:  float = 5.0    # look-ahead delay in ms
        self.attack_ms:     float = 0.5    # gain reduction attack
        self.release_ms:    float = 150.0  # gain recovery release

    def _make_processor(self, sample_rate: int):
        p = _dp.BrickwallLimiter(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        """Push current parameter values to the C++ object."""
        p.set_ceiling(self.ceiling_db)
        p.set_lookahead(self.lookahead_ms)
        p.set_attack(self.attack_ms)
        p.set_release(self.release_ms)

    def create_parameter_widget(self):
        w, lay = _base_widget()
        grp = _group_box("BRICKWALL LIMITER")
        from PySide6.QtWidgets import QVBoxLayout
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        params = [
            # (label, attr, lo, hi, scale, init_int, decimals)
            ("Ceiling dB",   "ceiling_db",   -120, 0,   10, int(self.ceiling_db*10),   1),
            ("Lookahead ms", "lookahead_ms",  0,   200, 10, int(self.lookahead_ms*10), 1),
            ("Attack ms",    "attack_ms",     1,   100, 10, int(self.attack_ms*10),    1),
            ("Release ms",   "release_ms",    10, 5000, 10, int(self.release_ms*10),   1),
        ]
        for label, attr, lo, hi, scale, init, dec in params:
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                # Push updated value to C++ object if it already exists.
                if self._processor is not None:
                    try:
                        self._apply_params(self._processor)
                    except Exception:
                        pass
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ---------------------------------------------------------------------------
# Multiband Compressor
# ---------------------------------------------------------------------------

class MultibandCompressorPlugin(_CppPlugin):
    """
    4-band compressor with Linkwitz-Riley 4th-order crossover filters.
    Crossovers at 200 Hz, 2 kHz and 8 kHz by default.
    """

    DISPLAY_NAME = "Multiband Compressor"

    def __init__(self) -> None:
        super().__init__()
        # Crossover frequencies (3 crossovers for 4 bands).
        self.crossovers = [200.0, 2000.0, 8000.0]

        # Per-band compressor settings (4 bands: low, low-mid, mid, high).
        # Stored as plain dicts to avoid importing BandConfig here.
        self.bands = [
            {"threshold_db": -18.0, "ratio": 3.0,
             "attack_ms": 20.0, "release_ms": 150.0, "makeup_db": 0.0},
            {"threshold_db": -18.0, "ratio": 4.0,
             "attack_ms": 10.0, "release_ms": 100.0, "makeup_db": 0.0},
            {"threshold_db": -18.0, "ratio": 4.0,
             "attack_ms": 10.0, "release_ms": 80.0,  "makeup_db": 0.0},
            {"threshold_db": -12.0, "ratio": 6.0,
             "attack_ms":  5.0, "release_ms": 60.0,  "makeup_db": 0.0},
        ]

    def _make_processor(self, sample_rate: int):
        p = _dp.MultibandCompressor(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        for i, hz in enumerate(self.crossovers):
            p.set_crossover(i, float(hz))
        for i, b in enumerate(self.bands):
            cfg = _dp.BandConfig()
            cfg.threshold_db = b["threshold_db"]
            cfg.ratio        = b["ratio"]
            cfg.attack_ms    = b["attack_ms"]
            cfg.release_ms   = b["release_ms"]
            cfg.makeup_db    = b["makeup_db"]
            p.set_band(i, cfg)

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout, QLabel
        w, lay = _base_widget()
        grp = _group_box("MULTIBAND COMPRESSOR")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        band_names = ["Low", "Low-Mid", "Mid", "High"]
        for i, (band, name) in enumerate(zip(self.bands, band_names)):
            band_label = QLabel(f"── {name} Band ──")
            band_label.setStyleSheet(
                f"color:rgba(153,69,255,0.8); font-size:9px; background:transparent;"
            )
            grp_lay.addWidget(band_label)

            # Threshold slider (-60 to 0 dB, ×10 for precision).
            row_w, slider, val_lbl = _param_row(
                grp, "Threshold dB",
                -600, 0, int(band["threshold_db"] * 10)
            )
            val_lbl.setText(f"{band['threshold_db']:.1f}")

            def _thr_cb(v, idx=i, l=val_lbl):
                self.bands[idx]["threshold_db"] = v / 10.0
                l.setText(f"{v/10.0:.1f}")
                if self._processor is not None:
                    try:
                        self._apply_params(self._processor)
                    except Exception:
                        pass
                self._notify()

            slider.valueChanged.connect(_thr_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ---------------------------------------------------------------------------
# Dynamic EQ
# ---------------------------------------------------------------------------

class DynamicEQPlugin(_CppPlugin):
    """
    Parametric EQ with threshold-triggered gain modulation (up to 8 bands).
    Each band activates its peak EQ only when the signal exceeds the threshold.
    """

    DISPLAY_NAME = "Dynamic EQ"

    def __init__(self) -> None:
        super().__init__()
        # Three default bands: air cut, presence boost, mud cut.
        self.num_bands = 3
        self.band_defs = [
            {"freq_hz": 250.0,  "q": 1.5, "static_gain_db": 0.0,
             "threshold_db": -30.0, "ratio": 3.0,
             "attack_ms": 10.0, "release_ms": 80.0,  "enabled": True},
            {"freq_hz": 3000.0, "q": 1.5, "static_gain_db": 0.0,
             "threshold_db": -24.0, "ratio": 2.0,
             "attack_ms": 8.0,  "release_ms": 60.0,  "enabled": True},
            {"freq_hz": 8000.0, "q": 1.0, "static_gain_db": 0.0,
             "threshold_db": -18.0, "ratio": 4.0,
             "attack_ms": 5.0,  "release_ms": 40.0,  "enabled": True},
        ]

    def _make_processor(self, sample_rate: int):
        p = _dp.DynamicEQ(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_num_bands(self.num_bands)
        for i, b in enumerate(self.band_defs[:self.num_bands]):
            band = _dp.DynEQBand()
            band.freq_hz        = b["freq_hz"]
            band.q              = b["q"]
            band.static_gain_db = b["static_gain_db"]
            band.threshold_db   = b["threshold_db"]
            band.ratio          = b["ratio"]
            band.attack_ms      = b["attack_ms"]
            band.release_ms     = b["release_ms"]
            band.enabled        = b["enabled"]
            p.set_band(i, band)

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout, QLabel
        w, lay = _base_widget()
        grp = _group_box("DYNAMIC EQ")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        for i, b in enumerate(self.band_defs[:self.num_bands]):
            lbl = QLabel(f"Band {i+1}: {b['freq_hz']:.0f} Hz")
            lbl.setStyleSheet(
                f"color:rgba(153,69,255,0.8); font-size:9px; background:transparent;"
            )
            grp_lay.addWidget(lbl)

            row_w, slider, val_lbl = _param_row(
                grp, "Threshold dB",
                -600, 0, int(b["threshold_db"] * 10)
            )
            val_lbl.setText(f"{b['threshold_db']:.1f}")

            def _cb(v, idx=i, l=val_lbl):
                self.band_defs[idx]["threshold_db"] = v / 10.0
                l.setText(f"{v/10.0:.1f}")
                if self._processor is not None:
                    try:
                        self._apply_params(self._processor)
                    except Exception:
                        pass
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ---------------------------------------------------------------------------
# De-Esser
# ---------------------------------------------------------------------------

class DeEsserPlugin(_CppPlugin):
    """
    Sibilance-targeted compressor operating in the 4-10 kHz frequency range.
    Supports WIDEBAND (full signal gain reduction) and SPLIT (HF band only) modes.
    """

    DISPLAY_NAME = "De-Esser"

    def __init__(self) -> None:
        super().__init__()
        self.frequency_hz: float = 7000.0  # sibilance detection centre frequency
        self.threshold_db: float = -20.0   # dBFS detection threshold
        self.ratio:        float = 6.0     # compression ratio
        self.attack_ms:    float = 1.0     # attack time
        self.release_ms:   float = 50.0    # release time
        self.split_mode:   bool  = False   # False=WIDEBAND, True=SPLIT

    def _make_processor(self, sample_rate: int):
        p = _dp.DeEsser(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_frequency(self.frequency_hz)
        p.set_threshold(self.threshold_db)
        p.set_ratio(self.ratio)
        p.set_attack(self.attack_ms)
        p.set_release(self.release_ms)
        p.set_split_mode(self.split_mode)

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout, QCheckBox
        w, lay = _base_widget()
        grp = _group_box("DE-ESSER")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        params = [
            ("Freq Hz",      "frequency_hz", 4000,  12000, 1, int(self.frequency_hz),  0),
            ("Threshold dB", "threshold_db", -600,  0,    10, int(self.threshold_db*10), 1),
            ("Ratio",        "ratio",         10,   200,  10, int(self.ratio*10),       1),
            ("Attack ms",    "attack_ms",      1,   100,  10, int(self.attack_ms*10),   1),
            ("Release ms",   "release_ms",    10,  1000,  10, int(self.release_ms*10),  1),
        ]
        for label, attr, lo, hi, scale, init, dec in params:
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                if self._processor is not None:
                    try:
                        self._apply_params(self._processor)
                    except Exception:
                        pass
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        # Split mode toggle.
        split_cb = QCheckBox("Split Mode (HF band only)")
        split_cb.setChecked(self.split_mode)
        split_cb.setStyleSheet(f"color:{_C['text_dim']}; font-size:9px;")

        def _split_toggled(state):
            self.split_mode = bool(state)
            if self._processor is not None:
                try:
                    self._apply_params(self._processor)
                except Exception:
                    pass
            self._notify()

        split_cb.stateChanged.connect(_split_toggled)
        grp_lay.addWidget(split_cb)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ---------------------------------------------------------------------------
# Transient Shaper
# ---------------------------------------------------------------------------

class TransientShaperPlugin(_CppPlugin):
    """
    Independent attack/sustain envelope shaper using a dual-envelope follower.
    Positive attack gain emphasises transients; negative softens them.
    """

    DISPLAY_NAME = "Transient Shaper"

    def __init__(self) -> None:
        super().__init__()
        self.attack_gain:  float = 6.0    # dB applied to transient portion
        self.sustain_gain: float = -3.0   # dB applied to sustain body

    def _make_processor(self, sample_rate: int):
        p = _dp.TransientShaper(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_attack_gain(self.attack_gain)
        p.set_sustain_gain(self.sustain_gain)

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout
        w, lay = _base_widget()
        grp = _group_box("TRANSIENT SHAPER")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        params = [
            ("Attack Gain dB",  "attack_gain",  -240, 240, 10, int(self.attack_gain*10),  1),
            ("Sustain Gain dB", "sustain_gain", -240, 240, 10, int(self.sustain_gain*10), 1),
        ]
        for label, attr, lo, hi, scale, init, dec in params:
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                if self._processor is not None:
                    try:
                        self._apply_params(self._processor)
                    except Exception:
                        pass
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ---------------------------------------------------------------------------
# Gate / Expander
# ---------------------------------------------------------------------------

class GateExpanderPlugin(_CppPlugin):
    """
    Threshold-based noise gate with 5-state machine (CLOSED/OPENING/OPEN/
    HOLDING/CLOSING) and hysteresis. Ratio > 1.0 gives downward expansion.
    """

    DISPLAY_NAME = "Gate / Expander"

    def __init__(self) -> None:
        super().__init__()
        self.threshold_db: float = -40.0  # open threshold (dBFS)
        self.hysteresis_db: float = 6.0   # gap between open and close thresholds
        self.ratio:         float = 2.0   # expansion ratio (1 = hard gate)
        self.attack_ms:     float = 2.0
        self.hold_ms:       float = 50.0
        self.release_ms:    float = 100.0
        self.range_db:      float = -80.0  # minimum gain when fully closed

    def _make_processor(self, sample_rate: int):
        p = _dp.GateExpander(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_threshold(self.threshold_db)
        p.set_hysteresis(self.hysteresis_db)
        p.set_ratio(self.ratio)
        p.set_attack(self.attack_ms)
        p.set_hold(self.hold_ms)
        p.set_release(self.release_ms)
        p.set_range(self.range_db)

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout
        w, lay = _base_widget()
        grp = _group_box("GATE / EXPANDER")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        params = [
            ("Threshold dB",  "threshold_db",  -800, 0,    10, int(self.threshold_db*10),  1),
            ("Hysteresis dB", "hysteresis_db",  0,   240,  10, int(self.hysteresis_db*10), 1),
            ("Ratio",         "ratio",          10,  200,  10, int(self.ratio*10),          1),
            ("Attack ms",     "attack_ms",       1,  500,  10, int(self.attack_ms*10),      1),
            ("Hold ms",       "hold_ms",         0, 2000,  10, int(self.hold_ms*10),        1),
            ("Release ms",    "release_ms",     10, 5000,  10, int(self.release_ms*10),     1),
        ]
        for label, attr, lo, hi, scale, init, dec in params:
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                if self._processor is not None:
                    try:
                        self._apply_params(self._processor)
                    except Exception:
                        pass
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w
