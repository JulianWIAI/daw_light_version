"""
gm_defaults_dialog.py — GM Instrument Defaults Settings Dialog
==============================================================

Lets the user assign a custom SFZ, SF2, or VST3 instrument to each General MIDI
program group.  Changes take effect on the next MIDI file open/import.

Open from the toolbar::

    dialog = GmDefaultsDialog(parent=main_window)
    dialog.exec()

The ``defaults_changed`` signal fires when the user clicks Save, so callers
can react immediately (e.g. update a status label) without polling.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui  import QFont
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .gm_defaults_manager import GM_CATEGORIES, GmDefaultsManager
from .audio_engine import GM_INSTRUMENTS

# ─────────────────────────────────────────────────────────────────────────────
# Colour palette  (matches the main application theme)
# ─────────────────────────────────────────────────────────────────────────────
_C = {
    "abyss":    "#0A0A0F",
    "deep":     "#111118",
    "surface":  "#1A1A24",
    "border":   "#2A2A3A",
    "text":     "#E0E0E0",
    "text_dim": "#888899",
    "cyan":     "#00E5FF",
    "gold":     "#FFD700",
    "pink":     "#FF6B9D",
    "purple":   "#9945FF",
    "green":    "#39FF14",
    "orange":   "#FF6B2B",
    "red":      "#FF4444",
}

_DIALOG_STYLESHEET = f"""
QDialog {{
    background: {_C['abyss']};
    color: {_C['text']};
}}
QLabel {{
    color: {_C['text']};
    background: transparent;
}}
QScrollArea {{
    background: {_C['abyss']};
    border: none;
}}
QScrollBar:vertical {{
    background: {_C['deep']};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {_C['border']};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QPushButton {{
    background: {_C['deep']};
    color: {_C['cyan']};
    border: 1px solid rgba(0,229,255,0.25);
    border-radius: 5px;
    padding: 0 10px;
    height: 28px;
}}
QPushButton:hover {{
    border-color: {_C['cyan']};
    background: rgba(0,229,255,0.10);
}}
QPushButton:pressed {{
    background: rgba(0,229,255,0.20);
}}
QPushButton#browse_sfz  {{ color: {_C['cyan']}; }}
QPushButton#browse_sf2  {{ color: {_C['gold']};   border-color: rgba(255,215,0,0.35); }}
QPushButton#browse_sf2:hover  {{ border-color: {_C['gold']};   background: rgba(255,215,0,0.12); }}
QPushButton#browse_vst3 {{ color: {_C['purple']}; border-color: rgba(153,69,255,0.35); }}
QPushButton#browse_vst3:hover {{ border-color: {_C['purple']}; background: rgba(153,69,255,0.12); }}
QPushButton#clear_btn   {{ color: {_C['text_dim']}; border-color: rgba(255,255,255,0.10); min-width:28px; max-width:28px; }}
QPushButton#clear_btn:hover   {{ color: {_C['red']};      border-color: {_C['red']}; }}
QPushButton#save_btn    {{ color: {_C['green']};  border-color: rgba(57,255,20,0.40); height:34px; }}
QPushButton#save_btn:hover    {{ background: rgba(57,255,20,0.15); border-color: {_C['green']}; }}
QPushButton#reset_btn   {{ color: {_C['orange']}; border-color: rgba(255,107,43,0.35); }}
QPushButton#reset_btn:hover   {{ background: rgba(255,107,43,0.12); border-color: {_C['orange']}; }}
QPushButton#cancel_btn  {{ color: {_C['text_dim']}; border-color: rgba(255,255,255,0.15); }}
QPushButton#cancel_btn:hover  {{ color: {_C['text']}; border-color: rgba(255,255,255,0.40); }}
QPushButton#ok_btn      {{ color: {_C['green']};  border-color: rgba(57,255,20,0.40); }}
QPushButton#ok_btn:hover      {{ background: rgba(57,255,20,0.15); border-color: {_C['green']}; }}
QFrame#row_frame {{
    background: {_C['surface']};
    border: 1px solid {_C['border']};
    border-radius: 7px;
}}
QFrame#separator {{
    background: {_C['border']};
    max-height: 1px;
}}
QListWidget {{
    background: {_C['deep']};
    color: {_C['text']};
    border: 1px solid {_C['border']};
    border-radius: 4px;
}}
QListWidget::item:selected {{
    background: rgba(0,229,255,0.18);
    color: {_C['cyan']};
}}
QListWidget::item:hover {{
    background: rgba(0,229,255,0.08);
}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# SF2 preset picker (shown after the user selects an .sf2 file)
# ─────────────────────────────────────────────────────────────────────────────

class _Sf2PresetPickerDialog(QDialog):
    """
    Minimal dialog that lets the user pick a bank/preset from a GM category
    to use as the SF2 instrument for that category.
    """

    def __init__(self, sf2_path: str, category_key: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select SF2 Preset")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet(_DIALOG_STYLESHEET)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._picked: Optional[tuple] = None  # (bank, preset, name)

        display_name = GM_CATEGORIES[category_key][0]
        presets = GM_INSTRUMENTS.get(display_name, [])
        if not presets:
            presets = [(0, 0, "Default")]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        sf2_lbl = QLabel(f"SF2: {os.path.basename(sf2_path)}")
        sf2_lbl.setStyleSheet(f"color:{_C['gold']}; font-weight:bold;")
        layout.addWidget(sf2_lbl)

        cat_lbl = QLabel(f"Category: {display_name}")
        cat_lbl.setStyleSheet(f"color:{_C['text_dim']}; font-size:10px;")
        layout.addWidget(cat_lbl)

        self._list = QListWidget()
        for preset, bank, name in presets:
            item = QListWidgetItem(f"Bank {bank:3d}  ·  Preset {preset:3d}  —  {name}")
            item.setData(Qt.ItemDataRole.UserRole, (bank, preset, name))
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self._accept)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        ok_btn = QPushButton("Select")
        ok_btn.setObjectName("ok_btn")
        ok_btn.setFixedWidth(90)
        ok_btn.clicked.connect(self._accept)
        btn_row.addWidget(ok_btn)

        layout.addLayout(btn_row)

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item:
            self._picked = item.data(Qt.ItemDataRole.UserRole)
            self.accept()

    def picked(self) -> Optional[tuple]:
        """Returns ``(bank, preset, name)`` after accept(), else ``None``."""
        return self._picked


# ─────────────────────────────────────────────────────────────────────────────
# Individual category row widget
# ─────────────────────────────────────────────────────────────────────────────

class _CategoryRow(QFrame):
    """
    One row in the dialog: displays the category name, GM ID range, current
    override, and Browse SFZ / SF2 / VST3 / Clear buttons.
    """

    def __init__(self, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("row_frame")

        display_name, gm_ids, _default = GM_CATEGORIES[key]
        self._key = key
        self._override: Optional[Dict[str, Any]] = None

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        # ── Category label ─────────────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(2)

        name_lbl = QLabel(display_name)
        font = QFont()
        font.setBold(True)
        font.setPointSize(10)
        name_lbl.setFont(font)
        name_lbl.setStyleSheet(f"color:{_C['cyan']};")
        left.addWidget(name_lbl)

        id_range = _format_id_range(gm_ids)
        range_lbl = QLabel(f"GM {id_range}")
        range_lbl.setStyleSheet(f"color:{_C['text_dim']}; font-size:9px;")
        left.addWidget(range_lbl)

        name_container = QWidget()
        name_container.setLayout(left)
        name_container.setFixedWidth(180)
        root.addWidget(name_container)

        # ── Path display ───────────────────────────────────────────────────
        self._path_lbl = QLabel("(using built-in default)")
        self._path_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:10px; background:transparent;"
        )
        self._path_lbl.setWordWrap(False)
        self._path_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._path_lbl.setToolTip("")
        root.addWidget(self._path_lbl, stretch=1)

        # ── Action buttons ─────────────────────────────────────────────────
        sfz_btn = QPushButton("SFZ…")
        sfz_btn.setObjectName("browse_sfz")
        sfz_btn.setToolTip("Browse for a .sfz instrument file")
        sfz_btn.setFixedWidth(62)
        sfz_btn.clicked.connect(self._browse_sfz)
        root.addWidget(sfz_btn)

        sf2_btn = QPushButton("SF2…")
        sf2_btn.setObjectName("browse_sf2")
        sf2_btn.setToolTip("Browse for a .sf2 soundfont and pick a preset")
        sf2_btn.setFixedWidth(62)
        sf2_btn.clicked.connect(self._browse_sf2)
        root.addWidget(sf2_btn)

        vst3_btn = QPushButton("VST3…")
        vst3_btn.setObjectName("browse_vst3")
        vst3_btn.setToolTip("Browse for a .vst3 plugin")
        vst3_btn.setFixedWidth(62)
        vst3_btn.clicked.connect(self._browse_vst3)
        root.addWidget(vst3_btn)

        clear_btn = QPushButton("✕")
        clear_btn.setObjectName("clear_btn")
        clear_btn.setToolTip("Remove override — revert to built-in default")
        clear_btn.clicked.connect(self._clear)
        root.addWidget(clear_btn)

    # ── Public interface ───────────────────────────────────────────────────

    def set_override(self, entry: Optional[Dict[str, Any]]) -> None:
        """Display *entry* in this row.  Pass ``None`` to revert to default."""
        self._override = entry
        if not entry:
            self._path_lbl.setText("(using built-in default)")
            self._path_lbl.setToolTip("")
            self._path_lbl.setStyleSheet(
                f"color:{_C['text_dim']}; font-size:10px; background:transparent;")
            return

        t    = entry.get("type", "sfz")
        path = entry.get("path", "")
        if t == "sf2":
            bank  = entry.get("bank", 0)
            pst   = entry.get("preset", 0)
            pname = entry.get("preset_name", "")
            badge = f"[SF2]"
            detail = f"{os.path.basename(path)}  (B{bank}/P{pst}"
            if pname:
                detail += f" — {pname}"
            detail += ")"
            label = f"{badge} {detail}"
        else:
            badge = f"[{t.upper()}]"
            label = f"{badge} {os.path.basename(path)}"

        self._path_lbl.setText(label)
        self._path_lbl.setToolTip(path)
        self._path_lbl.setStyleSheet(
            f"color:{_C['text']}; font-size:10px; background:transparent;")

    def get_override(self) -> Optional[Dict[str, Any]]:
        """Return the current override dict, or ``None`` if none set."""
        return self._override

    # Backward-compat shims so existing callers using set_path/get_path still work.
    def set_path(self, path: str) -> None:
        if path and path.strip():
            self.set_override({"type": "sfz", "path": path})
        else:
            self.set_override(None)

    def get_path(self) -> str:
        if self._override and self._override.get("type") == "sfz":
            return self._override.get("path", "")
        return ""

    # ── Browse slots ───────────────────────────────────────────────────────

    def _browse_sfz(self) -> None:
        start = (self._override or {}).get("path", "") or os.path.expanduser("~")
        if os.path.isfile(start):
            start = os.path.dirname(start)
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Choose SFZ instrument — {GM_CATEGORIES[self._key][0]}",
            start,
            "SFZ Instruments (*.sfz);;All Files (*)",
        )
        if path:
            self.set_override({"type": "sfz", "path": path})

    def _browse_sf2(self) -> None:
        start = (self._override or {}).get("path", "") or os.path.expanduser("~")
        if os.path.isfile(start):
            start = os.path.dirname(start)
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Choose SF2 soundfont — {GM_CATEGORIES[self._key][0]}",
            start,
            "SoundFont Files (*.sf2);;All Files (*)",
        )
        if not path:
            return
        dlg = _Sf2PresetPickerDialog(path, self._key, self)
        if dlg.exec() == QDialog.Accepted:
            picked = dlg.picked()
            if picked:
                bank, preset, name = picked
                self.set_override({
                    "type":        "sf2",
                    "path":        path,
                    "bank":        bank,
                    "preset":      preset,
                    "preset_name": name,
                })

    def _browse_vst3(self) -> None:
        start = (self._override or {}).get("path", "") or os.path.expanduser("~")
        if os.path.isfile(start):
            start = os.path.dirname(start)
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Choose VST3 plugin — {GM_CATEGORIES[self._key][0]}",
            start,
            "VST3 Plugins (*.vst3);;All Files (*)",
        )
        if path:
            self.set_override({"type": "vst3", "path": path})

    def _clear(self) -> None:
        self.set_override(None)


# ─────────────────────────────────────────────────────────────────────────────
# Main dialog
# ─────────────────────────────────────────────────────────────────────────────

class GmDefaultsDialog(QDialog):
    """
    Settings dialog for GM instrument defaults.

    Open it modally::

        dlg = GmDefaultsDialog(parent=main_window)
        dlg.exec()

    Or non-modally and react to saves::

        dlg = GmDefaultsDialog(parent=main_window)
        dlg.defaults_changed.connect(my_slot)
        dlg.show()
    """

    defaults_changed = Signal()
    """Emitted after the user clicks Save and the JSON is written."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GM Instrument Defaults")
        self.setMinimumWidth(780)
        self.setModal(True)
        self.setStyleSheet(_DIALOG_STYLESHEET)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowContextHelpButtonHint
        )

        self._manager = GmDefaultsManager()
        self._rows: Dict[str, _CategoryRow] = {}

        self._build_ui()
        self._load_current_settings()

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        # ── Header ────────────────────────────────────────────────────────
        header = QLabel("Default instruments for MIDI open / import")
        header.setStyleSheet(
            f"color:{_C['text']}; font-size:13px; font-weight:bold;"
        )
        root.addWidget(header)

        sub = QLabel(
            "Assign a custom SFZ, SF2, or VST3 instrument to each GM program group.  "
            "Leave a row empty to use the built-in SF2 soundfont (GeneralUser GS)."
        )
        sub.setStyleSheet(f"color:{_C['text_dim']}; font-size:10px;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFixedHeight(1)
        root.addWidget(sep)

        # ── Scrollable category rows ───────────────────────────────────────
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(6)

        for key in GM_CATEGORIES:
            row = _CategoryRow(key, self)
            self._rows[key] = row
            scroll_layout.addWidget(row)

        scroll_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(360)
        root.addWidget(scroll)

        sep2 = QFrame()
        sep2.setObjectName("separator")
        sep2.setFixedHeight(1)
        root.addWidget(sep2)

        # ── Settings file path hint ────────────────────────────────────────
        path_hint = QLabel(
            f"Settings file: {GmDefaultsManager.SETTINGS_FILE}"
        )
        path_hint.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px;"
        )
        root.addWidget(path_hint)

        # ── Bottom buttons ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        reset_btn = QPushButton("↺  Reset All to Defaults")
        reset_btn.setObjectName("reset_btn")
        reset_btn.setToolTip("Remove all overrides — restore built-in SF2 soundfont")
        reset_btn.clicked.connect(self._on_reset_all)
        btn_row.addWidget(reset_btn)

        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("✔  Save")
        save_btn.setObjectName("save_btn")
        save_btn.setFixedWidth(110)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)

        root.addLayout(btn_row)

    # ── Data helpers ───────────────────────────────────────────────────────

    def _load_current_settings(self) -> None:
        """Populate rows from the persisted overrides."""
        overrides = self._manager.load()
        for key, row in self._rows.items():
            raw = overrides.get(key)
            if not raw:
                row.set_override(None)
            elif isinstance(raw, str) and raw.strip():
                row.set_override({"type": "sfz", "path": raw})
            elif isinstance(raw, dict) and raw.get("type") and raw.get("path"):
                row.set_override(raw)
            else:
                row.set_override(None)

    def _collect_overrides(self) -> Dict[str, Any]:
        """Read the current state of every row into a dict."""
        return {
            key: row.get_override()
            for key, row in self._rows.items()
            if row.get_override()
        }

    # ── Button slots ───────────────────────────────────────────────────────

    def _on_save(self) -> None:
        overrides = self._collect_overrides()
        self._manager.save(overrides)
        self.defaults_changed.emit()
        self.accept()

    def _on_reset_all(self) -> None:
        for row in self._rows.values():
            row.set_override(None)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_id_range(ids: list) -> str:
    """
    Format a list of GM IDs as a compact range string.

    Examples:
        [0..7]  + catch-all  → "0-7 (+ catch-all)"
        [24..31]             → "24-31"
        [128]                → "128 (Ch.10)"
    """
    if not ids:
        return ""
    if ids == [128]:
        return "128  (Ch.10)"
    contiguous = sorted(ids)
    start = contiguous[0]
    prev  = contiguous[0]
    for n in contiguous[1:]:
        if n == prev + 1:
            prev = n
        else:
            break
    run_end = prev
    label = f"{start}-{run_end}"
    leftover = [n for n in contiguous if n < start or n > run_end]
    if leftover:
        label += " (+ catch-all)"
    return label
