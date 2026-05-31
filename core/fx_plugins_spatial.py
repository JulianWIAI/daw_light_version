"""
fx_plugins_spatial.py -- Spatial and Time-Based FX Rack Plugins.
=================================================================
Wraps the four C++ spatial processors as FxPluginBase subclasses so they
slot directly into the dynamic FX rack insert system.

Plugins:
    DelayEchoPlugin     -- BPM-synced delay, three modes (Stereo/Ping-Pong/Tape)
    FlangerPlugin       -- LFO-modulated short delay with stereo width control
    PhaserPlugin        -- Cascaded all-pass filter phaser (2–12 stages)
    StereoImagerPlugin  -- M/S width + LF mono lock + phase correlation meter

All UI code uses PySide6 (matching the existing DAW codebase) even though the
user's request mentioned PyQt5.  The two APIs are near-identical for the
widgets used here.

Each plugin follows the same _CppPlugin pattern from fx_plugins_cpp.py:
  - Lazy C++ object construction in _make_processor(sample_rate)
  - _apply_params() pushes every parameter to the live C++ object
  - Slider callbacks update the Python attribute, call _apply_params(), then _notify()
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
# Import the compiled C++ extension (same strategy as fx_plugins_cpp.py)
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
# Shared UI helpers (palette + widget factories)
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
    name_lbl.setFixedWidth(90)
    name_lbl.setStyleSheet(
        f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
    )
    slider = QSlider(Qt.Horizontal)
    slider.setRange(lo, hi)
    slider.setValue(init)
    slider.setStyleSheet(_SLIDER_STYLE)
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


def _btn_group(parent, labels: list, default: int = 0):
    """
    Return (container_widget, list[QPushButton]) — mutually exclusive toggle buttons.
    """
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
        style_off = (
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid rgba(153,69,255,0.3); border-radius:4px;"
            f" color:{_C['text_dim']}; font-size:9px; }}"
            f"QPushButton:checked {{ background:rgba(153,69,255,0.25);"
            f" border-color:rgba(153,69,255,0.8); color:#C8E6FF; }}"
        )
        btn.setStyleSheet(style_off)
        row.addWidget(btn)
        buttons.append(btn)
    # Mutual exclusion: clicking one unchecks the others.
    def _make_handler(idx):
        def _on_click(checked):
            for j, b in enumerate(buttons):
                b.blockSignals(True)
                b.setChecked(j == idx)
                b.blockSignals(False)
        return _on_click
    for i, btn in enumerate(buttons):
        btn.clicked.connect(_make_handler(i))
    return container, buttons


# ─────────────────────────────────────────────────────────────────────────────
# Shared C++ plugin base (identical pattern to fx_plugins_cpp._CppPlugin)
# ─────────────────────────────────────────────────────────────────────────────

class _CppSpatialPlugin(FxPluginBase):
    """Shared process() implementation for spatial C++ plugins."""

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
# 1. Delay / Echo
# ─────────────────────────────────────────────────────────────────────────────

class DelayEchoPlugin(_CppSpatialPlugin):
    """
    BPM-synced stereo delay with Ping-Pong and Tape modes.

    Tape mode adds sinusoidal pitch wobble (LFO on read position) and
    soft-clip saturation on the feedback path.
    """

    DISPLAY_NAME = "Delay / Echo"

    def __init__(self) -> None:
        super().__init__()
        self.bpm:         float = 120.0   # host BPM (0 = manual)
        self.division:    int   = 0       # 0=quarter, 1=dotted-8th, 2=eighth
        self.delay_ms:    float = 500.0   # manual delay (used when bpm==0)
        self.feedback:    float = 0.40
        self.wet:         float = 0.50
        self.hi_cut_hz:   float = 6000.0
        self.lo_cut_hz:   float = 150.0
        self.mode:        int   = 0       # 0=Stereo, 1=PingPong, 2=Tape
        self.tape_rate:   float = 0.5     # LFO rate Hz
        self.tape_depth:  float = 1.0     # pitch wobble depth in ms

    def _make_processor(self, sample_rate: int):
        p = _dp.DelayEcho(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_bpm(self.bpm)
        p.set_division(self.division)
        p.set_delay_ms(self.delay_ms)
        p.set_feedback(self.feedback)
        p.set_wet(self.wet)
        p.set_hi_cut(self.hi_cut_hz)
        p.set_lo_cut(self.lo_cut_hz)
        p.set_mode(self.mode)
        p.set_tape_rate(self.tape_rate)
        p.set_tape_depth(self.tape_depth)

    def _push(self) -> None:
        """Push current params to live C++ object and fire change callback."""
        if self._processor is not None:
            try:
                self._apply_params(self._processor)
            except Exception:
                pass
        self._notify()

    def create_parameter_widget(self):
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSlider
        )
        from PySide6.QtCore import Qt

        w, lay = _base_widget()

        # ── BPM + Division ────────────────────────────────────────────────────
        bpm_grp = _group_box("BPM SYNC")
        bpm_lay = QVBoxLayout(bpm_grp)
        bpm_lay.setSpacing(4)

        # BPM slider (60–200)
        bpm_row, bpm_sl, bpm_lbl = _param_row(bpm_grp, "BPM", 0, 300,
                                               int(self.bpm))
        bpm_lbl.setText(f"{self.bpm:.0f}")

        def _on_bpm(v):
            self.bpm = float(v)
            bpm_lbl.setText(f"{v:.0f}")
            self._push()

        bpm_sl.valueChanged.connect(_on_bpm)
        bpm_lay.addWidget(bpm_row)

        # Division buttons
        div_ctr, div_btns = _btn_group(
            bpm_grp, ["1/4", "d1/8", "1/8"], default=self.division
        )
        for i, btn in enumerate(div_btns):
            def _on_div(checked, idx=i):
                if checked:
                    self.division = idx
                    self._push()
            btn.clicked.connect(_on_div)

        div_lbl = QLabel("Division:")
        div_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        bpm_lay.addWidget(div_lbl)
        bpm_lay.addWidget(div_ctr)
        lay.addWidget(bpm_grp)

        # ── Mode ──────────────────────────────────────────────────────────────
        mode_grp = _group_box("MODE")
        mode_lay = QVBoxLayout(mode_grp)
        mode_ctr, mode_btns = _btn_group(
            mode_grp, ["Stereo", "Ping-Pong", "Tape"], default=self.mode
        )

        # Tape section (shown only in Tape mode).
        tape_widget = QWidget(mode_grp)
        tape_widget_lay = QVBoxLayout(tape_widget)
        tape_widget_lay.setContentsMargins(0, 0, 0, 0)
        tape_widget_lay.setSpacing(2)

        tape_rate_row, tape_rate_sl, tape_rate_lbl = _param_row(
            tape_widget, "Tape Rate Hz", 1, 100, int(self.tape_rate * 10)
        )
        tape_rate_lbl.setText(f"{self.tape_rate:.1f}")

        tape_depth_row, tape_depth_sl, tape_depth_lbl = _param_row(
            tape_widget, "Wobble ms", 0, 300, int(self.tape_depth * 10)
        )
        tape_depth_lbl.setText(f"{self.tape_depth:.1f}")

        def _on_tape_rate(v):
            self.tape_rate = v / 10.0
            tape_rate_lbl.setText(f"{self.tape_rate:.1f}")
            self._push()

        def _on_tape_depth(v):
            self.tape_depth = v / 10.0
            tape_depth_lbl.setText(f"{self.tape_depth:.1f}")
            self._push()

        tape_rate_sl.valueChanged.connect(_on_tape_rate)
        tape_depth_sl.valueChanged.connect(_on_tape_depth)
        tape_widget_lay.addWidget(tape_rate_row)
        tape_widget_lay.addWidget(tape_depth_row)
        tape_widget.setVisible(self.mode == 2)

        for i, btn in enumerate(mode_btns):
            def _on_mode(checked, idx=i):
                if checked:
                    self.mode = idx
                    tape_widget.setVisible(idx == 2)
                    self._push()
            btn.clicked.connect(_on_mode)

        mode_lay.addWidget(mode_ctr)
        mode_lay.addWidget(tape_widget)
        lay.addWidget(mode_grp)

        # ── Main params ────────────────────────────────────────────────────────
        main_grp = _group_box("PARAMETERS")
        main_lay = QVBoxLayout(main_grp)
        main_lay.setSpacing(4)

        param_defs = [
            ("Feedback", "feedback", 0, 99, int(self.feedback * 100), 100.0, 2),
            ("Wet",      "wet",      0, 100, int(self.wet * 100),      100.0, 2),
            ("Hi Cut Hz","hi_cut_hz",500, 20000, int(self.hi_cut_hz),  1.0, 0),
            ("Lo Cut Hz","lo_cut_hz",20, 2000,  int(self.lo_cut_hz),   1.0, 0),
        ]
        for label, attr, lo, hi, init, scale, dec in param_defs:
            row_w, slider, val_lbl = _param_row(main_grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            main_lay.addWidget(row_w)

        lay.addWidget(main_grp)
        lay.addStretch()
        return w


# ─────────────────────────────────────────────────────────────────────────────
# 2. Flanger
# ─────────────────────────────────────────────────────────────────────────────

class FlangerPlugin(_CppSpatialPlugin):
    """
    Classic comb-filter flanger with LFO-modulated short delay line.
    Stereo width is implemented via a per-channel LFO phase offset.
    """

    DISPLAY_NAME = "Flanger"

    def __init__(self) -> None:
        super().__init__()
        self.rate_hz:       float = 0.5
        self.depth_ms:      float = 2.0
        self.center_ms:     float = 5.0
        self.feedback:      float = 0.5
        self.wet:           float = 0.5
        self.waveform:      int   = 0    # 0=sine, 1=triangle, 2=square
        self.stereo_width:  float = 0.5  # 0=mono, 1=full stereo

    def _make_processor(self, sample_rate: int):
        p = _dp.Flanger(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_rate(self.rate_hz)
        p.set_depth(self.depth_ms)
        p.set_center(self.center_ms)
        p.set_feedback(self.feedback)
        p.set_wet(self.wet)
        p.set_waveform(self.waveform)
        p.set_stereo_width(self.stereo_width)

    def _push(self) -> None:
        if self._processor is not None:
            try:
                self._apply_params(self._processor)
            except Exception:
                pass
        self._notify()

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout, QLabel

        w, lay = _base_widget()
        grp = _group_box("FLANGER")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        # Waveform selector
        wf_ctr, wf_btns = _btn_group(grp, ["Sine", "Triangle", "Square"],
                                      default=self.waveform)
        wf_lbl = QLabel("Waveform:")
        wf_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        for i, btn in enumerate(wf_btns):
            def _on_wf(checked, idx=i):
                if checked:
                    self.waveform = idx
                    self._push()
            btn.clicked.connect(_on_wf)

        grp_lay.addWidget(wf_lbl)
        grp_lay.addWidget(wf_ctr)

        # Sliders
        params = [
            ("Rate Hz",      "rate_hz",      1,   2000, int(self.rate_hz * 100),      100.0, 2),
            ("Depth ms",     "depth_ms",     0,   100,  int(self.depth_ms * 10),       10.0, 1),
            ("Center ms",    "center_ms",    1,   80,   int(self.center_ms * 10),      10.0, 1),
            ("Feedback",     "feedback",    -95,  95,   int(self.feedback * 100),      100.0, 2),
            ("Wet",          "wet",          0,   100,  int(self.wet * 100),           100.0, 2),
            ("Stereo Width", "stereo_width", 0,   100,  int(self.stereo_width * 100),  100.0, 2),
        ]
        for label, attr, lo, hi, init, scale, dec in params:
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ─────────────────────────────────────────────────────────────────────────────
# 3. Phaser
# ─────────────────────────────────────────────────────────────────────────────

class PhaserPlugin(_CppSpatialPlugin):
    """
    Multi-stage all-pass phaser with logarithmic frequency sweep and stereo width.
    """

    DISPLAY_NAME = "Phaser"

    def __init__(self) -> None:
        super().__init__()
        self.stages:         int   = 4
        self.rate_hz:        float = 0.5
        self.depth:          float = 0.8
        self.min_freq_hz:    float = 200.0
        self.max_freq_hz:    float = 2000.0
        self.feedback:       float = 0.70
        self.wet:            float = 0.5
        self.stereo_offset:  float = 0.25  # 0.25 cycles = 90°

    def _make_processor(self, sample_rate: int):
        p = _dp.Phaser(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_stages(self.stages)
        p.set_rate(self.rate_hz)
        p.set_depth(self.depth)
        p.set_min_freq(self.min_freq_hz)
        p.set_max_freq(self.max_freq_hz)
        p.set_feedback(self.feedback)
        p.set_wet(self.wet)
        p.set_stereo_offset(self.stereo_offset)

    def _push(self) -> None:
        if self._processor is not None:
            try:
                self._apply_params(self._processor)
            except Exception:
                pass
        self._notify()

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout, QLabel

        w, lay = _base_widget()
        grp = _group_box("PHASER")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        # Stage count selector
        stage_ctr, stage_btns = _btn_group(
            grp, ["2", "4", "6", "8", "12"],
            default={2: 0, 4: 1, 6: 2, 8: 3, 12: 4}.get(self.stages, 1)
        )
        stage_lbl = QLabel("Stages:")
        stage_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        stage_values = [2, 4, 6, 8, 12]
        for i, btn in enumerate(stage_btns):
            def _on_stages(checked, n=stage_values[i]):
                if checked:
                    self.stages = n
                    self._push()
            btn.clicked.connect(_on_stages)

        grp_lay.addWidget(stage_lbl)
        grp_lay.addWidget(stage_ctr)

        # Sliders
        params = [
            ("Rate Hz",       "rate_hz",       1,   1000, int(self.rate_hz * 100),      100.0, 2),
            ("Depth",         "depth",         0,   100,  int(self.depth * 100),         100.0, 2),
            ("Min Freq Hz",   "min_freq_hz",   10,  2000, int(self.min_freq_hz),         1.0, 0),
            ("Max Freq Hz",   "max_freq_hz",   500, 16000,int(self.max_freq_hz),         1.0, 0),
            ("Feedback",      "feedback",      0,   98,   int(self.feedback * 100),      100.0, 2),
            ("Wet",           "wet",           0,   100,  int(self.wet * 100),           100.0, 2),
            ("Stereo Offset", "stereo_offset", 0,   100,  int(self.stereo_offset * 100), 100.0, 2),
        ]
        for label, attr, lo, hi, init, scale, dec in params:
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init/scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v/s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ─────────────────────────────────────────────────────────────────────────────
# 4. Stereo Imager
# ─────────────────────────────────────────────────────────────────────────────

class _CorrelationMeter(object):
    """
    Lightweight helper that holds a reference to a StereoImagerPlugin and
    paints a phase correlation bar using Qt.

    Kept as a plain QWidget subclass created inside create_parameter_widget()
    to avoid importing PySide6 at module level.
    """
    pass  # Actual class is defined inside create_parameter_widget() below.


class StereoImagerPlugin(_CppSpatialPlugin):
    """
    M/S stereo width processor with LF mono lock and phase correlation meter.

    Width:
        0.0  = mono (Side channel zeroed)
        1.0  = original stereo (unity)
        2.0  = doubled stereo width

    LF Mono Lock:
        Splits at crossover_hz using a Linkwitz-Riley 4th-order crossover.
        Content below the crossover is summed to mono to prevent sub-bass
        phase issues on mono playback systems.

    Correlation meter:
        After each render the C++ side computes the Pearson correlation of
        the output L and R channels.  The UI meter polls this value every
        200 ms via a QTimer and displays a colour-coded horizontal bar.
    """

    DISPLAY_NAME = "Stereo Imager"

    def __init__(self) -> None:
        super().__init__()
        self.width:          float = 1.0    # 0..2
        self.lf_mono_lock:   bool  = True
        self.crossover_hz:   float = 200.0
        self._last_correlation: float = 0.0  # updated by process() from bg thread

    def _make_processor(self, sample_rate: int):
        p = _dp.StereoImager(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_width(self.width)
        p.set_lf_mono_lock(self.lf_mono_lock)
        p.set_crossover_hz(self.crossover_hz)

    def _push(self) -> None:
        if self._processor is not None:
            try:
                self._apply_params(self._processor)
            except Exception:
                pass
        self._notify()

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Override to also capture the correlation value after each render."""
        result = super().process(audio, sample_rate)
        if self._processor is not None:
            try:
                # Store under the GIL — safe to read from the GUI thread.
                self._last_correlation = self._processor.get_correlation()
            except Exception:
                pass
        return result

    def create_parameter_widget(self):
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSlider,
        )
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QPainter, QColor

        plugin_ref = self  # capture for nested classes

        # ── Phase Correlation Meter widget ────────────────────────────────────
        class CorrelationMeter(QWidget):
            """Horizontal bar showing L/R phase correlation (−1 to +1)."""

            def __init__(self, parent=None):
                super().__init__(parent)
                self.setFixedSize(220, 36)
                # Poll the plugin's _last_correlation every 200 ms.
                self._timer = QTimer(self)
                self._timer.timeout.connect(self.update)
                self._timer.start(200)

            def paintEvent(self, _event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing)

                w, h = self.width(), self.height()
                cx = w // 2  # centre x = 0 correlation

                # Background
                painter.fillRect(0, 0, w, h, QColor(_C["deep"]))

                # Read current correlation (thread-safe float read under GIL).
                corr = max(-1.0, min(1.0, plugin_ref._last_correlation))

                # Draw correlation bar from centre.
                bar_w = int(abs(corr) * (cx - 2))
                bar_x = cx if corr >= 0 else cx - bar_w
                bar_color = QColor(_C["cyan"]) if corr >= 0 else QColor(_C["pink"])
                painter.fillRect(bar_x, 6, bar_w, h - 12, bar_color)

                # Centre line
                painter.setPen(QColor(_C["text_dim"]))
                painter.drawLine(cx, 0, cx, h)

                # Tick marks at −1, −0.5, 0, +0.5, +1
                for tick_val, tick_x in [
                    (-1.0, 2),
                    (-0.5, cx // 2),
                    (0.0, cx),
                    (0.5, cx + cx // 2),
                    (1.0, w - 2),
                ]:
                    painter.drawLine(int(tick_x), h - 8, int(tick_x), h)

                # Value text
                painter.setPen(QColor(_C["text"]))
                font = painter.font()
                font.setPointSize(8)
                painter.setFont(font)
                painter.drawText(0, 0, w, h, Qt.AlignCenter,
                                 f"Correlation: {corr:+.2f}")

        # ── Build the full parameter widget ───────────────────────────────────
        w, lay = _base_widget()
        grp = _group_box("STEREO IMAGER")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(6)

        # Width slider 0–200 (mapped to 0.0–2.0)
        width_row, width_sl, width_lbl = _param_row(
            grp, "Width %", 0, 200, int(self.width * 100)
        )
        width_lbl.setText(f"{int(self.width * 100)}%")

        def _on_width(v):
            self.width = v / 100.0
            width_lbl.setText(f"{v}%")
            self._push()

        width_sl.valueChanged.connect(_on_width)
        grp_lay.addWidget(width_row)

        # LF Mono Lock checkbox
        lf_cb = QCheckBox("LF Mono Lock")
        lf_cb.setChecked(self.lf_mono_lock)
        lf_cb.setStyleSheet(f"color:{_C['text']}; font-size:9px;")

        def _on_lf_lock(state):
            self.lf_mono_lock = bool(state)
            xover_row.setVisible(self.lf_mono_lock)
            self._push()

        lf_cb.stateChanged.connect(_on_lf_lock)
        grp_lay.addWidget(lf_cb)

        # Crossover Hz (visible only when LF mono lock is on)
        xover_row, xover_sl, xover_lbl = _param_row(
            grp, "Xover Hz", 20, 1000, int(self.crossover_hz)
        )
        xover_lbl.setText(f"{int(self.crossover_hz)} Hz")
        xover_row.setVisible(self.lf_mono_lock)

        def _on_xover(v):
            self.crossover_hz = float(v)
            xover_lbl.setText(f"{v} Hz")
            self._push()

        xover_sl.valueChanged.connect(_on_xover)
        grp_lay.addWidget(xover_row)

        # Phase correlation meter
        meter_lbl = QLabel("Phase Correlation")
        meter_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        grp_lay.addWidget(meter_lbl)
        grp_lay.addWidget(CorrelationMeter(grp))

        lay.addWidget(grp)
        lay.addStretch()
        return w
