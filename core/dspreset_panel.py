"""
dspreset_panel.py -- Dynamic PySide6 UI panel for Decent Sampler presets.
=========================================================================
Reads a DsInstrumentInfo (from dspreset_parser) and builds native Qt widgets
that mirror the <ui> control layout defined in the .dspreset file.
All audio work is delegated to the C++ engine; this module is GUI only.

Classes
-------
  DsKnobWidget       -- QDial mapped to a float parameter range
  DsSliderWidget     -- QSlider mapped to a float parameter range
  DsButtonWidget     -- toggle QPushButton for boolean parameters
  DsPresetPanel      -- QDockWidget that renders and manages the full UI
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui  import QFont
from PySide6.QtWidgets import (
    QDial, QDockWidget, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QVBoxLayout, QWidget,
)

from .dspreset_parser import DsInstrumentInfo, DsUIElement, parse_dspreset

logger = logging.getLogger(__name__)

# ── Bioluminescent theme colours (matches the DAW palette) ───────────────────
_BG      = "#060A18"
_DEEP    = "#0A0E22"
_SURFACE = "#0E1430"
_LIME    = "#39FF14"
_CYAN    = "#00E5FF"
_TEXT    = "#C8E6FF"
_DIM     = "#3D5A80"


# ═══════════════════════════════════════════════════════════════════════════════
# Individual control widgets
# ═══════════════════════════════════════════════════════════════════════════════

class DsKnobWidget(QDial):
    """
    QDial mapped to a float parameter in [min_value, max_value].
    Emits value_changed(parameter_name, float_value) on any movement.
    """

    # Signal: (parameter_name: str, value: float)
    value_changed = Signal(str, float)

    # Internal integer resolution for smooth movement.
    _RES = 1000

    def __init__(self, elem: DsUIElement,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._elem  = elem
        self._range = elem.max_value - elem.min_value

        self.setMinimum(0)
        self.setMaximum(self._RES)
        self.setNotchesVisible(True)
        self.setWrapping(False)
        self.setFixedSize(54, 54)
        self.setToolTip(
            f"{elem.label}\n"
            f"Range: {elem.min_value:.3g} – {elem.max_value:.3g}\n"
            f"Param: {elem.parameter_name}"
        )
        # Initialise dial position from the preset's default value.
        self._set_float(elem.default_value)
        self.valueChanged.connect(self._on_tick_changed)

    # ── Value mapping ─────────────────────────────────────────────────────

    def _to_tick(self, v: float) -> int:
        """Map float value to integer dial tick."""
        if self._range == 0:
            return 0
        frac = (v - self._elem.min_value) / self._range
        return int(max(0.0, min(1.0, frac)) * self._RES)

    def _to_float(self, tick: int) -> float:
        """Map integer dial tick back to float value."""
        return self._elem.min_value + (tick / self._RES) * self._range

    def _set_float(self, v: float) -> None:
        """Move the dial without emitting a signal."""
        self.blockSignals(True)
        self.setValue(self._to_tick(v))
        self.blockSignals(False)

    def get_value(self) -> float:
        """Current float value in the parameter's native range."""
        return self._to_float(self.value())

    # ── Slot ──────────────────────────────────────────────────────────────

    @Slot(int)
    def _on_tick_changed(self, tick: int) -> None:
        self.value_changed.emit(self._elem.parameter_name,
                                self._to_float(tick))


class DsSliderWidget(QSlider):
    """
    Horizontal QSlider mapped to a float parameter in [min_value, max_value].
    Emits value_changed(parameter_name, float_value) on movement.
    """

    value_changed = Signal(str, float)
    _RES          = 1000

    def __init__(self, elem: DsUIElement,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._elem  = elem
        self._range = elem.max_value - elem.min_value

        self.setMinimum(0)
        self.setMaximum(self._RES)
        self.setFixedHeight(22)
        self.setToolTip(
            f"{elem.label}\n"
            f"Range: {elem.min_value:.3g} – {elem.max_value:.3g}"
        )
        self._set_float(elem.default_value)
        self.valueChanged.connect(self._on_tick_changed)

    def _to_tick(self, v: float) -> int:
        if self._range == 0:
            return 0
        frac = (v - self._elem.min_value) / self._range
        return int(max(0.0, min(1.0, frac)) * self._RES)

    def _to_float(self, tick: int) -> float:
        return self._elem.min_value + (tick / self._RES) * self._range

    def _set_float(self, v: float) -> None:
        self.blockSignals(True)
        self.setValue(self._to_tick(v))
        self.blockSignals(False)

    def get_value(self) -> float:
        return self._to_float(self.value())

    @Slot(int)
    def _on_tick_changed(self, tick: int) -> None:
        self.value_changed.emit(self._elem.parameter_name,
                                self._to_float(tick))


class DsButtonWidget(QPushButton):
    """
    Toggle QPushButton for boolean DS parameters.
    Emits value_changed(parameter_name, 1.0) when on, 0.0 when off.
    """

    value_changed = Signal(str, float)

    def __init__(self, elem: DsUIElement,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(elem.label, parent)
        self._elem = elem
        self.setCheckable(True)
        self.setChecked(elem.default_value > 0.5)
        self.setFixedHeight(24)
        self.setToolTip(elem.parameter_name)
        self.setStyleSheet(
            f"QPushButton {{ background:{_DEEP}; color:{_DIM};"
            f" border:1px solid rgba(57,255,20,0.25); border-radius:3px;"
            f" font-size:10px; padding:0 8px; }}"
            f"QPushButton:checked {{ background:rgba(57,255,20,0.18);"
            f" color:{_LIME}; border-color:rgba(57,255,20,0.6); }}"
            f"QPushButton:hover {{ background:{_SURFACE}; }}"
        )
        self.toggled.connect(self._on_toggled)

    @Slot(bool)
    def _on_toggled(self, checked: bool) -> None:
        self.value_changed.emit(self._elem.parameter_name,
                                1.0 if checked else 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Row builder
# ═══════════════════════════════════════════════════════════════════════════════

def _build_control_row(
    elem:      DsUIElement,
    on_change: Callable[[str, float], None],
) -> tuple[QLabel, QWidget]:
    """
    Create a (label, control) pair for one DsUIElement.
    The control emits value_changed → on_change(parameter_name, value).
    """
    lbl = QLabel(elem.label)
    lbl.setFixedWidth(88)
    lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    lbl.setStyleSheet(f"color:{_DIM}; font-size:9px; background:transparent;")

    if elem.element_type == "labeled-knob":
        ctrl: QWidget = DsKnobWidget(elem)
        ctrl.value_changed.connect(on_change)       # type: ignore[attr-defined]
    elif elem.element_type == "slider":
        ctrl = DsSliderWidget(elem)
        ctrl.value_changed.connect(on_change)       # type: ignore[attr-defined]
    else:   # button
        ctrl = DsButtonWidget(elem)
        ctrl.value_changed.connect(on_change)       # type: ignore[attr-defined]

    return lbl, ctrl


# ═══════════════════════════════════════════════════════════════════════════════
# Main dock widget
# ═══════════════════════════════════════════════════════════════════════════════

class DsPresetPanel(QDockWidget):
    """
    Dock widget for Decent Sampler instruments.

    On load_preset() it:
      1. Parses the .dspreset XML (via dspreset_parser).
      2. Passes zone data to the C++ engine for audio setup.
      3. Dynamically creates Qt widgets matching the preset's <ui> layout.

    Usage:
        panel = DsPresetPanel(ds_engine, parent=main_window)
        main_window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, panel)
        panel.load_preset("/path/to/instrument.dspreset")
    """

    # Emitted when a user moves a control: (parameter_name, new_value).
    parameter_changed = Signal(str, float)

    def __init__(self, ds_engine=None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__("DS INSTRUMENT", parent)
        self._engine  = ds_engine   # C++ DecentSamplerEngine instance (may be None)
        self._info:   Optional[DsInstrumentInfo] = None
        # Maps parameter_name → its Qt control widget for programmatic updates.
        self._controls: Dict[str, QWidget] = {}

        # ── Outer layout ──────────────────────────────────────────────────
        root_w = QWidget()
        root_w.setStyleSheet(f"background:{_BG};")
        root   = QVBoxLayout(root_w)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Header: load button + instrument name label.
        hdr = QHBoxLayout()
        self._load_btn = QPushButton("Load .dspreset…")
        self._load_btn.setFixedHeight(24)
        self._load_btn.setStyleSheet(
            f"QPushButton {{ background:{_DEEP}; color:{_LIME};"
            f" border:1px solid rgba(57,255,20,0.3); border-radius:3px;"
            f" font-size:10px; padding:0 8px; }}"
            f"QPushButton:hover {{ background:rgba(57,255,20,0.1); }}"
        )
        self._load_btn.clicked.connect(self._on_load_clicked)
        hdr.addWidget(self._load_btn)

        self._name_lbl = QLabel("(no preset)")
        self._name_lbl.setStyleSheet(
            f"color:{_DIM}; font-size:10px; background:transparent;")
        hdr.addWidget(self._name_lbl, stretch=1)
        root.addLayout(hdr)

        # Separator.
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:rgba(57,255,20,0.15);")
        root.addWidget(sep)

        # Scrollable area for dynamically created control rows.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(f"background:{_BG}; border:none;")
        self._ctrl_widget = QWidget()
        self._ctrl_widget.setStyleSheet(f"background:{_BG};")
        self._ctrl_layout = QVBoxLayout(self._ctrl_widget)
        self._ctrl_layout.setContentsMargins(4, 4, 4, 4)
        self._ctrl_layout.setSpacing(6)
        self._ctrl_layout.addStretch()
        self._scroll.setWidget(self._ctrl_widget)
        root.addWidget(self._scroll, stretch=1)

        self.setWidget(root_w)
        self.setMinimumHeight(220)

    # ── Load slot ─────────────────────────────────────────────────────────

    @Slot()
    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Decent Sampler Preset", "",
            "Decent Sampler (*.dspreset);;All Files (*)",
        )
        if path:
            self.load_preset(path)

    def load_preset(self, path: str) -> None:
        """
        Parse the .dspreset, send zone data to the C++ engine, then rebuild
        the control widgets from the parsed UI element list.
        """
        self._info = parse_dspreset(path)

        # Pass zone metadata to the C++ audio engine.
        if self._engine is not None:
            try:
                self._engine.load_preset(path)
            except Exception as exc:
                logger.warning("DsPresetPanel: engine.load_preset failed: %s", exc)

        # Update header label.
        zone_str = (f"{self._info.num_zones} zones, "
                    f"{self._info.num_groups} groups")
        self._name_lbl.setText(f"{self._info.name}  [{zone_str}]")
        self._name_lbl.setStyleSheet(
            f"color:{_TEXT}; font-size:10px; background:transparent;")

        self._rebuild_controls()

    def _rebuild_controls(self) -> None:
        """Remove all existing control widgets and recreate them from _info."""
        if self._info is None:
            return
        self._controls.clear()

        # Remove old widgets (reverse order to avoid layout thrash).
        while self._ctrl_layout.count() > 0:
            item = self._ctrl_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        for elem in self._info.ui_elements:
            row_w   = QWidget()
            row_w.setStyleSheet("background:transparent;")
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(8)

            lbl, ctrl = _build_control_row(elem, self._on_param_changed)
            row_lay.addWidget(lbl)
            row_lay.addWidget(ctrl, stretch=1)

            self._controls[elem.parameter_name] = ctrl
            self._ctrl_layout.addWidget(row_w)

        self._ctrl_layout.addStretch()

    # ── Parameter change routing ──────────────────────────────────────────

    def _on_param_changed(self, param: str, value: float) -> None:
        """
        Called by any control widget on user interaction.
        Forwards the change to the C++ engine and emits parameter_changed.
        """
        if self._engine is not None:
            try:
                self._engine.set_parameter(param, value)
            except Exception:
                pass
        self.parameter_changed.emit(param, value)

    # ── MIDI CC input ─────────────────────────────────────────────────────

    def apply_cc(self, cc_num: int, cc_value: int) -> None:
        """
        Apply an incoming MIDI CC to the matching preset parameter.
        cc_value is 0–127 and is remapped to [min_value, max_value].
        Call this from the MIDI input handler in the main window.
        """
        if self._info is None:
            return
        param = self._info.cc_map.get(cc_num)
        if param is None:
            return
        ctrl = self._controls.get(param)
        if ctrl is None:
            return
        # Find the DsUIElement to know the value range.
        elem = next(
            (e for e in self._info.ui_elements if e.parameter_name == param),
            None,
        )
        if elem is None:
            return
        mapped = elem.min_value + (cc_value / 127.0) * (elem.max_value - elem.min_value)
        # Update the widget silently, then trigger the change callback.
        if hasattr(ctrl, "_set_float"):
            ctrl._set_float(mapped)         # type: ignore[attr-defined]
        self._on_param_changed(param, mapped)

    # ── Accessors ─────────────────────────────────────────────────────────

    def get_info(self) -> Optional[DsInstrumentInfo]:
        """Return the currently loaded DsInstrumentInfo, or None."""
        return self._info

    def set_engine(self, engine: object) -> None:
        """Attach or replace the C++ engine after construction."""
        self._engine = engine
