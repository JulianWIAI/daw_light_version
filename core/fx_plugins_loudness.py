"""
fx_plugins_loudness.py  --  Loudness Automation FX rack plugin.
================================================================
Wraps the LoudnessAutomation DSP chain (RmsAnalyzer → EnvelopeFollower →
PID Controller → per-sample gain interpolation) as a FxPluginBase subclass
so it can be loaded into any track's FX rack slot.

Threading:
    process() is called from AudioFilePlayer's background render thread.
    GUI updates (gain meter) are done via a QTimer polling the processor's
    current_gain_db property from the GUI thread — never from process().
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from .fx_plugin_base import FxPluginBase
from .loudness_automation_python import get_loudness_automation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour palette (matches the project's dark UI theme)
# ---------------------------------------------------------------------------

_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "cyan":     "#00E5FF",
    "gold":     "#FFD700",
    "text_dim": "#3D5A80",
    "text":     "#E0F0FF",
}


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _param_row(parent, label: str, lo: int, hi: int, init: int):
    """Return (container_widget, slider, value_label)."""
    from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSlider
    from PySide6.QtCore import Qt

    container = QWidget(parent)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(6)

    name_lbl = QLabel(label)
    name_lbl.setFixedWidth(96)
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
    val_lbl.setFixedWidth(46)
    # NOTE: must use Qt.AlignRight (enum), NOT a raw int — PySide6 rejects ints here.
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
        f"QGroupBox {{ border:1px solid rgba(255,215,0,0.3); border-radius:6px;"
        f" margin-top:10px; padding-top:6px; color:{_C['text_dim']};"
        f" font-size:9px; letter-spacing:1px; background:{_C['abyss']}; }}"
        f"QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 5px;"
        f" color:rgba(255,215,0,0.9); }}"
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
# LoudnessAutomationPlugin
# ---------------------------------------------------------------------------

class LoudnessAutomationPlugin(FxPluginBase):
    """
    Real-time loudness automation insert effect.

    Measures per-block RMS, smooths it through a one-pole envelope follower,
    and uses a PID controller to compute a gain correction.  The correction
    is applied with per-sample linear interpolation (no zipper noise).

    Serialisable parameters:
        target_dbfs  : target RMS loudness (dBFS)
        attack_ms    : envelope follower attack time
        release_ms   : envelope follower release time
        kp, ki, kd   : PID gains
        gain_min_db  : minimum gain correction (dB)
        gain_max_db  : maximum gain correction (dB)
    """

    DISPLAY_NAME = "Loudness Automation"

    def __init__(self) -> None:
        super().__init__()

        # Serialisable DSP parameters.
        self.target_dbfs:  float = -18.0
        self.attack_ms:    float = 20.0
        self.release_ms:   float = 200.0
        self.kp:           float = 1.0
        self.ki:           float = 0.1
        self.kd:           float = 0.05
        self.gain_min_db:  float = -30.0
        self.gain_max_db:  float = +12.0

        # Live processor — lazily created on first process() call.
        self._proc = None
        self._proc_sr: int = 0

        # Cached gain value for metering (written from audio thread, read by timer).
        # Simple float assignment is effectively atomic on CPython due to the GIL.
        self._cached_gain_db: float = 0.0

    # ------------------------------------------------------------------
    # Processor lifecycle
    # ------------------------------------------------------------------

    def _get_proc(self, sample_rate: int):
        """Return the live processor, recreating it when the sample rate changes."""
        if self._proc is None or self._proc_sr != sample_rate:
            self._proc = get_loudness_automation(
                sample_rate=float(sample_rate),
                target_dbfs=self.target_dbfs,
                attack_ms=self.attack_ms,
                release_ms=self.release_ms,
                kp=self.kp, ki=self.ki, kd=self.kd,
                gain_min_db=self.gain_min_db,
                gain_max_db=self.gain_max_db,
            )
            self._proc_sr = sample_rate
        return self._proc

    def _push_params(self) -> None:
        """Push current slider values to the live processor (if one exists)."""
        if self._proc is None:
            return
        try:
            self._proc.set_params(
                target_dbfs=self.target_dbfs,
                attack_ms=self.attack_ms,
                release_ms=self.release_ms,
                kp=self.kp, ki=self.ki, kd=self.kd,
                gain_min_db=self.gain_min_db,
                gain_max_db=self.gain_max_db,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # FxPluginBase — process()
    # ------------------------------------------------------------------

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Apply loudness automation to one audio buffer (audio-thread safe)."""
        if not self.enabled:
            return audio

        try:
            if audio.ndim == 1:
                audio = np.column_stack([audio, audio])
            elif audio.shape[1] == 1:
                audio = np.repeat(audio, 2, axis=1)

            left  = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
            right = np.ascontiguousarray(audio[:, 1], dtype=np.float32)

            proc = self._get_proc(sample_rate)
            out_l, out_r = proc.process_block(left, right)

            # Cache gain for the GUI meter — simple write, no Qt calls from this thread.
            try:
                self._cached_gain_db = float(proc.current_gain_db)
            except Exception:
                pass

            return np.column_stack([
                np.asarray(out_l, dtype=np.float32),
                np.asarray(out_r, dtype=np.float32),
            ])
        except Exception as exc:
            logger.warning("LoudnessAutomationPlugin.process() failed: %s", exc)
            return audio

    # ------------------------------------------------------------------
    # FxPluginBase — create_parameter_widget()
    # ------------------------------------------------------------------

    def create_parameter_widget(self):
        """Build and return the parameter control widget (GUI thread only)."""
        from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel
        from PySide6.QtCore import Qt, QTimer

        w, lay = _base_widget()
        grp = _group_box("LOUDNESS AUTOMATION")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        # Parameter slider definitions:
        # (label, attr_name, slider_lo, slider_hi, int_scale, display_decimals)
        param_defs = [
            ("Target dBFS",  "target_dbfs", -400,    0,  10, 1),
            ("Attack ms",    "attack_ms",      1,  500,  10, 1),
            ("Release ms",   "release_ms",    10, 5000,  10, 1),
            ("Kp",           "kp",             0,  100,  10, 2),
            ("Ki",           "ki",             0,   50, 100, 2),
            ("Kd",           "kd",             0,   50, 100, 2),
            ("Gain Min dB",  "gain_min_db",  -600,    0,  10, 1),
            ("Gain Max dB",  "gain_max_db",     0,  240,  10, 1),
        ]

        for label, attr, lo, hi, scale, dec in param_defs:
            cur_val = getattr(self, attr)
            init_int = int(round(cur_val * scale))
            # Clamp init_int to slider range to prevent setValue warnings.
            init_int = max(lo, min(hi, init_int))
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init_int)
            val_lbl.setText(f"{cur_val:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                self._push_params()
                self._notify()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        # ── Gain meter (read-only, updated by QTimer from the GUI thread) ────
        meter_row = QHBoxLayout()
        meter_lbl = QLabel("Gain Correction")
        meter_lbl.setFixedWidth(96)
        meter_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        gain_val_lbl = QLabel("  0.0 dB")
        gain_val_lbl.setStyleSheet(
            f"color:{_C['gold']}; font-size:9px; background:transparent;"
        )
        meter_row.addWidget(meter_lbl)
        meter_row.addWidget(gain_val_lbl)
        meter_row.addStretch()
        grp_lay.addLayout(meter_row)

        # Timer polls self._cached_gain_db at 10 Hz from the GUI thread.
        timer = QTimer(w)
        timer.setInterval(100)

        def _update_meter():
            db = max(-40.0, min(+20.0, self._cached_gain_db))
            gain_val_lbl.setText(f"{db:+.1f} dB")

        timer.timeout.connect(_update_meter)
        timer.start()

        lay.addWidget(grp)
        lay.addStretch()
        return w

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def set_params(self, params: dict) -> None:
        """Restore parameters and rebuild the live processor."""
        super().set_params(params)
        self._push_params()
