"""
fx_plugins_spectral_panning.py  --  Spectral Panning & Masking Resolution plugin.
===================================================================================
Wraps SpectralPanningProcessorPython (or its C++ equivalent) as a FxPluginBase
subclass so it can be loaded into any track's FX rack slot.

How it works:
    Two tracks each load this plugin, configured to the SAME Group ID, with one
    set to Slot A and the other to Slot B.  The plugins share state through the
    module-level SpectralMaskingManagerPython singleton:

      • Both tracks' audio is analyzed per-block to extract the spectral centroid.
      • When the centroids are closer than the Tolerance threshold, the system
        detects frequency masking and applies equal-and-opposite pan shifts
        (A moves left, B moves right) proportional to the masking severity.
      • All pan transitions are smoothed through a one-pole LP filter to prevent
        jarring spatial jumps.

Parameter widget exposes:
    • Group ID (1–8) — integer linking two paired plugin instances.
    • Slot (A / B)   — which role this track plays.
    • Tolerance Hz   — masking detection threshold.
    • Max Pan        — maximum pan deflection (%).
    • Smooth ms      — LP filter time constant for pan transitions.
    • Live centroid display (Hz) — updated by QTimer.
    • Live pan display  — updated by QTimer.

Threading:
    process() is called from the audio render thread.
    GUI labels are updated by QTimers polling cached values (no Qt calls in
    process()).
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from .fx_plugin_base import FxPluginBase
from .spectral_panning_python import get_spectral_panning_processor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "cyan":     "#00E5FF",
    "pink":     "#FF4DFF",
    "gold":     "#FFD700",
    "text_dim": "#3D5A80",
    "text":     "#E0F0FF",
}

# ---------------------------------------------------------------------------
# UI helpers (self-contained so the file has no UI imports at module level)
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
    slider.setValue(max(lo, min(hi, init)))
    slider.setStyleSheet(
        "QSlider::groove:horizontal { height:4px; background:rgba(0,229,255,0.15);"
        " border-radius:2px; }"
        "QSlider::handle:horizontal { width:12px; height:12px; margin:-4px 0;"
        " background:#00E5FF; border-radius:6px; }"
        "QSlider::sub-page:horizontal { background:rgba(0,229,255,0.4);"
        " border-radius:2px; }"
    )

    val_lbl = QLabel(str(init))
    val_lbl.setFixedWidth(48)
    # Use the proper Qt enum — PySide6 does not accept raw ints here.
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
        f"QGroupBox {{ border:1px solid rgba(255,77,255,0.3); border-radius:6px;"
        f" margin-top:10px; padding-top:6px; color:{_C['text_dim']};"
        f" font-size:9px; letter-spacing:1px; background:{_C['abyss']}; }}"
        f"QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 5px;"
        f" color:rgba(255,77,255,0.9); }}"
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


def _info_row(parent, label: str, init: str, colour: str):
    """A non-interactive display row: label + value (updated by QTimer)."""
    from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
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
    val_lbl = QLabel(init)
    val_lbl.setFixedWidth(80)
    val_lbl.setAlignment(Qt.AlignRight)
    val_lbl.setStyleSheet(
        f"color:{colour}; font-size:9px; background:transparent;"
    )

    row.addWidget(name_lbl)
    row.addWidget(val_lbl)
    row.addStretch()
    return container, val_lbl


# ---------------------------------------------------------------------------
# SpectralPanningPlugin
# ---------------------------------------------------------------------------

class SpectralPanningPlugin(FxPluginBase):
    """
    Spectral masking resolution via automated frequency-aware stereo panning.

    Pair two instances on different tracks with the same Group ID, setting one
    to Slot A and the other to Slot B.  When the tracks' spectral centroids fall
    within the Tolerance window, they are pushed apart in the stereo field.

    Serialisable parameters:
        group_id     : integer group (1–8) linking the two paired plugins.
        slot         : 0 = track A, 1 = track B.
        tolerance_hz : masking detection threshold (Hz).
        max_pan      : maximum pan shift (0 = off, 1 = full side).
        smooth_ms    : LP filter time constant (ms).
    """

    DISPLAY_NAME = "Spectral Panning"

    def __init__(self) -> None:
        super().__init__()

        # Serialisable DSP parameters.
        self.group_id:     int   = 1
        self.slot:         int   = 0       # 0 = A, 1 = B
        self.tolerance_hz: float = 300.0
        self.max_pan:      float = 0.5
        self.smooth_ms:    float = 100.0

        # Live processor — lazily created on first process() call.
        self._proc     = None
        self._proc_sr: int = 0

        # Thread-safe cache for GUI metering (written from audio thread via simple float).
        self._cached_centroid: float = 0.0
        self._cached_pan:      float = 0.0

    # ------------------------------------------------------------------
    # Processor lifecycle
    # ------------------------------------------------------------------

    def _get_proc(self, sample_rate: int):
        """Return the live processor, recreating when the sample rate changes."""
        if self._proc is None or self._proc_sr != sample_rate:
            self._proc = get_spectral_panning_processor(
                sample_rate=float(sample_rate),
                group_id=self.group_id,
                slot=self.slot,
                tolerance_hz=self.tolerance_hz,
                max_pan=self.max_pan,
                smooth_ms=self.smooth_ms,
            )
            self._proc_sr = sample_rate
        return self._proc

    def _push_params(self) -> None:
        """Forward current parameter values to the live processor."""
        if self._proc is None:
            return
        try:
            self._proc.set_params(
                group_id=self.group_id,
                slot=self.slot,
                tolerance_hz=self.tolerance_hz,
                max_pan=self.max_pan,
                smooth_ms=self.smooth_ms,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # FxPluginBase — process()
    # ------------------------------------------------------------------

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Apply spectral panning automation to one audio buffer."""
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

            # Cache values for the GUI meter — CPython GIL makes float write safe.
            try:
                self._cached_centroid = float(proc.centroid)
                self._cached_pan      = float(proc.current_pan)
            except Exception:
                pass

            return np.column_stack([
                np.asarray(out_l, dtype=np.float32),
                np.asarray(out_r, dtype=np.float32),
            ])

        except Exception as exc:
            logger.warning("SpectralPanningPlugin.process() failed: %s", exc)
            return audio

    # ------------------------------------------------------------------
    # FxPluginBase — create_parameter_widget()
    # ------------------------------------------------------------------

    def create_parameter_widget(self):
        """Build and return the parameter control widget (GUI thread only)."""
        from PySide6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        )
        from PySide6.QtCore import Qt, QTimer

        w, lay = _base_widget()
        grp = _group_box("SPECTRAL PANNING")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        # ── Group ID (1–8) ────────────────────────────────────────────────────
        row_gid, sl_gid, lbl_gid = _param_row(grp, "Group ID", 1, 8, self.group_id)
        lbl_gid.setText(str(self.group_id))

        def _on_group(v):
            self.group_id = v
            lbl_gid.setText(str(v))
            self._push_params()
            self._notify()

        sl_gid.valueChanged.connect(_on_group)
        grp_lay.addWidget(row_gid)

        # ── Slot A / B toggle ─────────────────────────────────────────────────
        slot_row = QHBoxLayout()
        slot_name = QLabel("Slot")
        slot_name.setFixedWidth(96)
        slot_name.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        btn_a = QPushButton("A")
        btn_b = QPushButton("B")
        for btn in (btn_a, btn_b):
            btn.setFixedWidth(40)
            btn.setFixedHeight(22)
            btn.setCheckable(True)
        btn_a.setChecked(self.slot == 0)
        btn_b.setChecked(self.slot == 1)

        def _slot_style(active: bool) -> str:
            if active:
                return (f"QPushButton {{ background:rgba(255,77,255,0.3);"
                        f" border:1px solid {_C['pink']}; border-radius:4px;"
                        f" color:{_C['pink']}; font-size:9px; }}")
            return (f"QPushButton {{ background:{_C['deep']};"
                    f" border:1px solid rgba(255,77,255,0.2); border-radius:4px;"
                    f" color:{_C['text_dim']}; font-size:9px; }}"
                    f"QPushButton:hover {{ border-color:{_C['pink']}; color:{_C['pink']}; }}")

        btn_a.setStyleSheet(_slot_style(self.slot == 0))
        btn_b.setStyleSheet(_slot_style(self.slot == 1))

        def _pick_a():
            self.slot = 0
            btn_a.setChecked(True);  btn_b.setChecked(False)
            btn_a.setStyleSheet(_slot_style(True));   btn_b.setStyleSheet(_slot_style(False))
            self._push_params(); self._notify()

        def _pick_b():
            self.slot = 1
            btn_b.setChecked(True);  btn_a.setChecked(False)
            btn_b.setStyleSheet(_slot_style(True));   btn_a.setStyleSheet(_slot_style(False))
            self._push_params(); self._notify()

        btn_a.clicked.connect(_pick_a)
        btn_b.clicked.connect(_pick_b)

        slot_row.addWidget(slot_name)
        slot_row.addWidget(btn_a)
        slot_row.addWidget(btn_b)
        slot_row.addStretch()
        grp_lay.addLayout(slot_row)

        # ── Tolerance Hz ──────────────────────────────────────────────────────
        row_tol, sl_tol, lbl_tol = _param_row(grp, "Tolerance Hz", 50, 2000,
                                               int(self.tolerance_hz))
        lbl_tol.setText(f"{self.tolerance_hz:.0f}")

        def _on_tol(v):
            self.tolerance_hz = float(v)
            lbl_tol.setText(f"{v:.0f}")
            self._push_params(); self._notify()

        sl_tol.valueChanged.connect(_on_tol)
        grp_lay.addWidget(row_tol)

        # ── Max Pan (0–100 %) ─────────────────────────────────────────────────
        row_pan, sl_pan, lbl_pan = _param_row(grp, "Max Pan %", 0, 100,
                                               int(self.max_pan * 100))
        lbl_pan.setText(f"{self.max_pan*100:.0f}%")

        def _on_pan(v):
            self.max_pan = v / 100.0
            lbl_pan.setText(f"{v:.0f}%")
            self._push_params(); self._notify()

        sl_pan.valueChanged.connect(_on_pan)
        grp_lay.addWidget(row_pan)

        # ── Smooth ms ─────────────────────────────────────────────────────────
        row_sm, sl_sm, lbl_sm = _param_row(grp, "Smooth ms", 10, 2000,
                                            int(self.smooth_ms))
        lbl_sm.setText(f"{self.smooth_ms:.0f}")

        def _on_sm(v):
            self.smooth_ms = float(v)
            lbl_sm.setText(f"{v:.0f}")
            self._push_params(); self._notify()

        sl_sm.valueChanged.connect(_on_sm)
        grp_lay.addWidget(row_sm)

        # ── Live centroid & pan meters ────────────────────────────────────────
        row_cent, val_cent = _info_row(grp, "Centroid",  " 0 Hz", _C['gold'])
        row_cpan, val_cpan = _info_row(grp, "Pan",       " 0.00", _C['pink'])
        grp_lay.addWidget(row_cent)
        grp_lay.addWidget(row_cpan)

        # QTimer refreshes meters at 10 Hz from the GUI thread (thread-safe).
        timer = QTimer(w)
        timer.setInterval(100)

        def _update_meters():
            c = self._cached_centroid
            p = self._cached_pan
            val_cent.setText(f"{c:.0f} Hz")
            val_cpan.setText(f"{p:+.2f}")

        timer.timeout.connect(_update_meters)
        timer.start()

        lay.addWidget(grp)
        lay.addStretch()
        return w

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def get_params(self) -> dict:
        """Include slot (int) and group_id (int) in serialisation."""
        return {
            "enabled":      self.enabled,
            "group_id":     self.group_id,
            "slot":         self.slot,
            "tolerance_hz": self.tolerance_hz,
            "max_pan":      self.max_pan,
            "smooth_ms":    self.smooth_ms,
        }

    def set_params(self, params: dict) -> None:
        """Restore parameters and rebuild the live processor."""
        super().set_params(params)
        # Restore int params that super() may coerce.
        if "group_id" in params:
            self.group_id = int(params["group_id"])
        if "slot" in params:
            self.slot = int(params["slot"]) & 1
        self._push_params()
