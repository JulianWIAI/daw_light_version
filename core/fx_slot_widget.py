"""
fx_slot_widget.py -- Single insert-slot row in the FX rack.
============================================================
Each slot row shows:

  Empty state:
    [ + Add Effect ]   ← full-width button; click → opens effect picker menu

  Loaded state:
    [⚡] [ Effect Name ]   [×]
     │         │            └── Remove button: clears slot back to empty
     │         └── Label: click to select slot (shows param panel below rack)
     └── Power toggle: lit = active, dim = bypassed

Signals emitted (consumed by FxRackWidget):
    bypass_toggled(bool)   -- user toggled the power button
    remove_requested()     -- user clicked ×
    select_requested()     -- user clicked the effect name label
    effect_chosen(str)     -- user chose an effect from the picker menu
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QLabel, QMenu, QSizePolicy,
)

from .fx_plugin_base import FxPluginBase
from .fx_plugin_registry import PLUGIN_CATEGORIES

# Crystal bioluminescence palette -- local copy avoids circular imports.
_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "surface":  "#0E1430",
    "cyan":     "#00E5FF",
    "purple":   "#9945FF",
    "text":     "#C8E6FF",
    "text_dim": "#3D5A80",
    "orange":   "#FF6B2B",
}

# Slot height fixed so the rack has a consistent rhythm.
_SLOT_HEIGHT = 32


class FxSlotWidget(QWidget):
    """
    One horizontal slot row in the FX rack.

    Instantiate with plugin=None for an empty slot, or with a FxPluginBase
    instance for a loaded slot.  Call set_plugin() / clear() at any time to
    transition between states.
    """

    # Emitted signals (index-free -- FxRackWidget uses lambdas to capture idx).
    bypass_toggled  = Signal(bool)   # True = active, False = bypassed
    remove_requested = Signal()
    select_requested = Signal()
    effect_chosen    = Signal(str)   # display name of the chosen effect

    def __init__(
        self,
        plugin: Optional[FxPluginBase] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._plugin: Optional[FxPluginBase] = None
        self._selected: bool = False  # visual highlight when param panel is open

        self.setFixedHeight(_SLOT_HEIGHT)
        self.setStyleSheet(f"background:{_C['deep']};")

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 4, 2)
        self._layout.setSpacing(4)

        # -- Power / bypass toggle (only visible in loaded state) ---------------
        self._power_btn = QPushButton("⚡")
        self._power_btn.setFixedSize(24, 24)
        self._power_btn.setCheckable(True)
        self._power_btn.setChecked(True)
        self._power_btn.setToolTip("Toggle bypass")
        self._power_btn.setStyleSheet(self._power_style(True))
        self._power_btn.toggled.connect(self._on_power_toggled)

        # -- Effect name label / "Add Effect" button ----------------------------
        # In empty state this is a full-width button.
        # In loaded state it is a clickable label (selects the slot).
        self._name_btn = QPushButton("+ Add Effect")
        self._name_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._name_btn.setFixedHeight(24)
        self._name_btn.setStyleSheet(self._empty_btn_style())
        self._name_btn.clicked.connect(self._on_name_clicked)

        # -- Remove button (only visible in loaded state) -----------------------
        self._remove_btn = QPushButton("×")
        self._remove_btn.setFixedSize(24, 24)
        self._remove_btn.setToolTip("Remove effect")
        self._remove_btn.setStyleSheet(
            f"QPushButton {{ background:transparent; border:none;"
            f" color:{_C['text_dim']}; font-size:14px; }}"
            f"QPushButton:hover {{ color:{_C['orange']}; }}"
        )
        self._remove_btn.clicked.connect(self.remove_requested.emit)

        self._layout.addWidget(self._power_btn)
        self._layout.addWidget(self._name_btn)
        self._layout.addWidget(self._remove_btn)

        # Populate initial state.
        if plugin is not None:
            self.set_plugin(plugin)
        else:
            self._show_empty_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_plugin(self, plugin: FxPluginBase) -> None:
        """Transition to loaded state showing the given plugin."""
        self._plugin = plugin
        self._show_loaded_state(plugin)

    def clear(self) -> None:
        """Transition back to empty state (plugin reference released)."""
        self._plugin = None
        self._show_empty_state()

    def set_selected(self, selected: bool) -> None:
        """Highlight the slot when its parameter panel is open."""
        self._selected = selected
        if self._plugin is not None:
            self._name_btn.setStyleSheet(self._loaded_btn_style(selected))
        self.update()

    @property
    def plugin(self) -> Optional[FxPluginBase]:
        return self._plugin

    # ------------------------------------------------------------------
    # Internal state transitions
    # ------------------------------------------------------------------

    def _show_empty_state(self) -> None:
        """Show full-width 'Add Effect' placeholder."""
        self._power_btn.hide()
        self._remove_btn.hide()
        self._name_btn.setText("+ Add Effect")
        self._name_btn.setStyleSheet(self._empty_btn_style())

    def _show_loaded_state(self, plugin: FxPluginBase) -> None:
        """Show bypass toggle + effect name label + remove button."""
        self._power_btn.show()
        self._remove_btn.show()
        self._power_btn.blockSignals(True)
        self._power_btn.setChecked(plugin.enabled)
        self._power_btn.blockSignals(False)
        self._power_btn.setStyleSheet(self._power_style(plugin.enabled))
        self._name_btn.setText(plugin.DISPLAY_NAME)
        self._name_btn.setStyleSheet(self._loaded_btn_style(self._selected))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_power_toggled(self, active: bool) -> None:
        """Sync plugin.enabled and restyle the button."""
        if self._plugin is not None:
            self._plugin.enabled = active
        self._power_btn.setStyleSheet(self._power_style(active))
        self.bypass_toggled.emit(active)

    def _on_name_clicked(self) -> None:
        """
        Empty slot → open effect picker menu.
        Loaded slot → emit select_requested so the rack shows the param panel.
        """
        if self._plugin is None:
            self._open_picker_menu()
        else:
            self.select_requested.emit()

    def _open_picker_menu(self) -> None:
        """Show a categorised context menu listing all available effects."""
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background:{_C['surface']}; color:{_C['text']};"
            f" border:1px solid rgba(0,229,255,0.2); font-size:10px; }}"
            f"QMenu::item:selected {{ background:rgba(0,229,255,0.15); }}"
            f"QMenu::separator {{ height:1px; background:rgba(0,229,255,0.1);"
            f" margin:2px 8px; }}"
        )

        # Build one submenu per category.
        for category, effect_names in PLUGIN_CATEGORIES.items():
            submenu = menu.addMenu(category)
            submenu.setStyleSheet(menu.styleSheet())
            for name in effect_names:
                action = submenu.addAction(name)
                # Capture name in default-arg closure so the lambda works.
                action.triggered.connect(
                    lambda checked=False, n=name: self.effect_chosen.emit(n)
                )

        # Show the menu directly below the slot widget.
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    # ------------------------------------------------------------------
    # Style helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _power_style(active: bool) -> str:
        if active:
            return (
                "QPushButton { background:rgba(0,229,255,0.15);"
                " border:1px solid rgba(0,229,255,0.5); border-radius:4px;"
                " color:#00E5FF; font-size:12px; }"
                "QPushButton:hover { background:rgba(0,229,255,0.25); }"
            )
        return (
            "QPushButton { background:transparent;"
            " border:1px solid rgba(61,90,128,0.4); border-radius:4px;"
            " color:#3D5A80; font-size:12px; }"
            "QPushButton:hover { border-color:rgba(0,229,255,0.3); }"
        )

    @staticmethod
    def _empty_btn_style() -> str:
        return (
            "QPushButton { background:transparent;"
            " border:1px dashed rgba(0,229,255,0.2); border-radius:4px;"
            " color:#3D5A80; font-size:10px; text-align:left; padding-left:8px; }"
            "QPushButton:hover { border-color:rgba(0,229,255,0.5);"
            " color:#00E5FF; }"
        )

    @staticmethod
    def _loaded_btn_style(selected: bool) -> str:
        if selected:
            return (
                "QPushButton { background:rgba(153,69,255,0.2);"
                " border:1px solid rgba(153,69,255,0.7); border-radius:4px;"
                " color:#C8E6FF; font-size:10px; text-align:left;"
                " padding-left:8px; }"
            )
        return (
            "QPushButton { background:rgba(0,229,255,0.06);"
            " border:1px solid rgba(0,229,255,0.2); border-radius:4px;"
            " color:#C8E6FF; font-size:10px; text-align:left;"
            " padding-left:8px; }"
            "QPushButton:hover { border-color:rgba(153,69,255,0.6);"
            " background:rgba(153,69,255,0.1); }"
        )
