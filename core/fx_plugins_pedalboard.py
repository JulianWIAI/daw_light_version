"""
fx_plugins_pedalboard.py -- Pedalboard-backed FX rack plugins.
==============================================================
Wraps the four legacy audio effects (EQ, Reverb, Compressor, Chorus) as
FxPluginBase subclasses so they can be loaded into dynamic FX rack slots.

Each plugin:
  - Stores its own parameters as instance attributes.
  - Builds a pedalboard.Pedalboard on every process() call (fast for offline
    renders; no persistent plugin state issues across parameter changes).
  - Returns a styled QWidget from create_parameter_widget() so the rack
    panel can display controls immediately after instantiation.

Design note:
  Parameter widgets call self._notify() on every slider change.  FxRackWidget
  sets self._on_changed to a lambda that emits chain_changed(track_id) so
  AudioFilePlayer can re-render the currently-playing clip.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import numpy as np

from .fx_plugin_base import FxPluginBase

logger = logging.getLogger(__name__)

# Crystal bioluminescence palette -- local copy avoids circular imports.
_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "cyan":     "#00E5FF",
    "text":     "#C8E6FF",
    "text_dim": "#3D5A80",
}


# ---------------------------------------------------------------------------
# Shared UI helper
# ---------------------------------------------------------------------------

def _make_param_row(parent, label: str, lo: int, hi: int, init: int):
    """
    Build a labelled horizontal slider row.

    Returns (container_widget, slider, value_label) so callers can connect
    slider.valueChanged and update the value_label text.
    """
    from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSlider
    from PySide6.QtCore import Qt

    container = QWidget(parent)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(6)

    # Fixed-width label so all sliders line up across rows.
    name_lbl = QLabel(label)
    name_lbl.setFixedWidth(72)
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

    # Right-aligned numeric readout.
    val_lbl = QLabel(str(init))
    val_lbl.setFixedWidth(36)
    val_lbl.setAlignment(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.AlignRight)
    val_lbl.setStyleSheet(
        f"color:{_C['cyan']}; font-size:9px; background:transparent;"
    )

    row.addWidget(name_lbl)
    row.addWidget(slider)
    row.addWidget(val_lbl)
    return container, slider, val_lbl


def _group_box(title: str):
    """Return a styled QGroupBox matching the crystal theme."""
    from PySide6.QtWidgets import QGroupBox
    g = QGroupBox(title)
    g.setStyleSheet(
        f"QGroupBox {{ border:1px solid rgba(0,229,255,0.2); border-radius:6px;"
        f" margin-top:10px; padding-top:6px; color:{_C['text_dim']};"
        f" font-size:9px; letter-spacing:1px; background:{_C['abyss']}; }}"
        f"QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 5px;"
        f" color:rgba(0,229,255,0.7); }}"
    )
    return g


def _base_widget():
    """Return a dark-themed QWidget + its QVBoxLayout."""
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    w = QWidget()
    w.setStyleSheet(f"background:{_C['abyss']};")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(8, 8, 8, 8)
    lay.setSpacing(4)
    return w, lay


# ---------------------------------------------------------------------------
# Base class shared by all pedalboard plugins
# ---------------------------------------------------------------------------

class _PedalboardPlugin(FxPluginBase):
    """
    Shared process() implementation for plugins backed by pedalboard.

    Subclasses must implement _build_plugins(sample_rate) -> list of
    pedalboard plugin instances (or empty list = pass-through).
    """

    def _build_plugins(self, sample_rate: int) -> list:
        """Return a list of pedalboard plugin objects for the current params."""
        return []

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Build a pedalboard.Pedalboard and run it on the audio buffer."""
        try:
            import pedalboard as pb
            plugins = self._build_plugins(sample_rate)
            if not plugins:
                return audio  # nothing active -- pass through unchanged
            board = pb.Pedalboard(plugins)
            # pedalboard expects (channels, samples) float32.
            processed = board(audio.T.astype(np.float32), sample_rate)
            return processed.T  # back to (samples, channels)
        except ImportError:
            logger.warning("pedalboard not installed -- %s bypassed", self.DISPLAY_NAME)
            return audio
        except Exception as exc:
            logger.warning("%s.process() failed: %s", self.DISPLAY_NAME, exc)
            return audio


# ---------------------------------------------------------------------------
# 5-Band EQ
# ---------------------------------------------------------------------------

class EQPlugin(_PedalboardPlugin):
    """
    5-Band graphic EQ using pedalboard shelf and peak filters.

    Bands: 32 Hz low shelf, 250 Hz peak, 1 kHz peak, 4 kHz peak, 16 kHz high shelf.
    Each band ±12 dB.
    """

    DISPLAY_NAME = "5-Band EQ"

    def __init__(self) -> None:
        super().__init__()
        self.eq_32:  float = 0.0   # dB at 32 Hz
        self.eq_250: float = 0.0   # dB at 250 Hz
        self.eq_1k:  float = 0.0   # dB at 1 kHz
        self.eq_4k:  float = 0.0   # dB at 4 kHz
        self.eq_16k: float = 0.0   # dB at 16 kHz

    def _build_plugins(self, sample_rate: int) -> list:
        import pedalboard as pb
        # DIAGNOSTIC ── probe 2: EQ band values at render time ────────────────
        import logging as _log
        _log.getLogger(__name__).info(
            "[DIAG] EQPlugin._build_plugins() | sr=%d | "
            "32Hz=%+.1fdB  250Hz=%+.1fdB  1kHz=%+.1fdB  4kHz=%+.1fdB  16kHz=%+.1fdB",
            sample_rate,
            self.eq_32, self.eq_250, self.eq_1k, self.eq_4k, self.eq_16k,
        )
        # ─────────────────────────────────────────────────────────────────────
        plugins = []
        if self.eq_32  != 0.0:
            plugins.append(pb.LowShelfFilter(
                cutoff_frequency_hz=60.0, gain_db=float(self.eq_32), q=0.7))
        if self.eq_250 != 0.0:
            plugins.append(pb.PeakFilter(
                cutoff_frequency_hz=250.0, gain_db=float(self.eq_250), q=1.0))
        if self.eq_1k  != 0.0:
            plugins.append(pb.PeakFilter(
                cutoff_frequency_hz=1000.0, gain_db=float(self.eq_1k), q=1.0))
        if self.eq_4k  != 0.0:
            plugins.append(pb.PeakFilter(
                cutoff_frequency_hz=4000.0, gain_db=float(self.eq_4k), q=1.0))
        if self.eq_16k != 0.0:
            plugins.append(pb.HighShelfFilter(
                cutoff_frequency_hz=10000.0, gain_db=float(self.eq_16k), q=0.7))
        _log.getLogger(__name__).info(  # DIAGNOSTIC
            "[DIAG] EQPlugin: %d active filter(s) built (all-zero bands are skipped / pass-through)",
            len(plugins),
        )
        return plugins

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSlider
        from PySide6.QtCore import Qt

        w, lay = _base_widget()
        grp = _group_box("5-BAND EQ")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(2)

        # Vertical band sliders in a row (like a hardware graphic EQ).
        bands_w = QWidget()
        bands_lay = QHBoxLayout(bands_w)
        bands_lay.setContentsMargins(0, 0, 0, 0)
        bands_lay.setSpacing(8)

        band_defs: List[Tuple[str, str]] = [
            ("32",  "eq_32"),
            ("250", "eq_250"),
            ("1k",  "eq_1k"),
            ("4k",  "eq_4k"),
            ("16k", "eq_16k"),
        ]
        for freq_label, attr in band_defs:
            col = QVBoxLayout()
            col.setSpacing(2)

            slider = QSlider(Qt.Vertical)
            slider.setRange(-24, 24)      # ±12 dB stored as ±24 int (×0.5 dB/unit)
            slider.setValue(int(getattr(self, attr) * 2))
            slider.setFixedHeight(80)
            slider.setStyleSheet(
                "QSlider::groove:vertical { width:4px; background:rgba(0,229,255,0.15);"
                " border-radius:2px; }"
                "QSlider::handle:vertical { height:10px; width:10px; margin:0 -3px;"
                " background:#00E5FF; border-radius:5px; }"
            )

            val_lbl = QLabel("0.0")
            val_lbl.setStyleSheet(
                f"color:{_C['cyan']}; font-size:8px; background:transparent;"
            )
            val_lbl.setAlignment(Qt.AlignCenter)

            def _on_band(v, a=attr, lbl=val_lbl):
                db = v * 0.5
                setattr(self, a, db)
                lbl.setText(f"{db:+.1f}")
                self._notify()

            slider.valueChanged.connect(_on_band)

            col.addWidget(slider, alignment=Qt.AlignHCenter)
            col.addWidget(QLabel(freq_label, styleSheet=(
                f"color:{_C['text_dim']}; font-size:8px; background:transparent;"
            )), alignment=Qt.AlignHCenter)
            col.addWidget(val_lbl, alignment=Qt.AlignHCenter)
            bands_lay.addLayout(col)

        grp_lay.addWidget(bands_w)
        lay.addWidget(grp)
        lay.addStretch()
        return w


# ---------------------------------------------------------------------------
# Reverb
# ---------------------------------------------------------------------------

class ReverbPlugin(_PedalboardPlugin):
    """Reverb via pedalboard.Reverb. Controls: room size, damping, wet level."""

    DISPLAY_NAME = "Reverb"

    def __init__(self) -> None:
        super().__init__()
        self.room: float = 0.3   # 0–1 room size
        self.damp: float = 0.5   # 0–1 HF damping
        self.wet:  float = 0.2   # 0–1 wet/dry mix

    def _build_plugins(self, sample_rate: int) -> list:
        import pedalboard as pb
        return [pb.Reverb(
            room_size=float(self.room),
            damping=float(self.damp),
            wet_level=float(self.wet),
            dry_level=1.0 - float(self.wet),
        )]

    def create_parameter_widget(self):
        w, lay = _base_widget()
        grp = _group_box("REVERB")
        grp_lay = __import__("PySide6.QtWidgets", fromlist=["QVBoxLayout"]).QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        # (label, attr, lo_int, hi_int, scale, init_int, fmt)
        param_defs = [
            ("Room",  "room", 0, 100, 100.0, int(self.room * 100), ".2f"),
            ("Damp",  "damp", 0, 100, 100.0, int(self.damp * 100), ".2f"),
            ("Wet",   "wet",  0, 100, 100.0, int(self.wet  * 100), ".2f"),
        ]
        for label, attr, lo, hi, scale, init, fmt in param_defs:
            row_w, slider, val_lbl = _make_param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:{fmt[1:]}}")

            def _cb(v, a=attr, s=scale, l=val_lbl, f=fmt):
                setattr(self, a, v / s)
                l.setText(f"{v/s:{f[1:]}}")
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ---------------------------------------------------------------------------
# Compressor
# ---------------------------------------------------------------------------

class CompressorPlugin(_PedalboardPlugin):
    """Compressor via pedalboard.Compressor. Controls: threshold, ratio, attack, release."""

    DISPLAY_NAME = "Compressor"

    def __init__(self) -> None:
        super().__init__()
        self.threshold: float = -18.0  # dBFS
        self.ratio:     float = 4.0    # compression ratio
        self.attack:    float = 10.0   # ms
        self.release:   float = 100.0  # ms

    def _build_plugins(self, sample_rate: int) -> list:
        import pedalboard as pb
        import logging as _log
        # DIAGNOSTIC ── probe 3: Compressor params at render time ─────────────
        _log.getLogger(__name__).info(
            "[DIAG] CompressorPlugin._build_plugins() | "
            "threshold=%.1fdBFS  ratio=%.1f:1  attack=%.1fms  release=%.1fms",
            self.threshold, self.ratio, self.attack, self.release,
        )
        # ─────────────────────────────────────────────────────────────────────
        return [pb.Compressor(
            threshold_db=float(self.threshold),
            ratio=float(self.ratio),
            attack_ms=float(self.attack),
            release_ms=float(self.release),
        )]

    def create_parameter_widget(self):
        w, lay = _base_widget()
        grp = _group_box("COMPRESSOR")
        grp_lay = __import__("PySide6.QtWidgets", fromlist=["QVBoxLayout"]).QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        # (label, attr, lo_int, hi_int, scale, init_int, decimals)
        param_defs = [
            ("Threshold", "threshold", -600, 0,    10.0,  int(self.threshold * 10), 1),
            ("Ratio",     "ratio",     10,   200,  10.0,  int(self.ratio * 10),     1),
            ("Attack ms", "attack",    1,    2000, 10.0,  int(self.attack * 10),    1),
            ("Release ms","release",   100,  10000,10.0,  int(self.release * 10),   1),
        ]
        for label, attr, lo, hi, scale, init, dec in param_defs:
            row_w, slider, val_lbl = _make_param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ---------------------------------------------------------------------------
# Chorus
# ---------------------------------------------------------------------------

class ChorusPlugin(_PedalboardPlugin):
    """Chorus via pedalboard.Chorus. Controls: rate, depth, wet mix."""

    DISPLAY_NAME = "Chorus"

    def __init__(self) -> None:
        super().__init__()
        self.rate:  float = 1.0    # Hz
        self.depth: float = 0.25   # 0–1
        self.wet:   float = 0.3    # 0–1 wet/dry mix

    def _build_plugins(self, sample_rate: int) -> list:
        import pedalboard as pb
        return [pb.Chorus(
            rate_hz=float(self.rate),
            depth=float(self.depth),
            mix=float(self.wet),
        )]

    def create_parameter_widget(self):
        w, lay = _base_widget()
        grp = _group_box("CHORUS")
        grp_lay = __import__("PySide6.QtWidgets", fromlist=["QVBoxLayout"]).QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        param_defs = [
            ("Rate Hz", "rate",  1,  50,  10.0, int(self.rate  * 10),  1),
            ("Depth",   "depth", 0,  100, 100.0,int(self.depth * 100), 2),
            ("Wet",     "wet",   0,  100, 100.0,int(self.wet   * 100), 2),
        ]
        for label, attr, lo, hi, scale, init, dec in param_defs:
            row_w, slider, val_lbl = _make_param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w
