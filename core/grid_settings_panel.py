"""
grid_settings_panel.py  --  Piano-roll grid settings panel widget.
==================================================================
Provides GridSettingsPanel, a compact Qt widget that lets the user select:

  • Note value    : 1/1, 1/2, 1/4, 1/8, 1/16, 1/32, 1/64, 1/128
  • Modifier      : (none) / T (triplet) / D (dotted) / Free (single tick)
  • Ruler mode    : B+B (bars+beats) / Time (mm:ss.ms) / SMPTE

The panel emits:
  grid_changed(str)   — new grid label, e.g. "1/16", "1/8T", "Free"
  ruler_changed(str)  — new ruler mode: "BarsBeats", "Time", "SMPTE"
  fps_changed(float)  — new SMPTE frame rate (only when in SMPTE mode)

All layout and style is self-contained — no external style sheets needed.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel,
    QComboBox, QFrame,
)
from PySide6.QtCore import Qt, Signal

# ── Colour palette (matches the project dark theme) ──────────────────────────
_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "surface":  "#0D1228",
    "cyan":     "#00E5FF",
    "purple":   "#9945FF",
    "pink":     "#FF4DFF",
    "gold":     "#FFD700",
    "text_dim": "#3D5A80",
    "text":     "#E0F0FF",
}


def _btn_style(active: bool, colour: str) -> str:
    """Return a QPushButton stylesheet for active/inactive state."""
    if active:
        return (
            f"QPushButton {{ background:rgba({_hex_to_rgb(colour)},0.25);"
            f" border:1px solid {colour}; border-radius:3px;"
            f" color:{colour}; font-size:9px; padding:1px 4px; }}"
        )
    return (
        f"QPushButton {{ background:{_C['deep']};"
        f" border:1px solid rgba({_hex_to_rgb(colour)},0.25); border-radius:3px;"
        f" color:{_C['text_dim']}; font-size:9px; padding:1px 4px; }}"
        f"QPushButton:hover {{ border-color:{colour}; color:{colour}; }}"
    )


def _hex_to_rgb(hex_color: str) -> str:
    """Convert #RRGGBB to 'R,G,B' string for rgba() use."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"{r},{g},{b}"


# ── Note value groups ─────────────────────────────────────────────────────────
# The base note-value buttons (denominator only — modifier is separate).
_BASE_VALUES = ["1", "2", "4", "8", "16", "32", "64", "128"]

# Modifiers: which grid types are available for each modifier button.
_MODIFIERS = ["", "T", "D"]   # "" = straight, "T" = triplet, "D" = dotted

# Base values that support triplet and dotted variants.
_TRIPLET_SUPPORTED = {"4", "8", "16", "32", "64"}
_DOTTED_SUPPORTED  = {"4", "8", "16", "32", "64"}

# Ruler mode labels.
_RULER_MODES   = ["B+B", "Time", "SMPTE"]
_RULER_MAP_OUT = {"B+B": "BarsBeats", "Time": "Time", "SMPTE": "SMPTE"}

# SMPTE FPS options.
_FPS_OPTIONS = ["24", "25", "29.97", "30"]


class GridSettingsPanel(QWidget):
    """
    Compact grid settings bar for the piano-roll toolbar.

    Signals
    -------
    grid_changed  : emitted whenever the active grid label changes.
    ruler_changed : emitted when the ruler display mode changes.
    fps_changed   : emitted when the SMPTE FPS selection changes.
    """

    grid_changed  = Signal(str)    # e.g. "1/16", "1/8T", "Free"
    ruler_changed = Signal(str)    # "BarsBeats", "Time", "SMPTE"
    fps_changed   = Signal(float)  # 24.0, 25.0, 29.97, 30.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Internal state.
        self._base   = "16"   # selected base denominator
        self._mod    = ""     # selected modifier: "" / "T" / "D" / "Free"
        self._ruler  = "B+B"
        self._fps    = 30.0

        self._build_ui()
        self._update_grid_label()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background:{_C['abyss']};")
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 2, 4, 2)
        root.setSpacing(6)

        # ── "GRID" header label ───────────────────────────────────────────────
        grid_lbl = QLabel("GRID")
        grid_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;")
        root.addWidget(grid_lbl)

        # ── Base note value buttons ────────────────────────────────────────────
        self._base_btns: dict[str, QPushButton] = {}
        base_row = QHBoxLayout()
        base_row.setSpacing(2)
        for val in _BASE_VALUES:
            btn = QPushButton(f"1/{val}" if val != "1" else "1")
            btn.setFixedHeight(22)
            btn.setFixedWidth(32)
            btn.setCheckable(True)
            btn.setChecked(val == self._base)
            btn.setStyleSheet(_btn_style(val == self._base, _C["cyan"]))
            btn.clicked.connect(lambda _chk, v=val: self._on_base(v))
            self._base_btns[val] = btn
            base_row.addWidget(btn)
        root.addLayout(base_row)

        # ── Vertical separator ────────────────────────────────────────────────
        root.addWidget(_vsep())

        # ── Modifier buttons: (plain) / T / D / Free ──────────────────────────
        self._mod_btns: dict[str, QPushButton] = {}
        mod_row = QHBoxLayout()
        mod_row.setSpacing(2)
        for mod, label in [("", "—"), ("T", "T"), ("D", "D"), ("Free", "Free")]:
            btn = QPushButton(label)
            btn.setFixedHeight(22)
            btn.setFixedWidth(36 if mod == "Free" else 22)
            btn.setCheckable(True)
            btn.setChecked(mod == self._mod)
            btn.setStyleSheet(_btn_style(mod == self._mod, _C["pink"]))
            btn.clicked.connect(lambda _chk, m=mod: self._on_mod(m))
            self._mod_btns[mod] = btn
            mod_row.addWidget(btn)
        root.addLayout(mod_row)

        # ── Vertical separator ────────────────────────────────────────────────
        root.addWidget(_vsep())

        # ── Ruler mode buttons ────────────────────────────────────────────────
        self._ruler_btns: dict[str, QPushButton] = {}
        ruler_row = QHBoxLayout()
        ruler_row.setSpacing(2)
        for rm in _RULER_MODES:
            btn = QPushButton(rm)
            btn.setFixedHeight(22)
            btn.setFixedWidth(40 if rm == "SMPTE" else 32)
            btn.setCheckable(True)
            btn.setChecked(rm == self._ruler)
            btn.setStyleSheet(_btn_style(rm == self._ruler, _C["gold"]))
            btn.clicked.connect(lambda _chk, r=rm: self._on_ruler(r))
            self._ruler_btns[rm] = btn
            ruler_row.addWidget(btn)
        root.addLayout(ruler_row)

        # ── FPS combo (only visible in SMPTE mode) ────────────────────────────
        self._fps_combo = QComboBox()
        self._fps_combo.addItems(_FPS_OPTIONS)
        self._fps_combo.setCurrentText("30")
        self._fps_combo.setFixedWidth(54)
        self._fps_combo.setFixedHeight(22)
        self._fps_combo.setStyleSheet(
            f"QComboBox {{ background:{_C['deep']}; color:{_C['gold']};"
            f" border:1px solid rgba(255,215,0,0.3); border-radius:3px;"
            f" padding:1px 4px; font-size:9px; }}"
            f"QComboBox::drop-down {{ border:none; }}"
            f"QComboBox QAbstractItemView {{ background:{_C['surface']};"
            f" color:{_C['text']}; selection-background-color:{_C['deep']}; }}"
        )
        self._fps_combo.currentTextChanged.connect(self._on_fps)
        self._fps_combo.setVisible(False)
        root.addWidget(self._fps_combo)

    # ── Slot handlers ─────────────────────────────────────────────────────────

    def _on_base(self, val: str) -> None:
        """User clicked a base note-value button."""
        if self._base == val:
            return
        self._base = val
        # If the selected modifier is not available for this base, reset to plain.
        if self._mod == "T" and val not in _TRIPLET_SUPPORTED:
            self._mod = ""
        if self._mod == "D" and val not in _DOTTED_SUPPORTED:
            self._mod = ""
        self._refresh_base_buttons()
        self._refresh_mod_buttons()
        self._update_grid_label()

    def _on_mod(self, mod: str) -> None:
        """User clicked a modifier button."""
        if self._mod == mod:
            return
        self._mod = mod
        # Free mode does not use a base note value, but we keep it stored.
        self._refresh_mod_buttons()
        self._update_grid_label()

    def _on_ruler(self, ruler: str) -> None:
        """User clicked a ruler mode button."""
        if self._ruler == ruler:
            return
        self._ruler = ruler
        self._refresh_ruler_buttons()
        self._fps_combo.setVisible(ruler == "SMPTE")
        self.ruler_changed.emit(_RULER_MAP_OUT[ruler])

    def _on_fps(self, text: str) -> None:
        """User changed the SMPTE FPS combo."""
        try:
            self._fps = float(text)
        except ValueError:
            self._fps = 30.0
        self.fps_changed.emit(self._fps)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_label(self) -> str:
        """Compute the current grid label from base + modifier state."""
        if self._mod == "Free":
            return "Free"
        base_str = f"1/{self._base}" if self._base != "1" else "1/1"
        return base_str + self._mod   # "" / "T" / "D"

    def _update_grid_label(self) -> None:
        label = self._build_label()
        self.grid_changed.emit(label)

    def _refresh_base_buttons(self) -> None:
        for val, btn in self._base_btns.items():
            active = (val == self._base)
            btn.setChecked(active)
            btn.setStyleSheet(_btn_style(active, _C["cyan"]))

    def _refresh_mod_buttons(self) -> None:
        for mod, btn in self._mod_btns.items():
            active = (mod == self._mod)
            # Disable triplet/dotted buttons that don't apply to the current base.
            if mod == "T":
                btn.setEnabled(self._base in _TRIPLET_SUPPORTED)
            elif mod == "D":
                btn.setEnabled(self._base in _DOTTED_SUPPORTED)
            btn.setChecked(active)
            btn.setStyleSheet(_btn_style(active, _C["pink"]))

    def _refresh_ruler_buttons(self) -> None:
        for rm, btn in self._ruler_btns.items():
            active = (rm == self._ruler)
            btn.setChecked(active)
            btn.setStyleSheet(_btn_style(active, _C["gold"]))

    # ── Public API ─────────────────────────────────────────────────────────────

    def current_grid_label(self) -> str:
        """Return the currently selected grid label."""
        return self._build_label()

    def current_ruler_mode(self) -> str:
        """Return the current ruler mode string as expected by GridSnapper."""
        return _RULER_MAP_OUT[self._ruler]

    def current_fps(self) -> float:
        return self._fps

    def set_grid_label(self, label: str) -> None:
        """Programmatically select a grid label (e.g., on project load)."""
        if label == "Free":
            self._mod = "Free"
        elif label.endswith("T"):
            base = label[2:-1]  # strip "1/" prefix and "T" suffix
            self._base = base
            self._mod  = "T"
        elif label.endswith("D"):
            base = label[2:-1]
            self._base = base
            self._mod  = "D"
        else:
            # Straight: "1/16" → base = "16", or "1/1" → base = "1"
            parts = label.split("/")
            self._base = parts[-1] if len(parts) > 1 else "4"
            self._mod  = ""
        self._refresh_base_buttons()
        self._refresh_mod_buttons()


def _vsep() -> QFrame:
    """Return a 1-px vertical separator line."""
    sep = QFrame()
    sep.setFrameShape(QFrame.VLine)
    sep.setFixedWidth(1)
    sep.setFixedHeight(20)
    sep.setStyleSheet(f"background:rgba(0,229,255,0.15); border:none;")
    return sep
