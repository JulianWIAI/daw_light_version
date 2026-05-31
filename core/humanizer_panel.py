"""
humanizer_panel.py  --  Per-Track Velocity Humanizer Control Panel
===================================================================
Compact Qt widget that exposes all VelocityHumanizer parameters as
sliders / spinboxes.  One shared instance lives in MainWindow; it is
reloaded with the current track's settings each time the user selects
a MIDI track.

Signals
-------
params_changed(channel: int, params: dict)
    Emitted whenever any parameter changes.  The dict always contains:
        enabled          : bool
        sigma            : float   (Variance knob, velocity units)
        downbeat_boost   : float   (0.0–0.5)
        offbeat_reduction: float   (0.0–0.3)
        time_sig_num     : int
        time_sig_denom   : int

Public methods
--------------
load_channel(channel, track_name, params_dict)
    Populate the panel from a saved params dict.  Pass {} or None to
    load the default preset.
current_channel() -> int
    The MIDI channel currently shown in the panel.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui  import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QSlider, QCheckBox, QSpinBox, QComboBox,
    QPushButton, QFrame,
)

# Palette constants — copied from the application colour scheme.
_C = {
    "abyss":    "#06080F",
    "deep":     "#0C1220",
    "cyan":     "#00E5FF",
    "pink":     "#FF4DFF",
    "gold":     "#FFD700",
    "text":     "#E0F0FF",
    "text_dim": "#5A7A9A",
    "orange":   "#FF8C00",
}


def _make_label(text: str, dim: bool = False) -> QLabel:
    """Helper: styled QLabel for the panel."""
    lbl = QLabel(text)
    colour = _C["text_dim"] if dim else _C["text"]
    lbl.setStyleSheet(
        f"color:{colour}; font-size:10px; background:transparent;"
    )
    return lbl


def _make_slider(lo: int, hi: int, val: int) -> QSlider:
    """Helper: horizontal QSlider with panel styling."""
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(val)
    s.setFixedHeight(18)
    s.setStyleSheet(f"""
        QSlider::groove:horizontal {{
            height: 4px;
            background: {_C['deep']};
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            width: 10px; height: 10px;
            margin: -3px 0;
            background: {_C['cyan']};
            border-radius: 5px;
        }}
        QSlider::sub-page:horizontal {{
            background: {_C['cyan']};
            border-radius: 2px;
        }}
    """)
    return s


class HumanizerPanel(QWidget):
    """
    Control panel for the per-track Gaussian velocity humanizer.

    Displays:
        • An enable/disable toggle.
        • Variance (σ) slider — the Gaussian spread in velocity units.
        • Downbeat Accent slider — boost on the bar's first beat.
        • Offbeat Cut slider    — reduction on weak offbeats.
        • Time-signature selector (numerator + denominator).
        • A Reset button that restores default values.
    """

    # Emitted on every parameter change.
    # Carries (channel: int, params: dict) so MainWindow can update its
    # VelocityHumanizer instance without searching for the active track.
    params_changed = Signal(int, dict)

    # Default parameter values.
    _DEFAULTS = {
        "enabled":           False,
        "sigma":             8.0,
        "downbeat_boost":    0.15,
        "offbeat_reduction": 0.08,
        "time_sig_num":      4,
        "time_sig_denom":    4,
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        # Currently displayed channel; -1 means "no track selected".
        self._channel: int  = -1
        # Suppress signal emission while loading values into controls.
        self._loading: bool = False

        self._build_ui()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Build the widget layout."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # ── Title bar ─────────────────────────────────────────────────────────
        title_row = QHBoxLayout()
        title_lbl = QLabel("VELOCITY HUMANIZER")
        title_lbl.setStyleSheet(
            f"color:{_C['cyan']}; font-size:11px; font-weight:bold;"
            f" background:transparent; letter-spacing:1px;"
        )
        # Track name shown in smaller dim text next to the title.
        self._track_lbl = _make_label("— no track —", dim=True)

        title_row.addWidget(title_lbl)
        title_row.addStretch()
        title_row.addWidget(self._track_lbl)
        outer.addLayout(title_row)

        # Separator
        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color:{_C['cyan']}; opacity:0.2;")
        outer.addWidget(sep)

        # ── Enable toggle ─────────────────────────────────────────────────────
        enable_row = QHBoxLayout()
        self._enable_cb = QCheckBox("Enable humanization")
        self._enable_cb.setStyleSheet(
            f"QCheckBox {{ color:{_C['text']}; font-size:10px;"
            f" background:transparent; }}"
            f"QCheckBox::indicator {{ width:14px; height:14px; }}"
            f"QCheckBox::indicator:checked {{ background:{_C['cyan']};"
            f" border-radius:2px; }}"
            f"QCheckBox::indicator:unchecked {{ background:{_C['deep']};"
            f" border:1px solid {_C['text_dim']}; border-radius:2px; }}"
        )
        self._enable_cb.stateChanged.connect(self._on_any_change)
        enable_row.addWidget(self._enable_cb)
        enable_row.addStretch()
        outer.addLayout(enable_row)

        # ── Variance (sigma) ─────────────────────────────────────────────────
        # Stored as integer 0–250 representing 0.0–25.0 velocity units.
        outer.addWidget(_make_label("Variance (σ)"))
        sigma_row = QHBoxLayout()
        self._sigma_sl  = _make_slider(0, 250, 80)   # 80 → 8.0
        self._sigma_val = _make_label("8.0", dim=True)
        self._sigma_sl.valueChanged.connect(self._on_sigma_changed)
        sigma_row.addWidget(self._sigma_sl)
        sigma_row.addWidget(self._sigma_val)
        outer.addLayout(sigma_row)
        outer.addWidget(
            _make_label("How much random spread around the target velocity.", dim=True)
        )

        # ── Downbeat Accent ───────────────────────────────────────────────────
        # Stored as integer 0–50 representing 0%–50%.
        outer.addWidget(_make_label("Downbeat Accent"))
        db_row = QHBoxLayout()
        self._db_sl  = _make_slider(0, 50, 15)        # 15 → 15%
        self._db_val = _make_label("15%", dim=True)
        self._db_sl.valueChanged.connect(self._on_db_changed)
        db_row.addWidget(self._db_sl)
        db_row.addWidget(self._db_val)
        outer.addLayout(db_row)
        outer.addWidget(
            _make_label("Velocity boost on beat 1 (and beat 3).", dim=True)
        )

        # ── Offbeat Cut ───────────────────────────────────────────────────────
        # Stored as integer 0–30 representing 0%–30%.
        outer.addWidget(_make_label("Offbeat Cut"))
        ob_row = QHBoxLayout()
        self._ob_sl  = _make_slider(0, 30, 8)         # 8 → 8%
        self._ob_val = _make_label("8%", dim=True)
        self._ob_sl.valueChanged.connect(self._on_ob_changed)
        ob_row.addWidget(self._ob_sl)
        ob_row.addWidget(self._ob_val)
        outer.addLayout(ob_row)
        outer.addWidget(
            _make_label("Velocity cut on eighth and 16th offbeats.", dim=True)
        )

        # ── Time signature ────────────────────────────────────────────────────
        outer.addWidget(_make_label("Time Signature"))
        ts_row = QHBoxLayout()
        self._ts_num = QSpinBox()
        self._ts_num.setRange(1, 16)
        self._ts_num.setValue(4)
        self._ts_num.setFixedWidth(48)
        self._ts_num.setStyleSheet(
            f"QSpinBox {{ background:{_C['deep']}; color:{_C['text']};"
            f" border:1px solid {_C['text_dim']}; border-radius:3px;"
            f" font-size:11px; }}"
        )
        ts_sep = _make_label(" / ")

        # Denominator: only standard note values allowed.
        self._ts_denom = QComboBox()
        for v in ("2", "4", "8", "16"):
            self._ts_denom.addItem(v)
        self._ts_denom.setCurrentText("4")
        self._ts_denom.setFixedWidth(52)
        self._ts_denom.setStyleSheet(
            f"QComboBox {{ background:{_C['deep']}; color:{_C['text']};"
            f" border:1px solid {_C['text_dim']}; border-radius:3px;"
            f" font-size:11px; }}"
        )

        self._ts_num  .valueChanged     .connect(self._on_any_change)
        self._ts_denom.currentIndexChanged.connect(self._on_any_change)

        ts_row.addWidget(self._ts_num)
        ts_row.addWidget(ts_sep)
        ts_row.addWidget(self._ts_denom)
        ts_row.addStretch()
        outer.addLayout(ts_row)

        # ── Reset button ──────────────────────────────────────────────────────
        reset_btn = QPushButton("Reset defaults")
        reset_btn.setFixedHeight(26)
        reset_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']}; color:{_C['text_dim']};"
            f" border:1px solid {_C['text_dim']}; border-radius:4px;"
            f" font-size:10px; }}"
            f"QPushButton:hover {{ color:{_C['cyan']};"
            f" border-color:{_C['cyan']}; }}"
        )
        reset_btn.clicked.connect(self._on_reset)
        outer.addWidget(reset_btn)

        outer.addStretch()

    # ── Public API ────────────────────────────────────────────────────────────

    def load_channel(
        self,
        channel:    int,
        track_name: str,
        params:     Optional[dict] = None,
    ) -> None:
        """
        Populate the panel with the settings for the given MIDI channel.

        channel    : MIDI channel number (used when emitting params_changed).
        track_name : Display name shown in the panel header.
        params     : Dict of saved parameters, or None to show defaults.
        """
        self._loading = True
        self._channel = channel
        self._track_lbl.setText(track_name)

        p = params or self._DEFAULTS

        # Load each control without triggering signals.
        self._enable_cb.setChecked(bool(p.get("enabled",           self._DEFAULTS["enabled"])))
        self._sigma_sl .setValue  (int  (p.get("sigma",            self._DEFAULTS["sigma"])  * 10))
        self._db_sl    .setValue  (int  (p.get("downbeat_boost",   self._DEFAULTS["downbeat_boost"])   * 100))
        self._ob_sl    .setValue  (int  (p.get("offbeat_reduction",self._DEFAULTS["offbeat_reduction"])* 100))
        self._ts_num   .setValue  (int  (p.get("time_sig_num",     self._DEFAULTS["time_sig_num"])))

        denom = str(p.get("time_sig_denom", self._DEFAULTS["time_sig_denom"]))
        idx   = self._ts_denom.findText(denom)
        if idx >= 0:
            self._ts_denom.setCurrentIndex(idx)

        # Update value labels.
        self._sigma_val.setText(f"{self._sigma_sl.value() / 10:.1f}")
        self._db_val   .setText(f"{self._db_sl.value()}%")
        self._ob_val   .setText(f"{self._ob_sl.value()}%")

        self._loading = False

    def current_channel(self) -> int:
        """Return the MIDI channel currently shown (-1 if none)."""
        return self._channel

    def current_params(self) -> dict:
        """Return the current parameter dict."""
        return {
            "enabled":           self._enable_cb.isChecked(),
            "sigma":             self._sigma_sl.value() / 10.0,
            "downbeat_boost":    self._db_sl.value() / 100.0,
            "offbeat_reduction": self._ob_sl.value() / 100.0,
            "time_sig_num":      self._ts_num.value(),
            "time_sig_denom":    int(self._ts_denom.currentText()),
        }

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _on_sigma_changed(self, raw: int) -> None:
        """Update the sigma value label and emit params_changed."""
        self._sigma_val.setText(f"{raw / 10.0:.1f}")
        self._on_any_change()

    def _on_db_changed(self, raw: int) -> None:
        """Update the downbeat boost label and emit params_changed."""
        self._db_val.setText(f"{raw}%")
        self._on_any_change()

    def _on_ob_changed(self, raw: int) -> None:
        """Update the offbeat cut label and emit params_changed."""
        self._ob_val.setText(f"{raw}%")
        self._on_any_change()

    @Slot()
    def _on_any_change(self) -> None:
        """Emit params_changed whenever any control fires."""
        if self._loading or self._channel < 0:
            return
        self.params_changed.emit(self._channel, self.current_params())

    @Slot()
    def _on_reset(self) -> None:
        """Restore all controls to factory defaults."""
        self.load_channel(self._channel, self._track_lbl.text(), self._DEFAULTS)
        # load_channel suppresses signals; emit manually after reset.
        if self._channel >= 0:
            self.params_changed.emit(self._channel, self.current_params())
