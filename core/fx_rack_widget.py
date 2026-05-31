"""
fx_rack_widget.py -- Insert-Slot FX Rack panel for audio tracks.
================================================================
Replaces the old fixed AudioFxPanel with a dynamic insert-slot system.

Layout (top to bottom):
  ┌─ Track label ──────────────────────────────┐
  │ ┌─ ROUTING ─────────────────────────────┐  │
  │ │  Volume [slider]  Pan [slider]        │  │
  │ │  [MUTE]  [SOLO]                       │  │
  │ └───────────────────────────────────────┘  │
  │ ┌─ FX CHAIN ────────────────────────────┐  │
  │ │  [⚡] [ Empty  ]              [×]     │  │  ← FxSlotWidget × N
  │ │  ...  (up to 8 slots)                 │  │
  │ └───────────────────────────────────────┘  │
  │ ┌─ PARAMETERS ──────────────────────────┐  │
  │ │  (selected slot's param widget here)  │  │
  │ └───────────────────────────────────────┘  │
  └────────────────────────────────────────────┘

Public interface (matches old AudioFxPanel so gui_windows.py needs no changes):
    load_chain(chain, track_name)    -- populate from an AudioFxChain
    chain_changed  = Signal(int)     -- emitted with track_id on any change

Memory management:
  When a slot is cleared, the plugin instance is removed from chain.plugins
  and all Python references are dropped.  The GC frees the object (and its
  C++ processor, if any) at the next collection.
"""

from __future__ import annotations

import logging
from typing import Optional, List

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QPushButton,
    QGroupBox, QScrollArea, QFrame,
)

from .audio_fx_chain import AudioFxChain
from .fx_plugin_base import FxPluginBase
from .fx_plugin_registry import PLUGIN_REGISTRY
from .fx_slot_widget import FxSlotWidget

logger = logging.getLogger(__name__)

# Crystal bioluminescence palette -- local copy.
_C = {
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "surface":  "#0E1430",
    "cyan":     "#00E5FF",
    "purple":   "#9945FF",
    "pink":     "#FF2D9E",
    "gold":     "#FFD700",
    "orange":   "#FF6B2B",
    "text":     "#C8E6FF",
    "text_dim": "#3D5A80",
}

_MIN_SLOTS = 4   # Rack always shows at least this many slots.
_MAX_SLOTS = 8   # Hard upper limit on insert slots.


def _group_box(title: str) -> QGroupBox:
    g = QGroupBox(title)
    g.setStyleSheet(
        f"QGroupBox {{ border:1px solid rgba(0,229,255,0.15); border-radius:6px;"
        f" margin-top:10px; padding-top:6px; color:{_C['text_dim']};"
        f" font-size:9px; letter-spacing:1px; background:{_C['abyss']}; }}"
        f"QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 5px;"
        f" color:rgba(0,229,255,0.6); }}"
    )
    return g


def _hslider(lo: int, hi: int, init: int) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(init)
    s.setStyleSheet(
        "QSlider::groove:horizontal { height:4px;"
        " background:rgba(0,229,255,0.12); border-radius:2px; }"
        "QSlider::handle:horizontal { width:12px; height:12px; margin:-4px 0;"
        " background:#00E5FF; border-radius:6px; }"
        "QSlider::sub-page:horizontal { background:rgba(0,229,255,0.35);"
        " border-radius:2px; }"
    )
    return s


def _param_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFixedWidth(52)
    lbl.setStyleSheet(
        f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
    )
    return lbl


# =============================================================================
# FxRackWidget
# =============================================================================

class FxRackWidget(QWidget):
    """
    Dynamic insert-slot FX rack panel for one audio track.

    Exposes the same public interface as the old AudioFxPanel so that
    gui_windows.py needs no changes to the dock wiring.
    """

    # Emitted with track_id whenever any parameter, routing value, or
    # plugin list changes -- consumed by MainWindow._on_audio_fx_changed().
    chain_changed = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._chain:    Optional[AudioFxChain] = None
        self._building: bool = True      # suppresses signals during load
        self._slots:    List[FxSlotWidget] = []
        self._selected_slot_idx: int = -1  # index of slot whose params are shown

        self.setMinimumWidth(260)
        self.setStyleSheet(f"background:{_C['abyss']};")

        # Outer layout with scroll so it works on small screens.
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea {{ background:{_C['abyss']}; border:none; }}"
        )
        root_lay.addWidget(scroll)

        inner = QWidget()
        inner.setStyleSheet(f"background:{_C['abyss']};")
        scroll.setWidget(inner)

        self._inner_lay = QVBoxLayout(inner)
        self._inner_lay.setContentsMargins(8, 8, 8, 8)
        self._inner_lay.setSpacing(6)

        # -- Track label -------------------------------------------------------
        self._track_label = QLabel("No audio track selected")
        self._track_label.setAlignment(Qt.AlignCenter)
        self._track_label.setStyleSheet(
            f"color:{_C['cyan']}; font-size:11px; font-weight:bold;"
            f" background:transparent; letter-spacing:0.5px;"
        )
        self._inner_lay.addWidget(self._track_label)

        # -- Routing (volume, pan, mute, solo) ---------------------------------
        self._build_routing_section()

        # -- FX Chain (slot list) ----------------------------------------------
        self._chain_grp = _group_box("FX CHAIN")
        self._slots_lay = QVBoxLayout(self._chain_grp)
        self._slots_lay.setContentsMargins(4, 4, 4, 4)
        self._slots_lay.setSpacing(3)
        self._inner_lay.addWidget(self._chain_grp)

        # -- Parameter panel (swapped when a slot is selected) -----------------
        self._param_grp = _group_box("PARAMETERS")
        self._param_lay = QVBoxLayout(self._param_grp)
        self._param_lay.setContentsMargins(4, 4, 4, 4)
        self._param_lay.setSpacing(0)

        # Placeholder shown when nothing is selected.
        self._param_placeholder = QLabel("Click an effect slot\nto edit its parameters.")
        self._param_placeholder.setAlignment(Qt.AlignCenter)
        self._param_placeholder.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        self._param_lay.addWidget(self._param_placeholder)
        self._current_param_widget: Optional[QWidget] = None

        self._inner_lay.addWidget(self._param_grp)
        self._inner_lay.addStretch()

        self._building = False

    # -------------------------------------------------------------------------
    # Public API (mirrors old AudioFxPanel)
    # -------------------------------------------------------------------------

    def load_chain(self, chain: AudioFxChain, track_name: str = "") -> None:
        """
        Populate the rack from an AudioFxChain.

        Rebuilds all slot widgets and restores routing controls from the chain.
        Safe to call when switching between tracks.
        """
        self._building = True
        self._chain = chain
        self._selected_slot_idx = -1

        # Update track label.
        self._track_label.setText(track_name or f"Audio Track {chain.track_id}")

        # Restore routing controls from chain.
        self._vol_slider.setValue(int(chain.volume * 100))
        self._pan_slider.setValue(int(chain.pan * 50))
        self._mute_btn.setChecked(chain.muted)
        self._solo_btn.setChecked(chain.soloed)

        self._building = False

        # Rebuild all slot widgets from the chain's plugin list.
        self._rebuild_slots()

        # Clear the parameter panel.
        self._clear_param_panel()

    @property
    def chain(self) -> Optional[AudioFxChain]:
        """The currently displayed AudioFxChain (or None)."""
        return self._chain

    # -------------------------------------------------------------------------
    # Routing section
    # -------------------------------------------------------------------------

    def _build_routing_section(self) -> None:
        """Create volume, pan, mute, and solo controls."""
        routing_grp = _group_box("ROUTING")
        routing_lay = QVBoxLayout(routing_grp)
        routing_lay.setContentsMargins(4, 4, 4, 4)
        routing_lay.setSpacing(4)

        # Volume row.
        vol_row = QHBoxLayout()
        vol_row.addWidget(_param_label("Volume"))
        self._vol_slider = _hslider(0, 200, 100)
        self._vol_val    = QLabel("1.00")
        self._vol_val.setFixedWidth(32)
        self._vol_val.setStyleSheet(
            f"color:{_C['cyan']}; font-size:9px; background:transparent;"
        )
        vol_row.addWidget(self._vol_slider)
        vol_row.addWidget(self._vol_val)
        routing_lay.addLayout(vol_row)

        # Pan row.
        pan_row = QHBoxLayout()
        pan_row.addWidget(_param_label("Pan"))
        self._pan_slider = _hslider(-50, 50, 0)
        self._pan_val    = QLabel("0")
        self._pan_val.setFixedWidth(32)
        self._pan_val.setStyleSheet(
            f"color:{_C['cyan']}; font-size:9px; background:transparent;"
        )
        pan_row.addWidget(self._pan_slider)
        pan_row.addWidget(self._pan_val)
        routing_lay.addLayout(pan_row)

        # Mute / Solo.
        ms_row = QHBoxLayout()
        self._mute_btn = QPushButton("MUTE")
        self._mute_btn.setCheckable(True)
        self._mute_btn.setFixedHeight(24)
        self._mute_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid rgba(255,107,43,0.3); border-radius:4px;"
            f" color:{_C['text_dim']}; font-size:9px; }}"
            f"QPushButton:checked {{ background:rgba(255,107,43,0.3);"
            f" border-color:{_C['orange']}; color:{_C['orange']}; }}"
        )

        self._solo_btn = QPushButton("SOLO")
        self._solo_btn.setCheckable(True)
        self._solo_btn.setFixedHeight(24)
        self._solo_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid rgba(255,215,0,0.3); border-radius:4px;"
            f" color:{_C['text_dim']}; font-size:9px; }}"
            f"QPushButton:checked {{ background:rgba(255,215,0,0.25);"
            f" border-color:{_C['gold']}; color:{_C['gold']}; }}"
        )

        ms_row.addWidget(self._mute_btn)
        ms_row.addWidget(self._solo_btn)
        routing_lay.addLayout(ms_row)

        # Connect routing controls to push() helper.
        self._vol_slider.valueChanged.connect(self._push_routing)
        self._pan_slider.valueChanged.connect(self._push_routing)
        self._mute_btn.toggled.connect(self._push_routing)
        self._solo_btn.toggled.connect(self._push_routing)

        self._inner_lay.addWidget(routing_grp)

    def _push_routing(self) -> None:
        """Write routing control values to chain and emit chain_changed."""
        if self._building or self._chain is None:
            return
        vol = self._vol_slider.value() / 100.0
        pan = self._pan_slider.value() / 50.0
        self._vol_val.setText(f"{vol:.2f}")
        self._pan_val.setText(str(self._pan_slider.value()))
        self._chain.volume = vol
        self._chain.pan    = pan
        self._chain.muted  = self._mute_btn.isChecked()
        self._chain.soloed = self._solo_btn.isChecked()
        self.chain_changed.emit(self._chain.track_id)

    # -------------------------------------------------------------------------
    # Slot management
    # -------------------------------------------------------------------------

    def _rebuild_slots(self) -> None:
        """
        Destroy all existing slot widgets and recreate them from chain.plugins.

        Always shows at least _MIN_SLOTS rows.  One trailing empty slot is
        added so the user can always load a new effect (unless at _MAX_SLOTS).
        """
        # Destroy existing slots.
        for slot in self._slots:
            slot.setParent(None)
            slot.deleteLater()
        self._slots.clear()

        if self._chain is None:
            return

        n_plugins = len(self._chain.plugins)
        # Show all loaded plugins + one trailing empty slot (unless max reached).
        n_slots = max(_MIN_SLOTS, n_plugins + (1 if n_plugins < _MAX_SLOTS else 0))
        n_slots = min(n_slots, _MAX_SLOTS)

        for i in range(n_slots):
            plugin = self._chain.plugins[i] if i < n_plugins else None
            slot = FxSlotWidget(plugin=plugin, parent=self._chain_grp)
            self._slots.append(slot)
            self._slots_lay.addWidget(slot)

            # Connect signals with slot index captured via default-arg closures.
            slot.bypass_toggled  .connect(lambda b, idx=i: self._on_bypass(idx, b))
            slot.remove_requested.connect(lambda idx=i:    self._on_remove(idx))
            slot.select_requested.connect(lambda idx=i:    self._on_select(idx))
            slot.effect_chosen   .connect(lambda n, idx=i: self._on_effect_chosen(idx, n))

        # Restore selection highlight.
        if 0 <= self._selected_slot_idx < len(self._slots):
            self._slots[self._selected_slot_idx].set_selected(True)

    # -------------------------------------------------------------------------
    # Slot signal handlers
    # -------------------------------------------------------------------------

    def _on_effect_chosen(self, slot_idx: int, effect_name: str) -> None:
        """Instantiate the chosen plugin and insert it at slot_idx."""
        if self._chain is None:
            return

        cls = PLUGIN_REGISTRY.get(effect_name)
        if cls is None:
            logger.warning("Unknown effect: %s", effect_name)
            return

        plugin: FxPluginBase = cls()
        # Register parameter-change callback so slider edits propagate.
        plugin._on_changed = self._on_plugin_param_changed
        # AI plugins that analyse or modify the chain receive a chain reference.
        if hasattr(plugin, "set_chain"):
            plugin.set_chain(self._chain)

        n_plugins = len(self._chain.plugins)
        if slot_idx < n_plugins:
            # Replace existing plugin (e.g. user changed an already-loaded slot).
            self._chain.plugins[slot_idx] = plugin
        else:
            # The slot is in the "trailing empty" zone -- append to chain.
            self._chain.plugins.append(plugin)

        # Rebuild slot widgets so indices stay consistent.
        self._selected_slot_idx = slot_idx
        self._rebuild_slots()
        # Immediately show the new plugin's parameter panel.
        self._show_param_panel(plugin)

        self.chain_changed.emit(self._chain.track_id)

    def _on_remove(self, slot_idx: int) -> None:
        """Remove the plugin at slot_idx from the chain and release its memory."""
        if self._chain is None:
            return
        if slot_idx >= len(self._chain.plugins):
            return  # was already an empty slot -- nothing to do

        # Remove from chain: Python drops the last reference → GC frees the object.
        self._chain.remove_plugin(slot_idx)

        # Clear param panel if we just removed the selected slot.
        if slot_idx == self._selected_slot_idx:
            self._selected_slot_idx = -1
            self._clear_param_panel()
        elif self._selected_slot_idx > slot_idx:
            # Selection index shifts down by one because the list compacted.
            self._selected_slot_idx -= 1

        self._rebuild_slots()
        self.chain_changed.emit(self._chain.track_id)

    def _on_bypass(self, slot_idx: int, active: bool) -> None:
        """Plugin.enabled has already been updated by the slot widget; just re-render."""
        if self._chain is not None:
            self.chain_changed.emit(self._chain.track_id)

    def _on_select(self, slot_idx: int) -> None:
        """Show the parameter panel for the selected slot."""
        if self._chain is None:
            return
        if slot_idx >= len(self._chain.plugins):
            return  # empty slot has no params to show

        # Deselect old slot.
        if 0 <= self._selected_slot_idx < len(self._slots):
            self._slots[self._selected_slot_idx].set_selected(False)

        self._selected_slot_idx = slot_idx
        if 0 <= slot_idx < len(self._slots):
            self._slots[slot_idx].set_selected(True)

        plugin = self._chain.plugins[slot_idx]
        self._show_param_panel(plugin)

    # -------------------------------------------------------------------------
    # Parameter panel management
    # -------------------------------------------------------------------------

    def _show_param_panel(self, plugin: FxPluginBase) -> None:
        """Replace the current parameter panel widget with this plugin's."""
        self._clear_param_panel()

        widget = plugin.create_parameter_widget()
        widget.setParent(self._param_grp)
        self._param_lay.addWidget(widget)
        self._current_param_widget = widget

        # Update group title to show which plugin is being edited.
        self._param_grp.setTitle(f"PARAMETERS — {plugin.DISPLAY_NAME.upper()}")

    def _clear_param_panel(self) -> None:
        """Remove the current parameter widget and show the placeholder."""
        if self._current_param_widget is not None:
            self._current_param_widget.setParent(None)
            self._current_param_widget.deleteLater()
            self._current_param_widget = None

        # Show placeholder text again.
        if self._param_placeholder.parent() is None:
            self._param_lay.addWidget(self._param_placeholder)
            self._param_placeholder.show()

        self._param_grp.setTitle("PARAMETERS")

    # -------------------------------------------------------------------------
    # Plugin parameter callback
    # -------------------------------------------------------------------------

    def _on_plugin_param_changed(self) -> None:
        """Called by any plugin's _notify() when a slider moves."""
        if self._chain is not None:
            self.chain_changed.emit(self._chain.track_id)
