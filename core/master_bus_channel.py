"""
master_bus_channel.py -- PySide6 Master Bus Channel Strip
==========================================================
Vertical mixer strip bound to the C++ (or Python fallback) MasterBus object.

A QTimer polls peak_L() / peak_R() at 20 Hz and repaints the dual stereo
VU meter.  All parameter calls (set_gain / set_audition_mode / etc.) invoke
the C++ MasterBus setters directly — no Python processing loop runs in the
audio path.

Audition mode selector (top of strip):
  [MIX]  [PREV -7]  [STRM -14]
  Clicking any button calls bus.set_audition_mode(n) instantly; the C++
  atomic<int> makes the change visible to the audio thread within one block.

Classes
-------
_StereoMeter     -- Custom QPainter-based dual VU bar widget (private).
MasterBusChannel -- Full vertical channel strip (public, added to the mixer).
"""

from __future__ import annotations

import math
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from .master_bus_python import AUDITION_BYPASS, AUDITION_PREVIEW, AUDITION_STREAMING

# ── Colour palette (kept in sync with gui_windows.C) ─────────────────────────

_C = {
    "void":     "#030308",
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "surface":  "#0E1430",
    "cyan":     "#00E5FF",
    "purple":   "#9945FF",
    "lime":     "#39FF14",
    "orange":   "#FF6B2B",
    "pink":     "#FF2D9E",
    "text":     "#C8E6FF",
    "text_dim": "#3D5A80",
}

# Meter display range: anything below -60 dBFS reads as "silent".
_METER_MIN_DB = -60.0
_METER_MAX_DB =   0.0

# Per-mode accent colours used to tint the active audition button.
_MODE_COLOR = {
    AUDITION_BYPASS:    _C["text_dim"],   # neutral — normal mix
    AUDITION_PREVIEW:   _C["orange"],     # warm orange — louder / commercial
    AUDITION_STREAMING: _C["cyan"],       # cyan — clean streaming level
}

# Human-readable label for the mode indicator strip.
_MODE_LABEL = {
    AUDITION_BYPASS:    "MIX",
    AUDITION_PREVIEW:   "-7 LUFS",
    AUDITION_STREAMING: "-14 LUFS",
}


def _linear_to_db(linear: float) -> float:
    """Convert a linear amplitude (0–1+) to dBFS, clamped to _METER_MIN_DB."""
    if linear <= 0.0:
        return _METER_MIN_DB
    return max(_METER_MIN_DB, 20.0 * math.log10(linear))


def _db_to_fraction(db: float) -> float:
    """Map a dBFS value to [0, 1] for drawing the meter bar height."""
    return (db - _METER_MIN_DB) / (_METER_MAX_DB - _METER_MIN_DB)


# ── Private helper: dual VU meter ─────────────────────────────────────────────

class _StereoMeter(QWidget):
    """
    Custom QPainter dual VU meter (L + R bars side by side).

    Colour gradient (bottom → top):
      0 – 70 %   (–∞ to –9 dBFS)  cyan
      70 – 88 %  (–9 to –3 dBFS)  orange
      88 – 100 % (–3 to  0 dBFS)  hot pink
      > 100 %    (clip zone)       solid red indicator at the top
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._peak_L: float = 0.0
        self._peak_R: float = 0.0
        self.setMinimumHeight(110)
        self.setMinimumWidth(44)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )

    def set_peaks(self, peak_L: float, peak_R: float) -> None:
        """Update peak values and schedule a repaint."""
        self._peak_L = float(peak_L)
        self._peak_R = float(peak_R)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        w   = self.width()
        h   = self.height()
        bw  = (w - 6) // 2   # width of each individual bar
        gap = 6               # pixel gap between the two bars

        for i, peak in enumerate((self._peak_L, self._peak_R)):
            x = i * (bw + gap)
            self._draw_bar(p, x, 0, bw, h, peak)

        p.end()

    def _draw_bar(
        self, p: QPainter, x: int, y: int, w: int, h: int, peak: float
    ) -> None:
        """Draw one channel bar: background + filled gradient level + grid marks."""
        # Dark background track.
        p.fillRect(x, y, w, h, QColor(_C["deep"]))

        db        = _linear_to_db(peak)
        fraction  = _db_to_fraction(db)
        fill_frac = min(fraction, 1.05)   # allow slight overflow into clip zone

        if fill_frac > 0.0:
            bar_h = int(h * fill_frac)
            bar_y = h - bar_h

            # Gradient: cyan (bottom) → lime → orange → pink (top).
            grad = QLinearGradient(0, h, 0, 0)
            grad.setColorAt(0.00, QColor(_C["cyan"]))
            grad.setColorAt(0.70, QColor(_C["cyan"]))
            grad.setColorAt(0.70, QColor(_C["lime"]))
            grad.setColorAt(0.88, QColor(_C["orange"]))
            grad.setColorAt(0.88, QColor(_C["pink"]))
            grad.setColorAt(1.00, QColor(_C["pink"]))

            p.fillRect(x, bar_y, w, bar_h, QBrush(grad))

        # Solid red clip indicator when peak exceeds 0 dBFS.
        if peak > 1.0:
            p.fillRect(x, y, w, 3, QColor("#FF0000"))

        # Subtle grid marks at -6, -12, -18, -24, -36 dBFS.
        p.setPen(QPen(QColor(0, 0, 0, 120), 1))
        for mark_db in (-6.0, -12.0, -18.0, -24.0, -36.0):
            f  = _db_to_fraction(mark_db)
            ly = y + h - int(h * f)
            p.drawLine(x, ly, x + w, ly)


# ── Public widget: master bus channel strip ───────────────────────────────────

class MasterBusChannel(QFrame):
    """
    Vertical mixer strip for the C++ MasterBus with audition mode selector.

    Layout (top to bottom):
      • Cyan accent bar + "MASTER" label
      • Audition mode button group: [MIX] [PREV -7] [STRM -14]
      • Active mode indicator label
      • Stereo VU meter (L + R)
      • GAIN label + vertical fader + dB readout
      • LIMITER section: ON/OFF toggle + ceiling spinbox
        (disabled when an audition mode is active — those paths use fixed limits)

    Parameters
    ----------
    master_bus :
        A MasterBus instance (C++ dp.MasterBus or MasterBusPython).
    parent :
        Optional parent QWidget.
    """

    def __init__(self, master_bus, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._bus         = master_bus
        self._active_mode = AUDITION_BYPASS

        self.setFixedWidth(112)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f"""
            MasterBusChannel {{
                background: {_C["abyss"]};
                border: 1px solid rgba(0,229,255,0.25);
                border-left: 2px solid {_C["cyan"]};
                border-radius: 8px;
                margin: 2px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(5, 6, 5, 6)
        root.setSpacing(4)

        # ── Header ──────────────────────────────────────────────────────────
        bar = QFrame()
        bar.setFixedHeight(4)
        bar.setStyleSheet(
            f"background:{_C['cyan']}; border-radius:2px; border:none;"
        )
        root.addWidget(bar)

        title = QLabel("MASTER")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        title.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        title.setStyleSheet(
            f"color:{_C['cyan']}; background:transparent; letter-spacing:2px;"
        )
        root.addWidget(title)

        # ── Audition mode selector ───────────────────────────────────────────
        # Three exclusive toggle buttons; clicking one calls set_audition_mode()
        # on the C++ MasterBus via an atomic<int> write (no audio dropout).
        mode_sep = QLabel("AUDITION")
        mode_sep.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        mode_sep.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:7px; background:transparent;"
            f" letter-spacing:1px;"
        )
        root.addWidget(mode_sep)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(2)
        mode_row.setContentsMargins(0, 0, 0, 0)

        self._mode_group: QButtonGroup = QButtonGroup(self)
        self._mode_group.setExclusive(True)

        # Button definitions: (id, short label, tooltip, accent colour)
        _modes = [
            (AUDITION_BYPASS,    "MIX",  "Normal mix path — user limiter active",
             _C["text_dim"]),
            (AUDITION_PREVIEW,   "PREV", "Audition: -7 LUFS preview master (+7 dB)",
             _C["orange"]),
            (AUDITION_STREAMING, "STRM", "Audition: -14 LUFS streaming master (-1 dBFS TP)",
             _C["cyan"]),
        ]
        self._mode_btns: List[QPushButton] = []

        for mode_id, label, tip, color in _modes:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(mode_id == AUDITION_BYPASS)
            btn.setToolTip(tip)
            btn.setFixedHeight(20)
            btn.setStyleSheet(self._mode_btn_style(color, active=mode_id == AUDITION_BYPASS))
            self._mode_group.addButton(btn, mode_id)
            self._mode_btns.append(btn)
            mode_row.addWidget(btn, stretch=1)

        root.addLayout(mode_row)

        # Small text label showing the active target name.
        self._mode_indicator = QLabel("Mix (Bypass)")
        self._mode_indicator.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._mode_indicator.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:8px; background:transparent;"
        )
        root.addWidget(self._mode_indicator)

        # Connect mode group: idClicked(int) fires when a button is toggled on.
        self._mode_group.idClicked.connect(self._on_mode_changed)

        # ── Stereo VU meter ──────────────────────────────────────────────────
        self._meter = _StereoMeter()
        root.addWidget(self._meter)

        # ── Gain fader ───────────────────────────────────────────────────────
        lbl_vol = QLabel("GAIN")
        lbl_vol.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lbl_vol.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:8px; background:transparent;"
        )
        root.addWidget(lbl_vol)

        # Range 0–200 maps to gain 0.0–2.0 (100 = unity = 0 dB).
        self._fader = QSlider(Qt.Orientation.Vertical)
        self._fader.setRange(0, 200)
        self._fader.setValue(100)
        self._fader.setFixedHeight(88)
        self._fader.setStyleSheet(f"""
            QSlider::groove:vertical {{
                background:{_C["deep"]};
                width:6px; border-radius:3px;
            }}
            QSlider::handle:vertical {{
                background:{_C["cyan"]};
                border:none; height:12px; width:18px;
                margin:0 -6px; border-radius:4px;
            }}
            QSlider::sub-page:vertical {{
                background:{_C["surface"]}; border-radius:3px;
            }}
        """)
        self._fader.valueChanged.connect(self._on_fader_changed)
        root.addWidget(self._fader, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._gain_lbl = QLabel("0.0 dB")
        self._gain_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._gain_lbl.setStyleSheet(
            f"color:{_C['text']}; font-size:9px; background:transparent;"
        )
        root.addWidget(self._gain_lbl)

        # ── User limiter section ──────────────────────────────────────────────
        # Disabled when an audition mode is active (those paths use hardcoded
        # limits; changing these controls would have no effect on the output).
        lim_header = QLabel("LIMITER")
        lim_header.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lim_header.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:8px; background:transparent;"
        )
        root.addWidget(lim_header)

        self._lim_btn = QPushButton("ON")
        self._lim_btn.setCheckable(True)
        self._lim_btn.setChecked(True)
        self._lim_btn.setFixedHeight(22)
        self._lim_btn.setStyleSheet(f"""
            QPushButton {{
                background:{_C["deep"]};
                border:1px solid rgba(0,229,255,0.3);
                border-radius:3px; color:{_C["text_dim"]}; font-size:10px;
            }}
            QPushButton:checked {{
                background:rgba(0,229,255,0.15);
                border-color:{_C["cyan"]}; color:{_C["cyan"]};
            }}
            QPushButton:disabled {{
                background:{_C["deep"]}; border-color:rgba(0,229,255,0.08);
                color:rgba(61,90,128,0.4);
            }}
        """)
        self._lim_btn.toggled.connect(self._on_limiter_toggled)
        root.addWidget(self._lim_btn)

        ceil_row = QHBoxLayout()
        ceil_row.setSpacing(2)
        ceil_lbl = QLabel("Ceil:")
        ceil_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:8px; background:transparent;"
        )
        ceil_row.addWidget(ceil_lbl)

        self._ceil_spin = QDoubleSpinBox()
        self._ceil_spin.setRange(-20.0, 0.0)
        self._ceil_spin.setSingleStep(0.1)
        self._ceil_spin.setDecimals(1)
        self._ceil_spin.setValue(-0.1)
        self._ceil_spin.setSuffix(" dB")
        self._ceil_spin.setFixedWidth(68)
        self._ceil_spin.setStyleSheet(f"""
            QDoubleSpinBox {{
                background:{_C["deep"]};
                border:1px solid rgba(0,229,255,0.2);
                border-radius:3px; color:{_C["text"]}; font-size:9px;
                padding:2px 4px;
            }}
            QDoubleSpinBox:focus   {{ border-color:{_C["cyan"]}; }}
            QDoubleSpinBox:disabled {{
                color:rgba(61,90,128,0.4);
                border-color:rgba(0,229,255,0.06);
            }}
        """)
        self._ceil_spin.valueChanged.connect(self._on_ceiling_changed)
        ceil_row.addWidget(self._ceil_spin)
        root.addLayout(ceil_row)

        # Keep a list of controls that should be locked in audition modes.
        self._bypass_only_widgets = [self._lim_btn, self._ceil_spin, lim_header]

        root.addStretch(1)

        # ── 20 Hz peak meter polling timer ───────────────────────────────────
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(50)   # 50 ms = 20 Hz
        self._poll_timer.timeout.connect(self._poll_peaks)
        self._poll_timer.start()

    # ── Slots ─────────────────────────────────────────────────────────────────

    @Slot(int)
    def _on_mode_changed(self, mode_id: int) -> None:
        """
        Called by QButtonGroup.idClicked when a mode button is pressed.

        Writes the new audition mode to the C++ MasterBus via the thread-safe
        atomic<int> setter.  The audio thread picks up the change within one
        block (< 1 ms); no audio dropout occurs.
        """
        self._active_mode = mode_id
        self._bus.set_audition_mode(mode_id)

        # Update the mode indicator label.
        labels = {
            AUDITION_BYPASS:    "Mix (Bypass)",
            AUDITION_PREVIEW:   "Audition: -7 LUFS",
            AUDITION_STREAMING: "Audition: -14 LUFS",
        }
        color = _MODE_COLOR.get(mode_id, _C["text_dim"])
        self._mode_indicator.setText(labels.get(mode_id, ""))
        self._mode_indicator.setStyleSheet(
            f"color:{color}; font-size:8px; background:transparent;"
        )

        # Refresh button styles so only the active button glows.
        _modes_info = [
            (AUDITION_BYPASS,    _C["text_dim"]),
            (AUDITION_PREVIEW,   _C["orange"]),
            (AUDITION_STREAMING, _C["cyan"]),
        ]
        for mid, col in _modes_info:
            btn = self._mode_group.button(mid)
            if btn is not None:
                btn.setStyleSheet(self._mode_btn_style(col, active=(mid == mode_id)))

        # Disable user limiter controls when an audition mode is active —
        # those paths bypass the user limiter entirely.
        in_bypass = (mode_id == AUDITION_BYPASS)
        self._lim_btn.setEnabled(in_bypass)
        self._ceil_spin.setEnabled(in_bypass)

    @Slot(int)
    def _on_fader_changed(self, value: int) -> None:
        """Map fader 0–200 → gain 0.0–2.0, update the C++ MasterBus."""
        gain = value / 100.0
        self._bus.set_gain(gain)
        db_str = "-∞ dB" if gain <= 0.0 else f"{20.0 * math.log10(gain):+.1f} dB"
        self._gain_lbl.setText(db_str)

    @Slot(bool)
    def _on_limiter_toggled(self, checked: bool) -> None:
        """Enable or disable the user brickwall limiter (BYPASS mode only)."""
        self._bus.set_limiter_enabled(checked)
        self._lim_btn.setText("ON" if checked else "OFF")

    @Slot(float)
    def _on_ceiling_changed(self, value: float) -> None:
        """Forward the new ceiling to the C++ MasterBus (BYPASS mode only)."""
        self._bus.set_ceiling(value)

    @Slot()
    def _poll_peaks(self) -> None:
        """Read current peak values from the C++ MasterBus and repaint the meter."""
        self._meter.set_peaks(self._bus.peak_L(), self._bus.peak_R())

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _mode_btn_style(color: str, active: bool) -> str:
        """Return the stylesheet for a mode toggle button in its current state."""
        if active:
            return (
                f"QPushButton {{"
                f"  background: rgba(0,0,0,0.0);"
                f"  border: 1px solid {color};"
                f"  border-radius: 3px;"
                f"  color: {color};"
                f"  font-size: 8px;"
                f"  font-weight: 700;"
                f"}}"
                f"QPushButton:hover {{"
                f"  background: rgba(255,255,255,0.05);"
                f"}}"
            )
        return (
            f"QPushButton {{"
            f"  background: {_C['deep']};"
            f"  border: 1px solid rgba(61,90,128,0.4);"
            f"  border-radius: 3px;"
            f"  color: {_C['text_dim']};"
            f"  font-size: 8px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  border-color: {color};"
            f"  color: {color};"
            f"}}"
        )
