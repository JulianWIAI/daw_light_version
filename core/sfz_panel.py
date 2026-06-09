"""
sfz_panel.py -- PySide6 SFZ instrument browser and key-range visualizer.
=========================================================================
Displays the instrument structure parsed from an SFZ file:
  - Piano keyboard with coloured overlays for each region's key range
  - Velocity layer map (per-key stacked velocity bands)
  - CC controls listed with their labels
  - Round-robin info per region

Classes:
  SfzKeyRangeWidget  -- QPainter piano keyboard with region colour overlays
  SfzInfoPanel       -- dock/widget showing metadata table + CC labels
  SfzInstrumentPanel -- combined dock widget (KeyRangeWidget + InfoPanel)
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QRectF, Signal, Slot
from PySide6.QtGui  import (QColor, QFont, QLinearGradient, QPainter,
                             QPainterPath, QPen, QBrush)
from PySide6.QtWidgets import (
    QDockWidget, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .sfz_engine_python import (
    SfzInstrumentInfo, SfzRegionInfo, parse_sfz,
)

# ── Colour palette (matches the DAW's dark theme) ─────────────────────────────

_BACKGROUND  = QColor("#060A18")
_WHITE_KEY   = QColor("#1A2444")
_BLACK_KEY   = QColor("#0A0E22")
_KEY_OUTLINE = QColor("#1E2A4A")
_TEXT        = QColor("#C8E6FF")
_TEXT_DIM    = QColor("#3D5A80")

# Region overlay colours: cycle through a fixed palette for distinguishability.
_REGION_COLORS = [
    QColor(0, 229, 255, 80),    # cyan
    QColor(57, 255, 20, 80),    # lime
    QColor(255, 107, 43, 80),   # orange
    QColor(153, 69, 255, 80),   # purple
    QColor(255, 45, 158, 80),   # pink
    QColor(0, 255, 179, 80),    # teal
]


def _note_is_black(note: int) -> bool:
    return (note % 12) in (1, 3, 6, 8, 10)


def _note_to_white_index(note: int) -> float:
    """Return the position within the white-key sequence (0 = C, fractional for black keys)."""
    octave = note // 12
    semitone = note % 12
    # Map semitone to white-key offset (where black keys sit between whites).
    _white_offsets = [0, 0.5, 1, 1.5, 2, 3, 3.5, 4, 4.5, 5, 5.5, 6]
    return octave * 7 + _white_offsets[semitone]


# ── Key-range overlay widget ──────────────────────────────────────────────────

class SfzKeyRangeWidget(QWidget):
    """
    Draws a piano keyboard (MIDI notes 0-127) with semi-transparent coloured
    rectangles over each region's key range.  Velocity layering is shown as
    stacked bands within each key-column.
    """

    note_clicked = Signal(int)  # emits the MIDI note number on mouse press

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._regions: List[SfzRegionInfo] = []
        self.setMinimumHeight(100)
        self.setMinimumWidth(400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(110)
        self.setMouseTracking(True)
        self._hover_note: int = -1

    def set_regions(self, regions: List[SfzRegionInfo]) -> None:
        self._regions = regions
        self.update()

    def clear(self) -> None:
        self._regions = []
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_piano(p)
        self._draw_region_overlays(p)
        p.end()

    def _draw_piano(self, p: QPainter) -> None:
        """Draw 128 MIDI keys as a piano keyboard."""
        w = self.width()
        h = self.height()

        p.fillRect(0, 0, w, h, _BACKGROUND)

        # Count white keys (0-127): C, D, E, F, G, A, B across octaves.
        # 128 notes = 10 full octaves + 8 extra (C0-G#0 range ends at G9=127).
        # White key count from 0 to 127: 75 white keys.
        total_white = sum(1 for n in range(128) if not _note_is_black(n))
        key_w = w / total_white

        white_idx = 0
        # Draw white keys first.
        p.setPen(QPen(_KEY_OUTLINE, 0.5))
        for note in range(128):
            if _note_is_black(note):
                continue
            x = white_idx * key_w
            p.fillRect(QRectF(x + 0.5, 1, key_w - 1, h - 2), _WHITE_KEY)
            p.drawRect(QRectF(x + 0.5, 1, key_w - 1, h - 2))
            # Octave label on C notes.
            if note % 12 == 0:
                p.setPen(_TEXT_DIM)
                p.setFont(QFont("Arial", 6))
                p.drawText(QRectF(x, h - 14, key_w * 2, 12),
                           Qt.AlignmentFlag.AlignLeft,
                           f"C{note // 12 - 1}")
                p.setPen(QPen(_KEY_OUTLINE, 0.5))
            white_idx += 1

        # Draw black keys on top.
        white_idx = 0
        for note in range(128):
            if not _note_is_black(note):
                white_idx += 1
                continue
            # Black key sits to the right of the previous white key.
            x = (white_idx - 0.65) * key_w
            bh = h * 0.62
            p.fillRect(QRectF(x, 1, key_w * 0.55, bh), _BLACK_KEY)
            p.drawRect(QRectF(x, 1, key_w * 0.55, bh))

    def _draw_region_overlays(self, p: QPainter) -> None:
        """Colour each region's key range with a semi-transparent overlay."""
        w = self.width()
        h = self.height()
        total_white = sum(1 for n in range(128) if not _note_is_black(n))
        key_w = w / total_white

        def note_x(note: int) -> float:
            wi = sum(1 for n in range(note) if not _note_is_black(n))
            return wi * key_w

        for idx, region in enumerate(self._regions):
            color = _REGION_COLORS[idx % len(_REGION_COLORS)]
            lo = region.key_range.lo
            hi = region.key_range.hi
            if lo > hi:
                continue
            x1 = note_x(lo)
            x2 = note_x(min(hi + 1, 127))
            # Velocity band: map vel range to vertical strip.
            vel_h  = h * 0.25
            vel_y  = h - vel_h - (region.vel_range.lo / 127.0) * vel_h
            vel_bh = max(2.0, (region.vel_range.hi - region.vel_range.lo) / 127.0 * vel_h)
            p.fillRect(QRectF(x1, 0, x2 - x1, h * 0.7), color)
            # Velocity strip at bottom.
            vel_color = QColor(color.red(), color.green(), color.blue(), 160)
            p.fillRect(QRectF(x1, h - vel_h, x2 - x1, vel_h), vel_color)

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        note = self._x_to_note(event.position().x())
        if note >= 0:
            self.note_clicked.emit(note)

    def _x_to_note(self, x: float) -> int:
        w = self.width()
        total_white = sum(1 for n in range(128) if not _note_is_black(n))
        key_w = w / total_white
        white_hit = int(x / key_w)
        count = 0
        for note in range(128):
            if not _note_is_black(note):
                if count == white_hit:
                    return note
                count += 1
        return -1


# ── Info panel: metadata table + CC labels ───────────────────────────────────

class SfzInfoPanel(QFrame):
    """Two-column table showing region info and a list of CC labels."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background:#060A18; color:#C8E6FF; font-size:10px;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Region table.
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Key Lo", "Key Hi", "Vel Lo", "Vel Hi", "Sample"]
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            "QTableWidget{background:#060A18; color:#C8E6FF; gridline-color:#1E2A4A;}"
            "QHeaderView::section{background:#0A0E22; color:#3D5A80; border:none; padding:2px;}"
            "QTableWidget::item:alternate{background:#0A1020;}"
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self._table, stretch=3)

        # CC labels.
        cc_lbl = QLabel("CC Controls")
        cc_lbl.setStyleSheet("color:#3D5A80; font-size:8px;")
        layout.addWidget(cc_lbl)

        self._cc_table = QTableWidget(0, 2)
        self._cc_table.setHorizontalHeaderLabels(["CC #", "Label"])
        self._cc_table.horizontalHeader().setStretchLastSection(True)
        self._cc_table.setStyleSheet(self._table.styleSheet())
        self._cc_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._cc_table.setMaximumHeight(100)
        layout.addWidget(self._cc_table)

    def populate(self, info: SfzInstrumentInfo) -> None:
        # Regions.
        self._table.setRowCount(len(info.regions))
        for row, r in enumerate(info.regions):
            self._table.setItem(row, 0, QTableWidgetItem(str(r.key_range.lo)))
            self._table.setItem(row, 1, QTableWidgetItem(str(r.key_range.hi)))
            self._table.setItem(row, 2, QTableWidgetItem(str(r.vel_range.lo)))
            self._table.setItem(row, 3, QTableWidgetItem(str(r.vel_range.hi)))
            self._table.setItem(row, 4, QTableWidgetItem(r.sample))
        # CC labels.
        self._cc_table.setRowCount(len(info.cc_labels))
        for row, (num, lbl) in enumerate(info.cc_labels):
            self._cc_table.setItem(row, 0, QTableWidgetItem(str(num)))
            self._cc_table.setItem(row, 1, QTableWidgetItem(lbl))

    def clear(self) -> None:
        self._table.setRowCount(0)
        self._cc_table.setRowCount(0)


# ── Combined dock widget ──────────────────────────────────────────────────────

class SfzInstrumentPanel(QDockWidget):
    """
    Full SFZ instrument browser dock.

    Contains:
      • [Load SFZ] button
      • Instrument name + region/group count
      • SfzKeyRangeWidget (piano keyboard with overlays)
      • SfzInfoPanel (region table + CC labels)

    Usage:
        panel = SfzInstrumentPanel(sfz_engine, parent=main_window)
        main_window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, panel)
    """

    def __init__(self, sfz_engine=None, parent: Optional[QWidget] = None) -> None:
        super().__init__("SFZ INSTRUMENT", parent)
        self._engine = sfz_engine
        self._info: Optional[SfzInstrumentInfo] = None

        # ── Layout ────────────────────────────────────────────────────────────
        root_w = QWidget()
        root_w.setStyleSheet("background:#060A18;")
        root = QVBoxLayout(root_w)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # Header row: load button + name label.
        header_row = QHBoxLayout()
        self._load_btn = QPushButton("Load SFZ…")
        self._load_btn.setFixedHeight(24)
        self._load_btn.setStyleSheet(
            "QPushButton{background:#0A0E22; border:1px solid rgba(0,229,255,0.3);"
            " border-radius:3px; color:#00E5FF; font-size:10px; padding:0 8px;}"
            "QPushButton:hover{background:rgba(0,229,255,0.1);}"
        )
        self._load_btn.clicked.connect(self._on_load_clicked)
        header_row.addWidget(self._load_btn)

        self._name_lbl = QLabel("(no instrument)")
        self._name_lbl.setStyleSheet("color:#3D5A80; font-size:10px; background:transparent;")
        header_row.addWidget(self._name_lbl, stretch=1)
        root.addLayout(header_row)

        # Key-range piano.
        self._keyboard = SfzKeyRangeWidget()
        root.addWidget(self._keyboard)

        # Info panel.
        self._info_panel = SfzInfoPanel()
        root.addWidget(self._info_panel, stretch=1)

        self.setWidget(root_w)
        self.setMinimumHeight(280)

    # ── Slots ─────────────────────────────────────────────────────────────────

    @Slot()
    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open SFZ Instrument", "",
            "SFZ Instruments (*.sfz);;All Files (*)"
        )
        if path:
            self.load_sfz(path)

    def load_sfz(self, path: str) -> None:
        """Parse the SFZ file and refresh the display."""
        self._info = parse_sfz(path)  # uses C++ or Python fallback
        if self._engine is not None:
            self._engine.load_sfz(path)

        count_str = (f"{self._info.num_regions} regions, "
                     f"{self._info.num_groups} groups")
        self._name_lbl.setText(f"{self._info.name}  [{count_str}]")
        self._name_lbl.setStyleSheet(
            "color:#C8E6FF; font-size:10px; background:transparent;"
        )

        self._keyboard.set_regions(self._info.regions)
        self._info_panel.populate(self._info)

    def get_info(self) -> Optional[SfzInstrumentInfo]:
        return self._info
