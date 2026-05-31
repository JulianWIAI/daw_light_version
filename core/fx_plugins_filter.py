"""
fx_plugins_filter.py  --  Auto-Filter / Envelope Filter FX rack plugin.
========================================================================
Wraps the C++ AutoFilter processor as an FxPluginBase subclass so it slots
directly into the dynamic FX rack insert system.

Plugin
------
AutoFilterPlugin
    Resonant multi-mode filter (Low-Pass / High-Pass / Band-Pass) with two
    modulation sources selectable from the UI:

    LFO  -- a built-in oscillator (sine / triangle / square / saw-up) sweeps
            the cutoff at a user-controlled rate and depth.

    Envelope Follower  -- tracks the amplitude of the incoming audio and
                         opens/closes the filter in response (classic auto-wah
                         / guitar-wah effect).

    Both sources can be active simultaneously and their offsets are summed.

UI controls
-----------
    Filter Mode   -- LP / HP / BP toggle buttons
    Cutoff Hz     -- base cutoff frequency slider
    Resonance (Q) -- resonance / self-oscillation slider
    Drive         -- pre-filter soft-clip amount
    Mod Source    -- LFO / Env / Both toggle buttons
    LFO Rate      -- oscillation speed (Hz)
    LFO Depth     -- modulation depth (fraction of max octave sweep)
    LFO Shape     -- Sine / Triangle / Square / Saw toggle buttons
    Env Attack    -- follower attack time (ms)
    Env Release   -- follower release time (ms)
    Env Depth     -- envelope modulation depth
    Wet           -- dry/wet mix

Follows the identical _CppPlugin pattern from fx_plugins_cpp.py.
"""

from __future__ import annotations

import logging
import sys
import os

import numpy as np

from .fx_plugin_base import FxPluginBase

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Import the compiled C++ extension
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
# Shared UI helpers
# ─────────────────────────────────────────────────────────────────────────────

_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "cyan":     "#00E5FF",
    "purple":   "#9945FF",
    "pink":     "#FF2D9E",
    "gold":     "#FFD700",
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
    """Mutually exclusive toggle button row."""
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
# AutoFilterPlugin
# ─────────────────────────────────────────────────────────────────────────────

class AutoFilterPlugin(FxPluginBase):
    """
    Resonant multi-mode filter with LFO and/or envelope follower modulation.

    Supports Low-Pass, High-Pass, and Band-Pass modes.
    The cutoff can be swept by an LFO (for rhythmic filter effects) or driven
    by an envelope follower that tracks the input's loudness (for wah-wah).
    """

    DISPLAY_NAME = "Auto-Filter"

    def __init__(self) -> None:
        super().__init__()
        self._processor = None
        self._processor_sr: int = 0

        # ── Filter ──────────────────────────────────────────────────────────
        self.filter_mode:  int   = 0       # 0=LP, 1=HP, 2=BP
        self.cutoff_hz:    float = 1000.0
        self.resonance:    float = 1.5
        self.drive:        float = 0.0

        # ── Modulation ─────────────────────────────────────────────────────
        self.mod_source:   int   = 0       # 0=LFO, 1=Env, 2=Both

        # LFO
        self.lfo_rate_hz:  float = 1.0
        self.lfo_depth:    float = 0.4
        self.lfo_shape:    int   = 0       # 0=Sine, 1=Tri, 2=Square, 3=Saw

        # Envelope follower
        self.env_attack_ms:  float = 10.0
        self.env_release_ms: float = 200.0
        self.env_depth:      float = 0.5

        # ── Output ──────────────────────────────────────────────────────────
        self.wet: float = 1.0

    # ── _CppPlugin pattern ────────────────────────────────────────────────

    def _make_processor(self, sample_rate: int):
        p = _dp.AutoFilter(float(sample_rate))
        self._apply_params(p)
        return p

    def _get_processor(self, sample_rate: int):
        if not _ensure_cpp():
            return None
        if self._processor is None or self._processor_sr != sample_rate:
            self._processor = self._make_processor(sample_rate)
            self._processor_sr = sample_rate
        return self._processor

    def _apply_params(self, p) -> None:
        """Push all current parameter values to the C++ object."""
        p.set_filter_mode(self.filter_mode)
        p.set_cutoff_hz(self.cutoff_hz)
        p.set_resonance(self.resonance)
        p.set_drive(self.drive)
        p.set_mod_source(self.mod_source)
        p.set_lfo_rate_hz(self.lfo_rate_hz)
        p.set_lfo_depth(self.lfo_depth)
        p.set_lfo_shape(self.lfo_shape)
        p.set_env_attack_ms(self.env_attack_ms)
        p.set_env_release_ms(self.env_release_ms)
        p.set_env_depth(self.env_depth)
        p.set_wet(self.wet)

    def _push(self) -> None:
        """Sync to live C++ object and fire change signal."""
        if self._processor is not None:
            try:
                self._apply_params(self._processor)
            except Exception:
                pass
        self._notify()

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
            logger.warning("AutoFilterPlugin.process() failed: %s", exc)
            return audio

    # ── UI ────────────────────────────────────────────────────────────────

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout, QLabel, QWidget

        w, lay = _base_widget()

        # ── Filter mode & core ────────────────────────────────────────────
        filter_grp = _group_box("FILTER")
        filter_lay = QVBoxLayout(filter_grp)
        filter_lay.setSpacing(4)

        # Mode selector: LP / HP / BP
        mode_lbl = QLabel("Mode:")
        mode_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        mode_ctr, mode_btns = _btn_group(filter_grp, ["LP", "HP", "BP"],
                                          default=self.filter_mode)
        for i, btn in enumerate(mode_btns):
            def _on_mode(checked, idx=i):
                if checked:
                    self.filter_mode = idx
                    self._push()
            btn.clicked.connect(_on_mode)

        filter_lay.addWidget(mode_lbl)
        filter_lay.addWidget(mode_ctr)

        # Cutoff, Resonance, Drive sliders
        filter_params = [
            # (label, attr, lo, hi, scale, init_int, decimals)
            ("Cutoff Hz",   "cutoff_hz",  20, 20000, 1.0,  int(self.cutoff_hz),           0),
            ("Resonance",   "resonance",   5,   120, 10.0, int(self.resonance * 10),       1),
            ("Drive",       "drive",       0,   100, 100.0,int(self.drive * 100),           2),
            ("Wet",         "wet",         0,   100, 100.0,int(self.wet * 100),             2),
        ]
        for label, attr, lo, hi, scale, init, dec in filter_params:
            row_w, slider, val_lbl = _param_row(filter_grp, label, lo, hi, init)
            val_lbl.setText(f"{init / scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v / s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            filter_lay.addWidget(row_w)

        lay.addWidget(filter_grp)

        # ── Modulation source ─────────────────────────────────────────────
        mod_grp = _group_box("MOD SOURCE")
        mod_lay = QVBoxLayout(mod_grp)
        mod_lay.setSpacing(4)

        src_lbl = QLabel("Source:")
        src_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        src_ctr, src_btns = _btn_group(mod_grp, ["LFO", "Env", "Both"],
                                        default=self.mod_source)

        # LFO and Envelope sections that show/hide depending on source
        lfo_section = QWidget(mod_grp)
        lfo_sec_lay = QVBoxLayout(lfo_section)
        lfo_sec_lay.setContentsMargins(0, 0, 0, 0)
        lfo_sec_lay.setSpacing(2)

        env_section = QWidget(mod_grp)
        env_sec_lay = QVBoxLayout(env_section)
        env_sec_lay.setContentsMargins(0, 0, 0, 0)
        env_sec_lay.setSpacing(2)

        def _update_section_visibility():
            lfo_section.setVisible(self.mod_source in (0, 2))
            env_section.setVisible(self.mod_source in (1, 2))

        for i, btn in enumerate(src_btns):
            def _on_src(checked, idx=i):
                if checked:
                    self.mod_source = idx
                    _update_section_visibility()
                    self._push()
            btn.clicked.connect(_on_src)

        mod_lay.addWidget(src_lbl)
        mod_lay.addWidget(src_ctr)

        # ── LFO sub-section ───────────────────────────────────────────────
        lfo_shape_lbl = QLabel("Shape:")
        lfo_shape_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        shape_ctr, shape_btns = _btn_group(
            lfo_section, ["Sine", "Tri", "Sqr", "Saw"], default=self.lfo_shape
        )
        for i, btn in enumerate(shape_btns):
            def _on_shape(checked, idx=i):
                if checked:
                    self.lfo_shape = idx
                    self._push()
            btn.clicked.connect(_on_shape)

        lfo_sec_lay.addWidget(lfo_shape_lbl)
        lfo_sec_lay.addWidget(shape_ctr)

        lfo_params = [
            ("LFO Rate Hz", "lfo_rate_hz", 1,  2000, 100.0, int(self.lfo_rate_hz * 100), 2),
            ("LFO Depth",   "lfo_depth",   0,   100, 100.0, int(self.lfo_depth * 100),   2),
        ]
        for label, attr, lo, hi, scale, init, dec in lfo_params:
            row_w, slider, val_lbl = _param_row(lfo_section, label, lo, hi, init)
            val_lbl.setText(f"{init / scale:.{dec}f}")

            def _lfo_cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v / s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_lfo_cb)
            lfo_sec_lay.addWidget(row_w)

        mod_lay.addWidget(lfo_section)

        # ── Envelope sub-section ──────────────────────────────────────────
        env_params = [
            ("Env Attack ms",  "env_attack_ms",   1,  5000, 1.0, int(self.env_attack_ms),  0),
            ("Env Release ms", "env_release_ms",  1, 50000, 10.0, int(self.env_release_ms * 10), 1),
            ("Env Depth",      "env_depth",        0,   100, 100.0, int(self.env_depth * 100), 2),
        ]
        for label, attr, lo, hi, scale, init, dec in env_params:
            row_w, slider, val_lbl = _param_row(env_section, label, lo, hi, init)
            val_lbl.setText(f"{init / scale:.{dec}f}")

            def _env_cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v / s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_env_cb)
            env_sec_lay.addWidget(row_w)

        mod_lay.addWidget(env_section)
        lay.addWidget(mod_grp)

        # Set initial section visibility
        _update_section_visibility()

        lay.addStretch()
        return w
