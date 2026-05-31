"""
fx_plugins_pitch.py  --  Pitch Correction and Pitch Shifter FX rack plugins.
=============================================================================
Wraps the two C++ pitch processors as FxPluginBase subclasses so they slot
directly into the dynamic FX rack insert system.

Plugins
-------
PitchCorrectorPlugin
    Auto-Tune style pitch correction.
    - YIN fundamental detection on each audio block.
    - Snaps to Major / Minor / Chromatic scales in any root key.
    - Retune Speed and Amount controls.
    - Live detected-note display updated every 100 ms via QTimer.

PitchShifterPlugin
    Manual pitch shifter with optional harmonizer voice.
    - Semitones (-12..+12) and Cents (-100..+100) controls.
    - Harmonizer enables a second parallel voice at a chosen interval.
    - Mix knob blends the harmony level.

Both follow the identical _CppPlugin pattern from fx_plugins_cpp.py:
  - Lazy C++ object construction in _make_processor(sample_rate)
  - _apply_params() pushes every parameter to the live C++ object
  - Slider / button callbacks update Python attrs, call _apply_params(), _notify()
"""

from __future__ import annotations

import logging
import sys
import os
from typing import Optional

import numpy as np

from .fx_plugin_base import FxPluginBase

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Import the compiled C++ extension (same strategy as other plugin files)
# ─────────────────────────────────────────────────────────────────────────────

_dp = None
_CPP_AVAILABLE = False


def _ensure_cpp() -> bool:
    global _dp, _CPP_AVAILABLE
    if _CPP_AVAILABLE:
        return True
    try:
        from . import daw_processors as _mod
        _dp = _mod
        _CPP_AVAILABLE = True
        return True
    except ImportError:
        pass
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
            "daw_processors not found. "
            "Build: cd cpp_processors && python setup.py build_ext --inplace"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Shared UI helpers  (palette + widget factories matching existing DAW style)
# ─────────────────────────────────────────────────────────────────────────────

_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "surface":  "#0E1430",
    "cyan":     "#00E5FF",
    "pink":     "#FF2D9E",
    "purple":   "#9945FF",
    "gold":     "#FFD700",
    "orange":   "#FF6B2B",
    "green":    "#00FF88",
    "text":     "#C8E6FF",
    "text_dim": "#3D5A80",
}

_SLIDER_STYLE = (
    "QSlider::groove:horizontal { height:4px;"
    " background:rgba(0,229,255,0.12); border-radius:2px; }"
    "QSlider::handle:horizontal { width:12px; height:12px; margin:-4px 0;"
    " background:#00E5FF; border-radius:6px; }"
    "QSlider::sub-page:horizontal { background:rgba(0,229,255,0.35);"
    " border-radius:2px; }"
)


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


def _param_row(parent, label: str, lo: int, hi: int, init: int):
    """Return (container_widget, QSlider, value_QLabel)."""
    from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QSlider
    from PySide6.QtCore import Qt
    container = QWidget(parent)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(6)
    name_lbl = QLabel(label)
    name_lbl.setFixedWidth(100)
    name_lbl.setStyleSheet(
        f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
    )
    slider = QSlider(Qt.Horizontal)
    slider.setRange(lo, hi)
    slider.setValue(init)
    slider.setStyleSheet(_SLIDER_STYLE)
    val_lbl = QLabel(str(init))
    val_lbl.setFixedWidth(44)
    val_lbl.setAlignment(Qt.AlignRight)
    val_lbl.setStyleSheet(
        f"color:{_C['cyan']}; font-size:9px; background:transparent;"
    )
    row.addWidget(name_lbl)
    row.addWidget(slider)
    row.addWidget(val_lbl)
    return container, slider, val_lbl


def _btn_group(parent, labels: list, default: int = 0):
    """Mutually exclusive toggle button group."""
    from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton
    container = QWidget(parent)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(3)
    buttons = []
    for i, label in enumerate(labels):
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setChecked(i == default)
        btn.setFixedHeight(22)
        btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid rgba(153,69,255,0.3); border-radius:4px;"
            f" color:{_C['text_dim']}; font-size:9px; }}"
            f"QPushButton:checked {{ background:rgba(153,69,255,0.25);"
            f" border-color:rgba(153,69,255,0.8); color:#C8E6FF; }}"
        )
        row.addWidget(btn)
        buttons.append(btn)

    def _make_handler(idx):
        def _on_click(_checked):
            for j, b in enumerate(buttons):
                b.blockSignals(True)
                b.setChecked(j == idx)
                b.blockSignals(False)
        return _on_click

    for i, btn in enumerate(buttons):
        btn.clicked.connect(_make_handler(i))
    return container, buttons


# ─────────────────────────────────────────────────────────────────────────────
# Base for both pitch C++ plugins
# ─────────────────────────────────────────────────────────────────────────────

class _CppPitchPlugin(FxPluginBase):
    """Shared process() implementation for pitch C++ plugins."""

    def __init__(self) -> None:
        super().__init__()
        self._processor = None
        self._processor_sr: int = 0

    def _make_processor(self, sample_rate: int):
        raise NotImplementedError

    def _get_processor(self, sample_rate: int):
        if not _ensure_cpp():
            return None
        if self._processor is None or self._processor_sr != sample_rate:
            self._processor = self._make_processor(sample_rate)
            self._processor_sr = sample_rate
        return self._processor

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Split stereo, pass to C++ processor, recombine."""
        proc = self._get_processor(sample_rate)
        if proc is None:
            return audio
        try:
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


# ─────────────────────────────────────────────────────────────────────────────
# 1. Pitch Corrector  (Auto-Tune style)
# ─────────────────────────────────────────────────────────────────────────────

# Note names for the detected-note display.
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_ROOT_NAMES = _NOTE_NAMES  # same list, reused for root selector


def _hz_to_note_name(hz: float) -> str:
    """Convert a frequency in Hz to the nearest MIDI note name (e.g. 'A4 440 Hz')."""
    if hz <= 0.0:
        return "— (unvoiced)"
    midi = 12.0 * (hz / 440.0).__class__.__mro__[0]  # avoid log import at top
    import math
    midi = 69.0 + 12.0 * math.log2(hz / 440.0)
    note_idx = round(midi) % 12
    octave    = round(midi) // 12 - 1
    return f"{_NOTE_NAMES[note_idx]}{octave}  ({hz:.1f} Hz)"


class PitchCorrectorPlugin(_CppPitchPlugin):
    """
    Auto-Tune style pitch correction.

    The C++ backend runs YIN pitch detection every 256 samples and snaps the
    detected frequency to the nearest note in the chosen scale using a
    circular-buffer OLA pitch shifter.

    A live note display polls get_detected_hz() / get_target_hz() every 100 ms
    via a QTimer so the user can see what the detector is hearing.
    """

    DISPLAY_NAME = "Pitch Corrector"

    # Musical scale names shown in the picker.
    _SCALE_NAMES = ["Major", "Minor", "Chromatic"]

    def __init__(self) -> None:
        super().__init__()
        self.scale:        int   = 2      # 0=Major, 1=Minor, 2=Chromatic
        self.root:         int   = 0      # 0=C .. 11=B
        self.retune_speed: float = 0.1    # 0=instant, 1=slow
        self.amount:       float = 1.0    # 0=off, 1=full correction
        self.output_gain:  float = 0.0    # dB trim

    def _make_processor(self, sample_rate: int):
        p = _dp.PitchCorrector(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        """Push all current parameter values to the C++ object."""
        p.set_scale(self.scale)
        p.set_root(self.root)
        p.set_retune_speed(self.retune_speed)
        p.set_amount(self.amount)
        p.set_output_gain(self.output_gain)

    def _push(self) -> None:
        """Helper: sync params to live C++ object and fire change signal."""
        if self._processor is not None:
            try:
                self._apply_params(self._processor)
            except Exception:
                pass
        self._notify()

    def create_parameter_widget(self):
        from PySide6.QtWidgets import (
            QVBoxLayout, QLabel, QWidget, QHBoxLayout
        )
        from PySide6.QtCore import Qt, QTimer

        w, lay = _base_widget()

        # ── Scale configuration group ──────────────────────────────────────
        scale_grp = _group_box("SCALE & ROOT")
        scale_lay = QVBoxLayout(scale_grp)
        scale_lay.setSpacing(4)

        # Scale type selector (Major / Minor / Chromatic)
        scale_lbl = QLabel("Scale:")
        scale_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        scale_ctr, scale_btns = _btn_group(scale_grp, self._SCALE_NAMES,
                                            default=self.scale)
        for i, btn in enumerate(scale_btns):
            def _on_scale(checked, idx=i):
                if checked:
                    self.scale = idx
                    self._push()
            btn.clicked.connect(_on_scale)

        scale_lay.addWidget(scale_lbl)
        scale_lay.addWidget(scale_ctr)

        # Root note selector (C .. B, split across two rows for compactness)
        root_lbl = QLabel("Root:")
        root_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        root_row1_ctr, root_row1_btns = _btn_group(
            scale_grp, _ROOT_NAMES[:6], default=self.root if self.root < 6 else -1
        )
        root_row2_ctr, root_row2_btns = _btn_group(
            scale_grp, _ROOT_NAMES[6:], default=self.root - 6 if self.root >= 6 else -1
        )
        all_root_btns = root_row1_btns + root_row2_btns

        def _make_root_handler(note_idx):
            def _on_root(checked):
                if checked:
                    self.root = note_idx
                    # Uncheck all other root buttons
                    for j, b in enumerate(all_root_btns):
                        b.blockSignals(True)
                        b.setChecked(j == note_idx)
                        b.blockSignals(False)
                    self._push()
            return _on_root

        for i, btn in enumerate(all_root_btns):
            btn.clicked.connect(_make_root_handler(i))

        scale_lay.addWidget(root_lbl)
        scale_lay.addWidget(root_row1_ctr)
        scale_lay.addWidget(root_row2_ctr)
        lay.addWidget(scale_grp)

        # ── Correction controls group ──────────────────────────────────────
        corr_grp = _group_box("CORRECTION")
        corr_lay = QVBoxLayout(corr_grp)
        corr_lay.setSpacing(4)

        params = [
            # (label, attr, lo, hi, scale, init_int, decimals)
            ("Retune Speed", "retune_speed", 0, 100, 100.0, int(self.retune_speed * 100), 2),
            ("Amount",       "amount",       0, 100, 100.0, int(self.amount * 100),       2),
            ("Output dB",    "output_gain", -240, 120, 10.0, int(self.output_gain * 10),  1),
        ]
        for label, attr, lo, hi, scale, init, dec in params:
            row_w, slider, val_lbl = _param_row(corr_grp, label, lo, hi, init)
            val_lbl.setText(f"{init / scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v / s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            corr_lay.addWidget(row_w)

        lay.addWidget(corr_grp)

        # ── Live detected note display ─────────────────────────────────────
        info_grp = _group_box("DETECTED PITCH")
        info_lay = QVBoxLayout(info_grp)
        info_lay.setSpacing(3)

        detected_lbl = QLabel("Detected:  —")
        detected_lbl.setAlignment(Qt.AlignCenter)
        detected_lbl.setStyleSheet(
            f"color:{_C['cyan']}; font-size:9px; background:transparent;"
        )
        target_lbl = QLabel("Target:      —")
        target_lbl.setAlignment(Qt.AlignCenter)
        target_lbl.setStyleSheet(
            f"color:{_C['gold']}; font-size:9px; background:transparent;"
        )
        info_lay.addWidget(detected_lbl)
        info_lay.addWidget(target_lbl)
        lay.addWidget(info_grp)

        # Poll detected / target Hz every 100 ms from the GUI thread.
        plugin_ref = self

        def _poll():
            if plugin_ref._processor is not None:
                try:
                    det = plugin_ref._processor.get_detected_hz()
                    tgt = plugin_ref._processor.get_target_hz()
                    detected_lbl.setText(f"Detected:  {_hz_to_note_name(det)}")
                    target_lbl.setText(  f"Target:     {_hz_to_note_name(tgt)}")
                except Exception:
                    pass

        timer = QTimer(w)
        timer.timeout.connect(_poll)
        timer.start(100)
        # Keep timer alive as long as widget exists.
        w._pitch_poll_timer = timer

        lay.addStretch()
        return w


# ─────────────────────────────────────────────────────────────────────────────
# 2. Pitch Shifter + Harmonizer
# ─────────────────────────────────────────────────────────────────────────────

# Common harmony interval presets (display name → semitones).
_HARMONY_PRESETS = [
    ("min 3rd  +3",   3),
    ("maj 3rd  +4",   4),
    ("4th      +5",   5),
    ("5th      +7",   7),
    ("Octave  +12",  12),
    ("Octave  -12", -12),
    ("maj 3rd  -4",  -4),
    ("5th      -7",  -7),
]


class PitchShifterPlugin(_CppPitchPlugin):
    """
    Manual pitch shifter: shift by ±12 semitones + ±100 cents, independent
    of playback speed.

    Optional harmonizer adds a second pitch-shifted voice (default: a perfect
    fifth above the original) blended via the Mix knob.
    """

    DISPLAY_NAME = "Pitch Shifter"

    def __init__(self) -> None:
        super().__init__()
        self.semitones:       int   = 0
        self.cents:           int   = 0
        self.harmonizer:      bool  = False
        self.harmony_semi:    int   = 7      # perfect fifth
        self.mix:             float = 0.5
        self.output_gain:     float = 0.0    # dB

    def _make_processor(self, sample_rate: int):
        p = _dp.PitchShifter(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_semitones(self.semitones)
        p.set_cents(self.cents)
        p.set_harmonizer(self.harmonizer)
        p.set_harmony_semitones(self.harmony_semi)
        p.set_mix(self.mix)
        p.set_output_gain(self.output_gain)

    def _push(self) -> None:
        if self._processor is not None:
            try:
                self._apply_params(self._processor)
            except Exception:
                pass
        self._notify()

    def create_parameter_widget(self):
        from PySide6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox, QWidget,
        )
        from PySide6.QtCore import Qt

        w, lay = _base_widget()

        # ── Pitch shift group ──────────────────────────────────────────────
        shift_grp = _group_box("PITCH SHIFT")
        shift_lay = QVBoxLayout(shift_grp)
        shift_lay.setSpacing(4)

        # Semitones: -12 .. +12
        semi_row, semi_sl, semi_lbl = _param_row(
            shift_grp, "Semitones", -12, 12, self.semitones
        )
        semi_lbl.setText(f"{self.semitones:+d}")

        def _on_semi(v):
            self.semitones = v
            semi_lbl.setText(f"{v:+d}")
            self._push()

        semi_sl.valueChanged.connect(_on_semi)
        shift_lay.addWidget(semi_row)

        # Cents: -100 .. +100
        cents_row, cents_sl, cents_lbl = _param_row(
            shift_grp, "Cents", -100, 100, self.cents
        )
        cents_lbl.setText(f"{self.cents:+d}")

        def _on_cents(v):
            self.cents = v
            cents_lbl.setText(f"{v:+d}")
            self._push()

        cents_sl.valueChanged.connect(_on_cents)
        shift_lay.addWidget(cents_row)

        # Output gain
        gain_row, gain_sl, gain_lbl = _param_row(
            shift_grp, "Output dB", -240, 120, int(self.output_gain * 10)
        )
        gain_lbl.setText(f"{self.output_gain:.1f}")

        def _on_gain(v):
            self.output_gain = v / 10.0
            gain_lbl.setText(f"{v / 10.0:.1f}")
            self._push()

        gain_sl.valueChanged.connect(_on_gain)
        shift_lay.addWidget(gain_row)
        lay.addWidget(shift_grp)

        # ── Harmonizer group ───────────────────────────────────────────────
        harm_grp = _group_box("HARMONIZER")
        harm_lay = QVBoxLayout(harm_grp)
        harm_lay.setSpacing(4)

        # Enable toggle
        harm_cb = QCheckBox("Enable Harmonizer")
        harm_cb.setChecked(self.harmonizer)
        harm_cb.setStyleSheet(f"color:{_C['text']}; font-size:9px;")

        # Harmony content (shown/hidden with the checkbox)
        harm_content = QWidget(harm_grp)
        harm_content_lay = QVBoxLayout(harm_content)
        harm_content_lay.setContentsMargins(0, 0, 0, 0)
        harm_content_lay.setSpacing(4)

        # Interval preset dropdown
        preset_row_w = QWidget(harm_content)
        preset_row_lay = QHBoxLayout(preset_row_w)
        preset_row_lay.setContentsMargins(0, 0, 0, 0)
        preset_lbl = QLabel("Interval:")
        preset_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        preset_combo = QComboBox()
        preset_combo.addItems([p[0] for p in _HARMONY_PRESETS])
        # Set default selection to the perfect fifth (+7)
        default_preset_idx = next(
            (i for i, p in enumerate(_HARMONY_PRESETS) if p[1] == self.harmony_semi), 3
        )
        preset_combo.setCurrentIndex(default_preset_idx)
        preset_combo.setStyleSheet(
            f"QComboBox {{ background:{_C['deep']}; color:{_C['text']};"
            f" border:1px solid {_C['purple']}; border-radius:3px;"
            f" font-size:9px; padding:2px 6px; }}"
            f"QComboBox QAbstractItemView {{ background:{_C['deep']};"
            f" color:{_C['text']}; selection-background-color:{_C['purple']}; }}"
        )

        def _on_preset(idx):
            self.harmony_semi = _HARMONY_PRESETS[idx][1]
            self._push()

        preset_combo.currentIndexChanged.connect(_on_preset)
        preset_row_lay.addWidget(preset_lbl)
        preset_row_lay.addWidget(preset_combo, 1)
        harm_content_lay.addWidget(preset_row_w)

        # Mix knob (slider)
        mix_row, mix_sl, mix_lbl = _param_row(
            harm_content, "Mix", 0, 100, int(self.mix * 100)
        )
        mix_lbl.setText(f"{self.mix:.2f}")

        def _on_mix(v):
            self.mix = v / 100.0
            mix_lbl.setText(f"{v / 100.0:.2f}")
            self._push()

        mix_sl.valueChanged.connect(_on_mix)
        harm_content_lay.addWidget(mix_row)

        harm_content.setVisible(self.harmonizer)

        def _on_harm_toggle(state):
            self.harmonizer = bool(state)
            harm_content.setVisible(self.harmonizer)
            self._push()

        harm_cb.stateChanged.connect(_on_harm_toggle)
        harm_lay.addWidget(harm_cb)
        harm_lay.addWidget(harm_content)
        lay.addWidget(harm_grp)

        lay.addStretch()
        return w
