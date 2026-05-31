"""
fx_plugins_harmonic.py -- Harmonic & Character FX Rack Plugins.
================================================================
Wraps the four C++ harmonic processors as FxPluginBase subclasses so they
slot directly into the dynamic FX rack insert system.

Plugins:
    SaturationPlugin  -- Tape & Tube emulator with harmonic content meters
    OverdrivePlugin   -- Overdrive / Distortion / Fuzz with tone filter
    BitcrusherPlugin  -- Bit depth + sample-rate decimation with wet/dry
    ExciterPlugin     -- High-frequency harmonic exciter with air shelf

Each plugin follows the _CppHarmonicPlugin pattern:
  - Lazy C++ object construction on first process() call
  - _apply_params() pushes all current Python attributes to the live C++ object
  - Slider/button callbacks update the attribute, call _apply_params(), _notify()
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
# Import the compiled C++ extension (same lazy strategy as other plugin files)
# ─────────────────────────────────────────────────────────────────────────────

_dp = None
_CPP_AVAILABLE = False


def _ensure_cpp() -> bool:
    """Try to import daw_processors; return True if successful."""
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
            "daw_processors not found — harmonic plugins will be silent. "
            "Build: cd cpp_processors && python setup.py build_ext --inplace"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Shared UI helpers  (colours + widget factories matching the DAW palette)
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
    """Styled QGroupBox matching the DAW dark theme."""
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
    """Return (QWidget, QVBoxLayout) with the dark DAW background."""
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    w = QWidget()
    w.setStyleSheet(f"background:{_C['abyss']};")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(8, 8, 8, 8)
    lay.setSpacing(4)
    return w, lay


def _param_row(parent, label: str, lo: int, hi: int, init: int):
    """
    Return (container_widget, QSlider, value_QLabel).
    lo/hi/init are integers; the calling code scales to float as needed.
    """
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
    Return (container_widget, list[QPushButton]) with mutual exclusion.
    Clicking one button un-checks all others.
    """
    from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton
    container = QWidget(parent)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 2, 0, 2)
    row.setSpacing(3)
    buttons = []
    _btn_style = (
        f"QPushButton {{ background:{_C['deep']};"
        f" border:1px solid rgba(153,69,255,0.3); border-radius:4px;"
        f" color:{_C['text_dim']}; font-size:9px; }}"
        f"QPushButton:checked {{ background:rgba(153,69,255,0.25);"
        f" border-color:rgba(153,69,255,0.8); color:#C8E6FF; }}"
    )
    for i, label in enumerate(labels):
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setChecked(i == default)
        btn.setFixedHeight(22)
        btn.setStyleSheet(_btn_style)
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
# Shared C++ plugin base
# ─────────────────────────────────────────────────────────────────────────────

class _CppHarmonicPlugin(FxPluginBase):
    """
    Base class for harmonic C++ plugins.
    Mirrors _CppPlugin / _CppSpatialPlugin: lazy construction, L/R split.
    """

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
            # Ensure stereo layout (N, 2).
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

    def _push(self) -> None:
        """Push current param state to the live C++ object and fire callback."""
        if self._processor is not None:
            try:
                self._apply_params(self._processor)
            except Exception:
                pass
        self._notify()

    def _apply_params(self, p) -> None:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
# 1. Saturation / Tape & Tube Emulator
# ─────────────────────────────────────────────────────────────────────────────

class SaturationPlugin(_CppHarmonicPlugin):
    """
    Tape & Tube harmonic saturation with 2nd/3rd/4th harmonic content meters.

    TUBE mode uses an asymmetric biased-tanh waveshaper that produces even-order
    harmonics (2nd, 4th) whose strength is controlled by the Bias knob.
    Auto gain compensation keeps small-signal levels consistent at all drive values.

    TAPE mode uses symmetric tanh saturation paired with a drive-dependent
    low-pass filter that progressively rolls off high frequencies at higher drive,
    mimicking the oxide compression behaviour of analogue tape.
    """

    DISPLAY_NAME = "Saturation"

    def __init__(self) -> None:
        super().__init__()
        self.mode:     int   = 0     # 0=TUBE, 1=TAPE
        self.drive_db: float = 6.0   # dB (0..40)
        self.output_db:float = 0.0   # dB (-24..+12)
        self.bias:     float = 0.2   # tube asymmetry (0..1)
        # Polled by the harmonic meter QTimer; written by the audio thread.
        self._last_harm2: float = 0.0
        self._last_harm3: float = 0.0
        self._last_harm4: float = 0.0

    def _make_processor(self, sample_rate: int):
        p = _dp.Saturation(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_mode(self.mode)
        p.set_drive(self.drive_db)
        p.set_output(self.output_db)
        p.set_bias(self.bias)

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Override to capture harmonic meter values after each render block."""
        result = super().process(audio, sample_rate)
        if self._processor is not None:
            try:
                self._last_harm2 = self._processor.get_harm2()
                self._last_harm3 = self._processor.get_harm3()
                self._last_harm4 = self._processor.get_harm4()
            except Exception:
                pass
        return result

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QPainter, QColor, QLinearGradient

        plugin_ref = self

        # ── Harmonic content meter ────────────────────────────────────────────
        class HarmonicMeter(QWidget):
            """Three vertical bars showing 2nd, 3rd, 4th harmonic energy."""

            def __init__(self, parent=None):
                super().__init__(parent)
                self.setFixedSize(220, 48)
                timer = QTimer(self)
                timer.timeout.connect(self.update)
                timer.start(100)  # 10 fps is smooth enough for a meter

            def paintEvent(self, _event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing)
                w, h = self.width(), self.height()
                painter.fillRect(0, 0, w, h, QColor(_C["deep"]))

                # Retrieve current harmonic levels (GIL keeps this thread-safe).
                levels = [
                    min(1.0, max(0.0, plugin_ref._last_harm2)),
                    min(1.0, max(0.0, plugin_ref._last_harm3)),
                    min(1.0, max(0.0, plugin_ref._last_harm4)),
                ]
                labels = ["2nd", "3rd", "4th"]
                colours = [_C["cyan"], _C["purple"], _C["gold"]]

                bar_w = (w - 20) // 3
                for i, (level, lbl, col) in enumerate(zip(levels, labels, colours)):
                    bx = 10 + i * (bar_w + 3)
                    bar_h = int(level * (h - 20))
                    # Bar drawn bottom-up
                    painter.fillRect(bx, h - 10 - bar_h, bar_w, bar_h, QColor(col))
                    # Label
                    painter.setPen(QColor(_C["text_dim"]))
                    font = painter.font()
                    font.setPointSize(7)
                    painter.setFont(font)
                    painter.drawText(bx, h - 2, lbl)

        # ── Build widget ──────────────────────────────────────────────────────
        w, lay = _base_widget()

        # Mode selector
        mode_grp = _group_box("MODE")
        mode_lay = QVBoxLayout(mode_grp)
        mode_ctr, mode_btns = _btn_group(mode_grp, ["Tube", "Tape"], default=self.mode)
        for i, btn in enumerate(mode_btns):
            def _on_mode(checked, idx=i):
                if checked:
                    self.mode = idx
                    self._push()
            btn.clicked.connect(_on_mode)
        mode_lay.addWidget(mode_ctr)
        lay.addWidget(mode_grp)

        # Parameter sliders
        params_grp = _group_box("PARAMETERS")
        params_lay = QVBoxLayout(params_grp)
        params_lay.setSpacing(4)

        param_defs = [
            ("Drive dB",   "drive_db",  0,   400, int(self.drive_db * 10),  10.0, 1),
            ("Output dB",  "output_db", -240, 120, int(self.output_db * 10), 10.0, 1),
            ("Bias",       "bias",      0,   100, int(self.bias * 100),      100.0, 2),
        ]
        for label, attr, lo, hi, init, scale, dec in param_defs:
            row_w, slider, val_lbl = _param_row(params_grp, label, lo, hi, init)
            val_lbl.setText(f"{init / scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v / s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            params_lay.addWidget(row_w)

        lay.addWidget(params_grp)

        # Harmonic meter
        meter_grp = _group_box("HARMONIC CONTENT")
        meter_lay = QVBoxLayout(meter_grp)
        meter_lay.addWidget(HarmonicMeter(meter_grp))
        lay.addWidget(meter_grp)

        lay.addStretch()
        return w


# ─────────────────────────────────────────────────────────────────────────────
# 2. Distortion / Overdrive / Fuzz
# ─────────────────────────────────────────────────────────────────────────────

class OverdrivePlugin(_CppHarmonicPlugin):
    """
    Distortion / Overdrive / Fuzz with pre-gain, tone filter, and output trim.

    Three drive modes expose very different character:
      OVERDRIVE  -- Warm asymmetric soft-clip (diode-pair flavour)
      DISTORTION -- Hard symmetric hard-clip for dense upper harmonics
      FUZZ       -- Full-wave rectification for the classic "wall of fuzz" sound

    The tone filter is applied before the waveshaper so that it shapes which
    frequencies get clipped — a low-pass makes the distortion warmer and
    smoother, while a high-pass makes it tighter and more aggressive.
    """

    DISPLAY_NAME = "Overdrive / Fuzz"

    def __init__(self) -> None:
        super().__init__()
        self.mode:      int   = 0      # 0=OVERDRIVE, 1=DISTORTION, 2=FUZZ
        self.pregain_db:float = 18.0   # dB (0..60)
        self.tone_hz:   float = 3500.0 # Hz (200..8000)
        self.tone_type: int   = 0      # 0=LP, 1=HP, 2=tilt shelf
        self.output_db: float = -6.0   # dB (-24..+6)

    def _make_processor(self, sample_rate: int):
        p = _dp.Overdrive(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_mode(self.mode)
        p.set_pregain(self.pregain_db)
        p.set_tone(self.tone_hz)
        p.set_tone_type(self.tone_type)
        p.set_output(self.output_db)

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout, QLabel

        w, lay = _base_widget()

        # ── Drive mode buttons ────────────────────────────────────────────────
        mode_grp = _group_box("DRIVE MODE")
        mode_lay = QVBoxLayout(mode_grp)
        mode_ctr, mode_btns = _btn_group(
            mode_grp, ["Overdrive", "Distortion", "Fuzz"], default=self.mode
        )
        for i, btn in enumerate(mode_btns):
            def _on_mode(checked, idx=i):
                if checked:
                    self.mode = idx
                    self._push()
            btn.clicked.connect(_on_mode)
        mode_lay.addWidget(mode_ctr)
        lay.addWidget(mode_grp)

        # ── Tone filter type ──────────────────────────────────────────────────
        tone_grp = _group_box("TONE FILTER")
        tone_lay = QVBoxLayout(tone_grp)
        tone_type_ctr, tone_type_btns = _btn_group(
            tone_grp, ["Low-Pass", "High-Pass", "Tilt"], default=self.tone_type
        )
        for i, btn in enumerate(tone_type_btns):
            def _on_tt(checked, idx=i):
                if checked:
                    self.tone_type = idx
                    self._push()
            btn.clicked.connect(_on_tt)
        tone_lay.addWidget(tone_type_ctr)

        # Tone frequency slider
        tone_row, tone_sl, tone_lbl = _param_row(
            tone_grp, "Tone Hz", 200, 8000, int(self.tone_hz)
        )
        tone_lbl.setText(f"{int(self.tone_hz)}")

        def _on_tone_hz(v):
            self.tone_hz = float(v)
            tone_lbl.setText(f"{v}")
            self._push()

        tone_sl.valueChanged.connect(_on_tone_hz)
        tone_lay.addWidget(tone_row)
        lay.addWidget(tone_grp)

        # ── Main parameter sliders ────────────────────────────────────────────
        main_grp = _group_box("PARAMETERS")
        main_lay = QVBoxLayout(main_grp)
        main_lay.setSpacing(4)

        param_defs = [
            ("Pre-Gain dB", "pregain_db", 0,    600,  int(self.pregain_db * 10), 10.0, 1),
            ("Output dB",   "output_db",  -240, 60,   int(self.output_db * 10),  10.0, 1),
        ]
        for label, attr, lo, hi, init, scale, dec in param_defs:
            row_w, slider, val_lbl = _param_row(main_grp, label, lo, hi, init)
            val_lbl.setText(f"{init / scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v / s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            main_lay.addWidget(row_w)

        lay.addWidget(main_grp)
        lay.addStretch()
        return w


# ─────────────────────────────────────────────────────────────────────────────
# 3. Bitcrusher
# ─────────────────────────────────────────────────────────────────────────────

class BitcrusherPlugin(_CppHarmonicPlugin):
    """
    Bit depth and sample-rate decimation with wet/dry parallel mix.

    Bit depth reduction quantises the float samples to the resolution of an
    N-bit integer — 16-bit sounds like CD, 8-bit sounds like a Game Boy,
    and 1-bit is pure 1-bit noise.

    Sample-rate decimation uses a sample-and-hold to repeat each captured
    sample until the next trigger, producing the aliased "lo-fi" texture of
    vintage samplers and early digital audio.

    Optional triangular-PDF dithering adds low-level noise before quantisation
    to break up the harmonic structure of the quantisation distortion.
    """

    DISPLAY_NAME = "Bitcrusher"

    def __init__(self) -> None:
        super().__init__()
        self.bit_depth:   float = 16.0    # 1..24
        self.resample_hz: float = 44100.0 # 500..48000
        self.wet:         float = 1.0     # 0..1
        self.dither:      bool  = False

    def _make_processor(self, sample_rate: int):
        p = _dp.Bitcrusher(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_bit_depth(self.bit_depth)
        p.set_sample_rate_hz(self.resample_hz)
        p.set_wet(self.wet)
        p.set_dither(self.dither)

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout, QCheckBox

        w, lay = _base_widget()
        grp = _group_box("BITCRUSHER")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        param_defs = [
            # (label, attr, lo, hi, init_int, scale, decimals)
            ("Bit Depth",    "bit_depth",   10,  240,  int(self.bit_depth * 10),   10.0, 1),
            ("Sample Rate",  "resample_hz", 500, 48000, int(self.resample_hz),      1.0,  0),
            ("Wet",          "wet",         0,   100,  int(self.wet * 100),         100.0,2),
        ]
        for label, attr, lo, hi, init, scale, dec in param_defs:
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init / scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v / s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        # Dither toggle
        dither_cb = QCheckBox("Dithering")
        dither_cb.setChecked(self.dither)
        dither_cb.setStyleSheet(f"color:{_C['text']}; font-size:9px;")

        def _on_dither(state):
            self.dither = bool(state)
            self._push()

        dither_cb.stateChanged.connect(_on_dither)
        grp_lay.addWidget(dither_cb)

        lay.addWidget(grp)
        lay.addStretch()
        return w


# ─────────────────────────────────────────────────────────────────────────────
# 4. Exciter / Enhancer
# ─────────────────────────────────────────────────────────────────────────────

class ExciterPlugin(_CppHarmonicPlugin):
    """
    High-frequency harmonic exciter with adjustable crossover and air shelf.

    A Linkwitz-Riley 4th-order crossover isolates the high-frequency content
    above crossover_hz.  That band is then run through a normalised tanh
    waveshaper to generate new upper harmonics, and optionally boosted with a
    high-shelf EQ at 8 kHz (the "air" control).

    The low band is never processed — only HF is excited, preventing the muddy
    low-mid buildup that plagues cheap enhancers.

    Wet/dry mix blends the fully processed signal against the original so the
    effect can be dialled in subtly.
    """

    DISPLAY_NAME = "Exciter"

    def __init__(self) -> None:
        super().__init__()
        self.crossover_hz: float = 6000.0  # 3000..12000
        self.harmonics:    float = 0.5     # 0..1
        self.air_db:       float = 3.0     # 0..12 dB
        self.wet:          float = 0.5     # 0..1

    def _make_processor(self, sample_rate: int):
        p = _dp.Exciter(float(sample_rate))
        self._apply_params(p)
        return p

    def _apply_params(self, p) -> None:
        p.set_crossover_hz(self.crossover_hz)
        p.set_harmonics(self.harmonics)
        p.set_air(self.air_db)
        p.set_wet(self.wet)

    def create_parameter_widget(self):
        from PySide6.QtWidgets import QVBoxLayout

        w, lay = _base_widget()
        grp = _group_box("EXCITER")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        param_defs = [
            # (label, attr, lo, hi, init_int, scale, decimals)
            ("Crossover Hz", "crossover_hz", 3000, 12000, int(self.crossover_hz), 1.0,  0),
            ("Harmonics",    "harmonics",    0,    100,   int(self.harmonics*100), 100.0,2),
            ("Air dB",       "air_db",       0,    120,   int(self.air_db * 10),   10.0, 1),
            ("Wet",          "wet",          0,    100,   int(self.wet * 100),     100.0,2),
        ]
        for label, attr, lo, hi, init, scale, dec in param_defs:
            row_w, slider, val_lbl = _param_row(grp, label, lo, hi, init)
            val_lbl.setText(f"{init / scale:.{dec}f}")

            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, a, v / s)
                l.setText(f"{v / s:.{d}f}")
                self._push()

            slider.valueChanged.connect(_cb)
            grp_lay.addWidget(row_w)

        lay.addWidget(grp)
        lay.addStretch()
        return w
