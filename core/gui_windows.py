"""
gui_windows.py — SBS-Synth Master  ✦  Bioluminescent Crystal UI
================================================================
Visual concept: "Pokémon Legends Z-A meets Futuristic Alice in Wonderland"
    Deep space-black backgrounds with glowing crystal-cyan and hot-pink
    neon accents.  Every panel looks like a floating holographic slab lit
    from within.  Notes pulse like bioluminescent spores.

Architecture (unchanged from previous revision):
    Pure presentation layer — all logic lives in AudioEngine / MidiLogic /
    ControllerManager / EffectChain.  The window only translates Qt events
    into calls on those objects.

New in this revision:
    • Per-instrument piano roll  — active track vivid, others are ghost notes.
    • EffectsPanel dock          — EQ, Reverb, Compressor, Chorus per track.
    • Composition-mode toggle    — switch between focused-track and all-tracks.
    • WAV export                 — renders via FluidSynth CLI.
    • New Project                — clears everything and resets state.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import numpy as np

from PySide6.QtCore import Qt, QPoint, QRectF, QPointF, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QKeyEvent,
    QLinearGradient, QRadialGradient, QPainterPath, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSlider, QPushButton, QSpinBox, QDoubleSpinBox, QComboBox,
    QScrollArea, QScrollBar, QFrame, QDockWidget, QStatusBar, QToolBar,
    QDialog, QGridLayout, QLineEdit, QMessageBox, QFileDialog, QInputDialog,
    QListWidget, QListWidgetItem, QSplitter, QSizePolicy, QCheckBox,
    QGroupBox, QDial, QStackedWidget, QMenu, QRadioButton, QButtonGroup,
    QProgressDialog, QTabWidget, QTableWidget, QTableWidgetItem,
)

from .audio_engine import AudioEngine, InstrumentPlugin, GM_INSTRUMENTS
from .midi_logic import (
    MidiLogic, MidiNote, MidiClip, MidiTrack,
    AudioClip, AudioTrack,
)
from .controller import ControllerManager, SCALES
from .effects import EffectChain
from .vst_engine import VstManager, VstTrack, scan_vst_paths
from .audio_fx_chain import AudioFxChain
from .audio_file_player import AudioFilePlayer
from .audio_fx_panel import AudioFxPanel
from .audio_mixer_strip import AudioMixerStrip
from .import_manager import ImportManager, detect_file_type, AUDIO_EXTENSIONS
from .instrument_renderer import InstrumentRenderer
from .project_manager import ProjectManager
from .export_worker import ExportWorker
from .automation_lane import AutomationPanel, AutomationEnvelope
from .channel_rack import ChannelRackWindow, ChannelStepData
from .instrument_preview import InstrumentPreviewWidget
from .rack_sampler_engine import RackSamplerEngine
from .waveform_peaks_python import WaveformPeakGenerator
from .humanizer_panel import HumanizerPanel
from .velocity_humanizer_python import get_humanizer
from .grid_snapper_python import get_grid_snapper, all_grid_labels
from .grid_settings_panel import GridSettingsPanel
from .export_dialog import ExportDialog as MasterExportDialog
from .mastering_export_worker import TrackRenderInfo
from .project_render_info import AutomationRenderInfo, MidiTrackRenderInfo, FullProjectRenderInfo
from .master_bus_python import get_master_bus
from .master_bus_channel import MasterBusChannel
from .sfz_engine_python import get_sfz_engine, parse_sfz
from .sfz_panel import SfzKeyRangeWidget
from .sfz_realtime_player import SfzRealTimePlayer
from .gm_defaults_dialog import GmDefaultsDialog
from .gm_defaults_manager import GmDefaultsManager
from .midi_drop_importer import _parse_midi_file
from .instrument_randomizer import InstrumentRandomizer, build_library_from_engine
from .auto_mastering_dialog import AutoMasterDialog
from .ai_mix_assistant import AIMixAssistant
# Decent Sampler (.dspreset) support — parser, GUI panel, engine factory, player.
from .dspreset_parser import parse_dspreset
from .dspreset_panel import DsPresetPanel
from .dspreset_engine import get_ds_engine, load_preset_into_engine
from .dspreset_realtime_player import DsRealTimePlayer
from .telemetry_manager import TelemetryManager

logger = logging.getLogger(__name__)



# ═══════════════════════════════════════════════════════════════════════════
# CRYSTAL BIOLUMINESCENCE PALETTE
# ═══════════════════════════════════════════════════════════════════════════

C = {
    # Backgrounds — layers of dark void
    "void":      "#030308",   # main window background
    "abyss":     "#060A18",   # panel backgrounds
    "deep":      "#0A0E22",   # widget backgrounds
    "surface":   "#0E1430",   # hover / elevated surface

    # Neon crystal accents
    "cyan":      "#00E5FF",   # primary — electric crystal blue
    "pink":      "#FF2D9E",   # secondary — alien blossom magenta
    "purple":    "#9945FF",   # tertiary — deep crystal purple
    "lime":      "#39FF14",   # active / playing — bioluminescent green
    "gold":      "#FFD700",   # markers / special — rare crystal gold
    "orange":    "#FF6B2B",   # warning / record

    # Text
    "text":      "#C8E6FF",   # primary text — ice white-blue
    "text_dim":  "#3D5A80",   # secondary text — muted ocean blue

    # Track neons (per-lane colour sequence)
    "tracks": [
        "#00E5FF",  # 0  cyan
        "#FF2D9E",  # 1  hot pink
        "#39FF14",  # 2  lime
        "#FFD700",  # 3  gold
        "#9945FF",  # 4  purple
        "#FF6B2B",  # 5  orange
        "#00FFB3",  # 6  teal
        "#FF4444",  # 7  red
        "#44AAFF",  # 8  sky blue
        "#FF88CC",  # 9  light pink
        "#AAFF44",  # 10 yellow-green
        "#FF8844",  # 11 amber
        "#AA44FF",  # 12 violet
        "#44FFAA",  # 13 mint
        "#FF4488",  # 14 rose
        "#44FFFF",  # 15 aqua
    ],
}

STYLESHEET = f"""
/* ─── Global ─────────────────────────────────────────────────────────── */
QMainWindow, QWidget {{
    background-color: {C["void"]};
    color: {C["text"]};
    font-family: 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px;
}}
QDockWidget {{
    background: {C["abyss"]};
    titlebar-close-icon: none;
}}
QDockWidget::title {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 {C["deep"]}, stop:1 {C["abyss"]});
    padding: 5px 10px;
    font-weight: bold; font-size: 11px; letter-spacing: 2px;
    color: {C["cyan"]};
    border-bottom: 1px solid rgba(0,229,255,0.3);
}}

/* ─── Buttons ─────────────────────────────────────────────────────────── */
QPushButton {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 {C["surface"]}, stop:1 {C["deep"]});
    color: {C["text"]};
    border: 1px solid rgba(0,229,255,0.25);
    border-radius: 5px;
    padding: 5px 14px;
    font-size: 12px; letter-spacing: 0.5px;
}}
QPushButton:hover {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 rgba(0,229,255,0.18), stop:1 rgba(0,229,255,0.08));
    border: 1px solid rgba(0,229,255,0.7);
    color: {C["cyan"]};
}}
QPushButton:pressed {{
    background: rgba(0,229,255,0.25);
    border: 1px solid {C["cyan"]};
}}
QPushButton:checked {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 rgba(255,45,158,0.4), stop:1 rgba(255,45,158,0.15));
    border: 1px solid {C["pink"]};
    color: {C["pink"]};
}}

/* ─── Sliders ─────────────────────────────────────────────────────────── */
QSlider::groove:horizontal {{
    height: 3px;
    background: rgba(0,229,255,0.12);
    border-radius: 1px;
}}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 {C["purple"]}, stop:1 {C["cyan"]});
    border-radius: 1px;
}}
QSlider::handle:horizontal {{
    background: {C["cyan"]};
    border: 2px solid {C["abyss"]};
    width: 13px; height: 13px;
    border-radius: 7px; margin: -5px 0;
}}
QSlider::groove:vertical {{
    width: 3px;
    background: rgba(0,229,255,0.12);
    border-radius: 1px;
}}
QSlider::sub-page:vertical {{
    background: qlineargradient(x1:0,y1:1,x2:0,y2:0,
        stop:0 {C["purple"]}, stop:1 {C["cyan"]});
    border-radius: 1px;
}}
QSlider::handle:vertical {{
    background: {C["cyan"]};
    border: 2px solid {C["abyss"]};
    width: 13px; height: 13px;
    border-radius: 7px; margin: 0 -5px;
}}

/* ─── Combos / Spinboxes ─────────────────────────────────────────────── */
QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {C["deep"]};
    border: 1px solid rgba(0,229,255,0.2);
    border-radius: 4px; padding: 3px 8px;
    color: {C["text"]};
}}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: rgba(0,229,255,0.6);
}}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{
    background: {C["deep"]};
    border: 1px solid rgba(0,229,255,0.3);
    selection-background-color: rgba(0,229,255,0.2);
    color: {C["text"]};
}}

/* ─── Scroll bars ────────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {C["abyss"]}; width: 7px; border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: rgba(0,229,255,0.3); border-radius: 3px; min-height: 20px;
}}
QScrollBar:horizontal {{
    background: {C["abyss"]}; height: 7px; border-radius: 3px;
}}
QScrollBar::handle:horizontal {{
    background: rgba(0,229,255,0.3); border-radius: 3px; min-width: 20px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ border: none; background: none; }}

/* ─── List widgets ───────────────────────────────────────────────────── */
QListWidget {{
    background: {C["deep"]}; border: 1px solid rgba(0,229,255,0.18);
    border-radius: 4px; color: {C["text"]}; outline: none;
}}
QListWidget::item:selected {{
    background: rgba(0,229,255,0.2);
    color: {C["cyan"]};
}}
QListWidget::item:hover {{
    background: rgba(0,229,255,0.08);
}}

/* ─── Line edit ──────────────────────────────────────────────────────── */
QLineEdit {{
    background: {C["deep"]}; border: 1px solid rgba(0,229,255,0.2);
    border-radius: 4px; padding: 5px 10px; color: {C["text"]};
}}
QLineEdit:focus {{ border-color: rgba(0,229,255,0.6); }}

/* ─── Group box ──────────────────────────────────────────────────────── */
QGroupBox {{
    border: 1px solid rgba(0,229,255,0.2);
    border-radius: 6px; margin-top: 10px; padding-top: 6px;
    color: {C["text_dim"]}; font-size: 11px; letter-spacing: 1px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; padding: 0 5px;
    color: rgba(0,229,255,0.7);
}}

/* ─── Status bar ─────────────────────────────────────────────────────── */
QStatusBar {{
    background: {C["abyss"]};
    border-top: 1px solid rgba(0,229,255,0.1);
}}
QStatusBar QLabel {{ padding: 0 8px; }}

/* ─── Tool bar ───────────────────────────────────────────────────────── */
QToolBar {{
    background: {C["abyss"]};
    border-bottom: 1px solid rgba(0,229,255,0.15);
    spacing: 4px; padding: 3px 6px;
}}
QToolBar::separator {{
    background: rgba(0,229,255,0.15); width: 1px; margin: 4px 6px;
}}

/* ─── Checkboxes ─────────────────────────────────────────────────────── */
QCheckBox {{ color: {C["text"]}; spacing: 6px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid rgba(0,229,255,0.4); border-radius: 3px;
    background: {C["deep"]};
}}
QCheckBox::indicator:checked {{
    background: {C["cyan"]};
    border-color: {C["cyan"]};
}}

/* ─── Dial ───────────────────────────────────────────────────────────── */
QDial {{
    background: transparent;
}}
"""


# ═══════════════════════════════════════════════════════════════════════════
# Utility widget: labelled vertical slider
# ═══════════════════════════════════════════════════════════════════════════

def _make_vslider(min_val: int, max_val: int, init: int,
                  parent: QWidget = None) -> QSlider:
    s = QSlider(Qt.Vertical, parent)
    s.setRange(min_val, max_val)
    s.setValue(init)
    s.setFixedHeight(80)
    return s


def _make_hslider(min_val: int, max_val: int, init: int,
                  parent: QWidget = None) -> QSlider:
    s = QSlider(Qt.Horizontal, parent)
    s.setRange(min_val, max_val)
    s.setValue(init)
    return s


def _label(text: str, color: str = C["text_dim"],
           size: int = 10, bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(
        f"color:{color}; font-size:{size}px; font-weight:{weight};"
        " background:transparent; letter-spacing:0.5px;"
    )
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


# ═══════════════════════════════════════════════════════════════════════════
# Effects Panel
# ═══════════════════════════════════════════════════════════════════════════

class EffectsPanel(QWidget):
    """
    Per-instrument DSP effects panel.

    Sections (top → bottom):
        EQ       — 5 vertical band sliders with a curve display area.
        Reverb   — Room, Damp, Width, Level sliders + enable toggle.
        Compressor — Threshold, Ratio, Attack, Release + enable toggle.
        Chorus   — Level, Speed, Depth + enable toggle.

    All control changes call `_push()` which applies them to FluidSynth
    immediately via the EffectChain.apply() route.
    """

    effect_changed = Signal()  # emitted whenever any parameter changes

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._chain: Optional[EffectChain] = None
        self._engine: Optional[AudioEngine] = None
        self._building = True

        self.setFixedWidth(220)
        self.setStyleSheet(f"background:{C['abyss']};")

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Track label ────────────────────────────────────────────────
        self._track_label = _label("No track selected", C["cyan"], 11, True)
        root.addWidget(self._track_label)

        # ── EQ section ────────────────────────────────────────────────
        eq_grp = self._make_group("EQ — 5 BAND")
        eq_inner = QVBoxLayout(eq_grp)
        eq_inner.setContentsMargins(4, 4, 4, 4)
        eq_inner.setSpacing(2)

        self._eq_enable = QCheckBox("Active")
        self._eq_enable.setChecked(True)
        self._eq_enable.toggled.connect(self._push)
        eq_inner.addWidget(self._eq_enable)

        # 5 band sliders in a row
        bands_widget = QWidget()
        bands_layout = QHBoxLayout(bands_widget)
        bands_layout.setContentsMargins(0, 0, 0, 0)
        bands_layout.setSpacing(4)
        self._eq_sliders: List[QSlider] = []
        for freq_label in ["32", "250", "1k", "4k", "16k"]:
            col = QVBoxLayout()
            col.setSpacing(2)
            sl = _make_vslider(-24, 24, 0)
            sl.valueChanged.connect(self._push)
            self._eq_sliders.append(sl)
            col.addWidget(sl, alignment=Qt.AlignCenter)
            col.addWidget(_label(freq_label, C["text_dim"], 9))
            bands_layout.addLayout(col)

        eq_inner.addWidget(bands_widget)
        eq_grp.setLayout(eq_inner)
        root.addWidget(eq_grp)

        # ── Reverb section ─────────────────────────────────────────────
        rev_grp = self._make_group("REVERB")
        rev_inner = QVBoxLayout(rev_grp)
        rev_inner.setContentsMargins(4, 4, 4, 4)
        rev_inner.setSpacing(4)

        self._rev_enable = QCheckBox("Active")
        self._rev_enable.setChecked(True)
        self._rev_enable.toggled.connect(self._push)
        rev_inner.addWidget(self._rev_enable)

        self._rev_room  = self._make_param_row(rev_inner, "Room",  0, 100, 50)
        self._rev_damp  = self._make_param_row(rev_inner, "Damp",  0, 100, 40)
        self._rev_width = self._make_param_row(rev_inner, "Width", 0, 100, 70)
        self._rev_level = self._make_param_row(rev_inner, "Level", 0, 100, 25)

        rev_grp.setLayout(rev_inner)
        root.addWidget(rev_grp)

        # ── Compressor section ─────────────────────────────────────────
        comp_grp = self._make_group("COMPRESSOR")
        comp_inner = QVBoxLayout(comp_grp)
        comp_inner.setContentsMargins(4, 4, 4, 4)
        comp_inner.setSpacing(4)

        self._comp_enable = QCheckBox("Active")
        self._comp_enable.setChecked(False)
        self._comp_enable.toggled.connect(self._push)
        comp_inner.addWidget(self._comp_enable)

        self._comp_thresh  = self._make_param_row(comp_inner, "Thresh", 0, 127, 90)
        self._comp_ratio   = self._make_param_row(comp_inner, "Ratio",  1,  20,  4)
        self._comp_attack  = self._make_param_row(comp_inner, "Attack", 1, 200, 10)
        self._comp_release = self._make_param_row(comp_inner, "Rel.",   1, 500,100)

        comp_grp.setLayout(comp_inner)
        root.addWidget(comp_grp)

        # ── Chorus section ─────────────────────────────────────────────
        cho_grp = self._make_group("CHORUS")
        cho_inner = QVBoxLayout(cho_grp)
        cho_inner.setContentsMargins(4, 4, 4, 4)
        cho_inner.setSpacing(4)

        self._cho_enable = QCheckBox("Active")
        self._cho_enable.setChecked(False)
        self._cho_enable.toggled.connect(self._push)
        cho_inner.addWidget(self._cho_enable)

        self._cho_level = self._make_param_row(cho_inner, "Level", 0, 100, 30)
        self._cho_speed = self._make_param_row(cho_inner, "Speed", 1,  50,  3)
        self._cho_depth = self._make_param_row(cho_inner, "Depth", 0, 300, 80)

        cho_grp.setLayout(cho_inner)
        root.addWidget(cho_grp)

        root.addStretch()
        self._building = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_group(self, title: str) -> QGroupBox:
        g = QGroupBox(title)
        g.setStyleSheet(f"""
            QGroupBox {{
                border: 1px solid rgba(0,229,255,0.2);
                border-radius: 6px; margin-top: 10px; padding-top: 6px;
                color: rgba(0,229,255,0.6); font-size: 10px; letter-spacing: 1px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 8px; padding: 0 4px;
            }}
        """)
        return g

    def _make_param_row(self, layout: QVBoxLayout,
                        label: str, lo: int, hi: int, init: int) -> QSlider:
        """Add a labelled horizontal slider row and return the slider."""
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(_label(label, C["text_dim"], 10))
        sl = _make_hslider(lo, hi, init)
        sl.valueChanged.connect(self._push)
        row.addWidget(sl)
        val_lbl = _label(str(init), C["cyan"], 9)
        sl.valueChanged.connect(lambda v, l=val_lbl: l.setText(str(v)))
        row.addWidget(val_lbl)
        layout.addLayout(row)
        return sl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_chain(
        self, chain: EffectChain, engine: AudioEngine, track_name: str
    ) -> None:
        """Switch the panel to display and edit a different EffectChain."""
        self._chain  = chain
        self._engine = engine
        self._track_label.setText(f"✦  {track_name}")
        self._building = True   # suppress _push() during load

        # EQ
        self._eq_enable.setChecked(chain.eq_enabled)
        vals = [chain.eq_32, chain.eq_250, chain.eq_1k, chain.eq_4k, chain.eq_16k]
        for sl, v in zip(self._eq_sliders, vals):
            sl.setValue(int(v * 2))   # ±12 dB → ±24 slider units

        # Reverb
        self._rev_enable.setChecked(chain.reverb_enabled)
        self._rev_room .setValue(int(chain.reverb_room  * 100))
        self._rev_damp .setValue(int(chain.reverb_damp  * 100))
        self._rev_width.setValue(int(chain.reverb_width * 100))
        self._rev_level.setValue(int(chain.reverb_level * 100))

        # Compressor
        self._comp_enable .setChecked(chain.comp_enabled)
        self._comp_thresh .setValue(int(chain.comp_threshold))
        self._comp_ratio  .setValue(int(chain.comp_ratio))
        self._comp_attack .setValue(int(chain.comp_attack))
        self._comp_release.setValue(int(chain.comp_release))

        # Chorus
        self._cho_enable.setChecked(chain.chorus_enabled)
        self._cho_level .setValue(int(chain.chorus_level * 100))
        self._cho_speed .setValue(int(chain.chorus_speed * 10))
        self._cho_depth .setValue(int(chain.chorus_depth * 10))

        self._building = False

        # Apply immediately so FluidSynth reflects this chain's settings right
        # away — otherwise FluidSynth keeps its heavy built-in reverb defaults
        # until the user moves a slider, making the panel appear non-functional.
        if self._engine is not None:
            self._engine.apply_effect_chain(self._chain)

    # ------------------------------------------------------------------

    def _push(self) -> None:
        """Read all widget values into the EffectChain and apply to engine."""
        if self._building or self._chain is None:
            return

        # EQ
        self._chain.eq_enabled = self._eq_enable.isChecked()
        db_vals = [sl.value() / 2.0 for sl in self._eq_sliders]
        self._chain.eq_32, self._chain.eq_250, self._chain.eq_1k, \
            self._chain.eq_4k, self._chain.eq_16k = db_vals

        # Reverb
        self._chain.reverb_enabled = self._rev_enable.isChecked()
        self._chain.reverb_room    = self._rev_room .value() / 100.0
        self._chain.reverb_damp    = self._rev_damp .value() / 100.0
        self._chain.reverb_width   = self._rev_width.value() / 100.0
        self._chain.reverb_level   = self._rev_level.value() / 100.0

        # Compressor
        self._chain.comp_enabled   = self._comp_enable .isChecked()
        self._chain.comp_threshold = float(self._comp_thresh .value())
        self._chain.comp_ratio     = float(self._comp_ratio  .value())
        self._chain.comp_attack    = float(self._comp_attack .value())
        self._chain.comp_release   = float(self._comp_release.value())

        # Chorus
        self._chain.chorus_enabled = self._cho_enable.isChecked()
        self._chain.chorus_level   = self._cho_level.value() / 100.0
        self._chain.chorus_speed   = self._cho_speed.value() / 10.0
        self._chain.chorus_depth   = self._cho_depth.value() / 10.0

        if self._engine:
            self._engine.apply_effect_chain(self._chain)

        self.effect_changed.emit()


# ═══════════════════════════════════════════════════════════════════════════
# Piano Roll Widget
# ═══════════════════════════════════════════════════════════════════════════

class PianoRollWidget(QWidget):
    """
    Scrollable MIDI grid with per-instrument focus and ghost-note display.

    Focus mode (default):
        Active track's notes are drawn vivid and fully interactive.
        All other tracks' notes appear as semi-transparent ghost outlines
        so you can see the full composition context without confusion.

    Composition mode (toggle):
        Every track is drawn at full brightness.  Useful for seeing how
        all instrument lines fit together.
    """

    # ── Signals ───────────────────────────────────────────────────────────────
    note_added          = Signal(int, float, float, int)  # ch, rel_beat, dur, pitch
    note_removed        = Signal(int, int)                # ch, note_id
    note_moved          = Signal(int, int, float, int)    # ch, note_id, new_rel_beat, new_pitch
    note_resized        = Signal(int, int, float)         # ch, note_id, new_duration
    loop_region_changed = Signal(float, float)            # start_beat, end_beat
    seek_requested      = Signal(float)                   # beat position
    scroll_x_changed    = Signal(float)                   # view_x
    view_y_changed      = Signal(int)                     # _view_y (scrollbar sync)

    BEAT_WIDTH:    int = 80
    NOTE_HEIGHT:   int = 14
    PITCH_MIN:     int = 0     # C-1 (full MIDI range)
    PITCH_MAX:     int = 128   # exclusive upper bound (127 = G9)
    HEADER_HEIGHT: int = 30
    PIANO_WIDTH:   int = 52

    # Mouse drag modes
    _MODE_NONE   = "none"
    _MODE_DRAW   = "draw"     # drawing a new note
    _MODE_RESIZE = "resize"   # dragging right edge of a note
    _MODE_MOVE   = "move"     # dragging note body / selected notes
    _MODE_LASSO  = "lasso"    # rubber-band selection rectangle

    # Width in pixels of the right-edge resize handle on each note
    RESIZE_GRIP = 8

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # ── Display data ──────────────────────────────────────────────────
        self._tracks:         List[MidiTrack] = []
        self._channel_colors: Dict[int, QColor] = {}
        self._active_channel: int   = 0
        self._total_beats:    float = 32.0
        self._composition_mode: bool = False
        self._active_clip:    Optional[MidiClip] = None  # clip currently edited

        # ── Viewport state ────────────────────────────────────────────────
        self._view_x:   float = 0.0
        self._view_y:   int   = 0
        self._vy_accum: float = 0.0   # sub-step accumulator for trackpad scroll

        # ── Selection ─────────────────────────────────────────────────────
        self._selected_ids: set = set()   # set of note_id ints

        # ── Drag state ────────────────────────────────────────────────────
        self._drag_mode: str = self._MODE_NONE

        # Draw mode (left-drag on empty area → new note)
        self._draw_start: Optional[Tuple[float, int]] = None

        # Resize mode (drag right edge of a note)
        self._resize_note_id:       int   = -1
        self._resize_orig_duration: float = 1.0

        # Move mode (drag body of a note or the selection)
        self._move_origin_beat:   float = 0.0   # beat at drag start
        self._move_origin_pitch:  int   = 0     # pitch at drag start
        self._move_notes_state:   Dict[int, Tuple[float, int]] = {}
        #   note_id → (original_rel_beat, original_pitch)

        # Lasso mode (shift+drag or drag on empty area)
        self._lasso_start: Optional[QPointF] = None
        self._lasso_end:   Optional[QPointF] = None

        # Last click beat position — used as paste target for Cmd+V
        self._last_click_beat: float = 0.0

        # ── Loop region ───────────────────────────────────────────────────
        self._loop_enabled:    bool  = False
        self._loop_start:      float = 0.0
        self._loop_end:        float = 8.0
        self._loop_drag_origin: Optional[float] = None

        # Ruler press tracking (click = seek, drag = loop)
        self._ruler_pressed:    bool  = False
        self._ruler_press_x:    int   = 0
        self._ruler_press_beat: float = 0.0

        # Edit mode: "draw" (default) or "select"
        self._edit_mode: str = "draw"

        # Current playhead position in beats — updated from MainWindow refresh tick.
        self._playhead_beat: float = 0.0

        # Grid snap engine (C++ GridSnapper or Python fallback).
        # Initialized to 1/16 grid, BarsBeats ruler.
        self._snapper = get_grid_snapper()
        self._snapper.set_grid("1/16")

        # Convenience alias used by the legacy drawing code path.
        self._grid_beats: float = self._snapper.grid_beats()

        self.setMinimumSize(640, 400)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setStyleSheet(f"background:{C['void']};")

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        """Reset view_x when the piano roll becomes visible after being hidden."""
        super().showEvent(event)
        if self._active_clip is not None:
            self._view_x = 0.0
        self.update()

    def keyPressEvent(self, ev) -> None:
        """Explicitly ignore key events so they propagate to the main window."""
        ev.ignore()

    def keyReleaseEvent(self, ev) -> None:
        """Explicitly ignore key events so they propagate to the main window."""
        ev.ignore()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_tracks(self, tracks: List[MidiTrack]) -> None:
        self._tracks = tracks
        self._channel_colors = {t.channel: QColor(t.color) for t in tracks}
        self.update()

    def set_active_channel(self, ch: int) -> None:
        self._active_channel = ch
        self.update()

    def set_composition_mode(self, on: bool) -> None:
        self._composition_mode = on
        self.update()

    def set_total_beats(self, beats: float) -> None:
        self._total_beats = beats
        self.update()

    def set_playhead_beat(self, beat: float) -> None:
        """Update the playhead position (in beats) and schedule a repaint."""
        self._playhead_beat = beat
        self.update()

    def set_loop_region(self, enabled: bool, start: float, end: float) -> None:
        self._loop_enabled = enabled
        self._loop_start   = start
        self._loop_end     = end
        self.update()

    def set_edit_mode(self, mode: str) -> None:
        """Switch between 'draw' (pencil) and 'select' (lasso) mode."""
        self._edit_mode = mode
        self._drag_mode = self._MODE_NONE
        self.setCursor(Qt.CrossCursor if mode == "draw" else Qt.ArrowCursor)

    def set_view_x(self, vx: float) -> None:
        self._view_x = max(0.0, vx)
        self.update()

    def set_active_clip(self, clip: Optional[MidiClip]) -> None:
        """
        Set the MidiClip whose notes the piano roll edits.

        When a clip is set the piano roll shows only that clip's notes at
        their relative positions (beat 0 = clip start).  Pass None to return
        to the legacy flat-track display.
        """
        self._active_clip = clip
        self._selected_ids.clear()
        self._drag_mode = self._MODE_NONE
        if clip is not None:
            # Always start at beat 0 of the clip regardless of arrangement scroll
            self._view_x = 0.0
            # Scroll vertically to the note range (or middle C if the clip is empty)
            if clip.notes:
                avg = sum(n.pitch for n in clip.notes) / len(clip.notes)
                self.jump_to_pitch(int(avg))
            else:
                self.jump_to_pitch(60)
        self.update()

    def jump_to_pitch(self, pitch: int) -> None:
        """
        Scroll the piano roll so *pitch* appears near the top of the viewport.

        view_y = 0 → topmost visible row is pitch 127 (G9).
        Increasing view_y shifts the view downward (toward lower notes).
        We place the target pitch 4 rows below the top edge for context.
        """
        target_top = pitch + 4
        self._view_y = max(0, self.PITCH_MAX - 1 - target_top)
        self.view_y_changed.emit(self._view_y)
        self.update()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        # Always call p.end() — even if a sub-draw raises — so the backing
        # store is never left with an active painter, which would make the
        # widget fail to accept mouse events on the next repaint cycle.
        try:
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            gw = w - self.PIANO_WIDTH
            gh = h - self.HEADER_HEIGHT
            self._draw_bg(p, w, h)
            self._draw_piano(p, gh)
            self._draw_ruler(p, gw)
            self._draw_grid(p, gw, gh)
            self._draw_notes(p, gw, gh)
            self._draw_playhead(p, gw, gh)
        finally:
            p.end()

    def _draw_bg(self, p: QPainter, w: int, h: int) -> None:
        p.fillRect(0, 0, w, h, QColor(C["void"]))

    def _draw_piano(self, p: QPainter, gh: int) -> None:
        p.save()
        p.translate(0, self.HEADER_HEIGHT)
        num = self.PITCH_MAX - self.PITCH_MIN

        for i in range(num):
            pitch = self.PITCH_MAX - 1 - i - self._view_y
            if not (self.PITCH_MIN <= pitch < self.PITCH_MAX):
                continue
            y = i * self.NOTE_HEIGHT
            if y > gh:
                break
            is_black = pitch % 12 in {1, 3, 6, 8, 10}
            if is_black:
                p.fillRect(0, y, self.PIANO_WIDTH - 2, self.NOTE_HEIGHT - 1,
                           QColor(15, 18, 35))
            else:
                grad = QLinearGradient(0, y, self.PIANO_WIDTH, y)
                grad.setColorAt(0, QColor(30, 40, 65))
                grad.setColorAt(1, QColor(20, 26, 50))
                p.fillRect(0, y, self.PIANO_WIDTH - 2, self.NOTE_HEIGHT - 1,
                           QBrush(grad))

            if pitch % 12 == 0:
                p.setPen(QColor(C["cyan"]))
                p.setFont(QFont("Arial", 7))
                p.drawText(3, y + self.NOTE_HEIGHT - 2, f"C{pitch//12 - 1}")

        p.restore()

    def _draw_ruler(self, p: QPainter, gw: int) -> None:
        p.save()
        p.translate(self.PIANO_WIDTH, 0)
        p.fillRect(0, 0, gw, self.HEADER_HEIGHT, QColor(C["abyss"]))

        # Loop region highlight on ruler
        if self._loop_enabled or self._loop_drag_origin is not None:
            xs = int((self._loop_start - self._view_x) * self.BEAT_WIDTH)
            xe = int((self._loop_end   - self._view_x) * self.BEAT_WIDTH)
            if xs < gw and xe > 0:
                fill_x = max(0, xs)
                fill_w = min(gw, xe) - fill_x
                p.fillRect(fill_x, 0, fill_w, self.HEADER_HEIGHT,
                           QColor(153, 69, 255, 50))
                p.setPen(QPen(QColor(C["purple"]), 2))
                if 0 <= xs <= gw:
                    p.drawLine(xs, 0, xs, self.HEADER_HEIGHT)
                if 0 <= xe <= gw:
                    p.drawLine(xe, 0, xe, self.HEADER_HEIGHT)

        # Glowing bottom border on ruler
        p.setPen(QPen(QColor(0, 229, 255, 60), 1))
        p.drawLine(0, self.HEADER_HEIGHT - 1, gw, self.HEADER_HEIGHT - 1)

        p.setFont(QFont("Arial", 9))
        # Ask the snapper for ruler labels at the correct density and mode.
        ruler_lbls = self._snapper.ruler_labels(
            max(0.0, self._view_x - 2.0),
            self._view_x + gw / self.BEAT_WIDTH + 2.0,
            float(self.BEAT_WIDTH),
        )
        for lbl in ruler_lbls:
            x = int((lbl.beat - self._view_x) * self.BEAT_WIDTH)
            if 0 <= x <= gw:
                if lbl.is_major:
                    p.setPen(QColor(C["cyan"]))
                    p.drawLine(x, 4, x, self.HEADER_HEIGHT - 1)
                    p.drawText(x + 3, self.HEADER_HEIGHT - 5, lbl.text)
                else:
                    p.setPen(QPen(QColor(0, 229, 255, 40), 1))
                    p.drawLine(x, 22, x, self.HEADER_HEIGHT - 1)
                    p.setPen(QColor(C["text_dim"]))
                    p.drawText(x + 3, self.HEADER_HEIGHT - 5, lbl.text)

        # Hint label when no loop is set yet
        if not self._loop_enabled and self._loop_drag_origin is None:
            p.setPen(QColor(C["text_dim"]))
            p.setFont(QFont("Arial", 8))
            p.drawText(4, self.HEADER_HEIGHT - 4, "drag ruler → set loop")

        p.restore()

    def _draw_grid(self, p: QPainter, gw: int, gh: int) -> None:
        p.save()
        p.translate(self.PIANO_WIDTH, self.HEADER_HEIGHT)
        num = self.PITCH_MAX - self.PITCH_MIN

        for i in range(num):
            pitch = self.PITCH_MAX - 1 - i - self._view_y
            if not (self.PITCH_MIN <= pitch < self.PITCH_MAX):
                continue
            y = i * self.NOTE_HEIGHT
            if y > gh:
                break
            is_black = pitch % 12 in {1, 3, 6, 8, 10}
            # Crystal dark rows — black-key rows are slightly more purple
            r = QColor(8, 10, 22) if is_black else QColor(10, 13, 28)
            p.fillRect(0, y, gw, self.NOTE_HEIGHT, r)

            # C-note separator line
            if pitch % 12 == 0:
                p.setPen(QPen(QColor(0, 229, 255, 25), 1))
                p.drawLine(0, y, gw, y)

        # Vertical beat lines — bar lines bright, beat lines medium, sub-grid faint.
        # Uses the GridSnapper to get exact positions for all grid types
        # (straight, triplet, dotted, Free), avoiding floating-point drift.
        bar_beats = float(self._snapper.time_sig())
        lines = self._snapper.grid_lines(
            max(0.0, self._view_x - self._grid_beats),
            self._view_x + gw / self.BEAT_WIDTH + self._grid_beats,
        )
        for b in lines:
            x = int((b - self._view_x) * self.BEAT_WIDTH)
            if 0 <= x <= gw:
                in_bar = b % bar_beats
                if in_bar < 0.001 or abs(in_bar - bar_beats) < 0.001:
                    p.setPen(QPen(QColor(0, 229, 255, 35), 1))   # bar line
                elif abs(in_bar % 1.0) < 0.001:
                    p.setPen(QPen(QColor(0, 229, 255, 18), 1))   # beat line
                else:
                    p.setPen(QPen(QColor(0, 229, 255, 8), 1))    # sub-beat line
                p.drawLine(x, 0, x, gh)

        p.restore()

    def _clip_notes_for_display(self) -> Tuple[List[MidiNote], QColor, bool]:
        """
        Return (notes, base_color, is_clip_mode).

        In clip mode: returns the active clip's relative notes and the track color.
        In legacy mode: returns all tracks' absolute notes.
        """
        if self._active_clip is not None:
            color = self._channel_colors.get(
                self._active_channel, QColor(C["cyan"]))
            return self._active_clip.notes, color, True

        # Legacy: flat list from the active track (only)
        for track in self._tracks:
            if track.channel == self._active_channel:
                color = self._channel_colors.get(track.channel, QColor(C["cyan"]))
                return track.notes, color, False
        return [], QColor(C["cyan"]), False

    def _note_screen_rect(self, note: MidiNote) -> QRectF:
        """
        Return the note's rectangle in the piano-roll coordinate system
        (translated so origin is at PIANO_WIDTH, HEADER_HEIGHT).
        """
        x  = (note.start_beat - self._view_x) * self.BEAT_WIDTH
        nw = max(4.0, note.duration * self.BEAT_WIDTH - 2)
        row = (self.PITCH_MAX - note.pitch - 1) - self._view_y
        y   = row * self.NOTE_HEIGHT
        return QRectF(x, y + 1, nw, self.NOTE_HEIGHT - 2)

    def _find_note_at_screen(
        self, sx: int, sy: int
    ) -> Tuple[Optional[MidiNote], str]:
        """
        Hit-test screen position (sx, sy) against rendered notes.

        Returns (note, zone) where zone is 'resize' (right edge) or 'body',
        or (None, '') if no note is under the cursor.
        The coordinates are relative to the note-grid area (after header/piano offset).
        """
        notes, _, _ = self._clip_notes_for_display()
        for note in reversed(notes):
            r = self._note_screen_rect(note)
            if r.contains(sx, sy):
                zone = ('resize'
                        if sx >= r.right() - self.RESIZE_GRIP
                        else 'body')
                return note, zone
        return None, ''

    def _draw_notes(self, p: QPainter, gw: int, gh: int) -> None:
        p.save()
        p.translate(self.PIANO_WIDTH, self.HEADER_HEIGHT)

        notes, color, is_clip_mode = self._clip_notes_for_display()

        # When in move mode, compute preview offsets so notes follow the mouse
        delta_beat  = 0.0
        delta_pitch = 0
        if self._drag_mode == self._MODE_MOVE and self._move_notes_state:
            delta_beat  = self._move_origin_beat  - 0.0   # computed in mouseMoveEvent
            delta_pitch = self._move_origin_pitch - 0
            # Actual deltas are stored in _move_origin_* as the CURRENT position
            # (we overwrite them in mouseMoveEvent as the preview offsets)
            delta_beat  = getattr(self, '_move_delta_beat',  0.0)
            delta_pitch = getattr(self, '_move_delta_pitch', 0)

        # Ghost notes from other tracks in composition mode (legacy path)
        if self._composition_mode and not is_clip_mode:
            for track in self._tracks:
                if track.channel == self._active_channel:
                    continue
                ghost_col = self._channel_colors.get(
                    track.channel, QColor(C["cyan"]))
                for note in track.notes:
                    r = self._note_screen_rect(note)
                    if r.right() < 0 or r.x() > gw:
                        continue
                    p.setPen(QPen(
                        QColor(ghost_col.red(), ghost_col.green(),
                               ghost_col.blue(), 50), 1))
                    p.setBrush(
                        QColor(ghost_col.red(), ghost_col.green(),
                               ghost_col.blue(), 15))
                    p.drawRoundedRect(r, 2, 2)

        # Active notes
        for note in notes:
            is_selected = note.note_id in self._selected_ids
            is_moving   = (self._drag_mode == self._MODE_MOVE
                           and note.note_id in self._move_notes_state)

            # Compute display position (with live move preview)
            disp_note_beat  = note.start_beat
            disp_note_pitch = note.pitch
            disp_duration   = note.duration

            if is_moving:
                disp_note_beat  = max(0.0, note.start_beat + delta_beat)
                disp_note_pitch = max(0, min(127, note.pitch + delta_pitch))
            elif (self._drag_mode == self._MODE_RESIZE
                  and note.note_id == self._resize_note_id):
                disp_duration = max(0.0625, getattr(self, '_resize_preview_dur',
                                                    note.duration))

            x  = (disp_note_beat - self._view_x) * self.BEAT_WIDTH
            nw = max(4.0, disp_duration * self.BEAT_WIDTH - 2)
            row = (self.PITCH_MAX - disp_note_pitch - 1) - self._view_y
            y   = row * self.NOTE_HEIGHT

            if x + nw < 0 or x > gw or y + self.NOTE_HEIGHT < 0 or y > gh:
                continue

            rect = QRectF(x, y + 1, nw, self.NOTE_HEIGHT - 2)

            if is_selected or is_moving:
                # Selection highlight: white inner border + brighter body
                grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                grad.setColorAt(0, color.lighter(220))
                grad.setColorAt(0.5, color.lighter(150))
                grad.setColorAt(1, color)
                p.fillRect(rect, QBrush(grad))
                p.setPen(QPen(QColor(255, 255, 255, 220), 1.5))
                p.drawRoundedRect(rect, 2, 2)
            else:
                grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                grad.setColorAt(0, color.lighter(170))
                grad.setColorAt(0.5, color)
                grad.setColorAt(1, color.darker(140))
                p.fillRect(rect, QBrush(grad))
                p.setPen(QPen(color.lighter(200), 1))
                p.drawRoundedRect(rect, 2, 2)

                glow = QRectF(x + 1, y + 2, max(2.0, nw - 2), 2)
                p.fillRect(glow, QColor(255, 255, 255, 60))

            # Resize grip indicator: a thin bright line on the right edge
            if nw > self.RESIZE_GRIP + 2:
                p.setPen(QPen(QColor(255, 255, 255, 100), 2))
                p.drawLine(QPointF(x + nw - 1, y + 2),
                           QPointF(x + nw - 1, y + self.NOTE_HEIGHT - 3))

        # Lasso rectangle
        if self._drag_mode == self._MODE_LASSO and self._lasso_start and self._lasso_end:
            lx = min(self._lasso_start.x(), self._lasso_end.x()) - self.PIANO_WIDTH
            ly = min(self._lasso_start.y(), self._lasso_end.y()) - self.HEADER_HEIGHT
            lw = abs(self._lasso_end.x() - self._lasso_start.x())
            lh = abs(self._lasso_end.y() - self._lasso_start.y())
            p.setPen(QPen(QColor(0, 229, 255, 200), 1, Qt.DashLine))
            p.setBrush(QColor(0, 229, 255, 20))
            p.drawRect(QRectF(lx, ly, lw, lh))

        p.restore()

    def _draw_playhead(self, p: QPainter, gw: int, gh: int) -> None:
        """
        Draw the real-time playhead as a bright neon vertical line.

        The line spans the full note-grid height (below the ruler/header).
        A small downward-pointing triangle cap sits on the ruler at the top
        to make the position obvious at a glance.

        Coordinates are in the translated frame set up by paintEvent
        (origin at (PIANO_WIDTH, 0) for the ruler, shifted again to
        HEADER_HEIGHT for the grid — see caller).
        """
        # Convert beat position to pixel X in the grid coordinate system.
        # _view_x is the leftmost visible beat; BEAT_WIDTH is pixels/beat.
        x = int((self._playhead_beat - self._view_x) * self.BEAT_WIDTH)

        # Only draw when the playhead is within the visible area.
        if x < 0 or x > gw:
            return

        p.save()
        p.translate(self.PIANO_WIDTH, 0)

        # ── Neon glow: wide semi-transparent band behind the main line ──
        glow_pen = QPen(QColor(0, 229, 255, 40), 5)
        p.setPen(glow_pen)
        p.drawLine(x, 0, x, self.HEADER_HEIGHT + gh)

        # ── Main 1-px cyan line spanning ruler + note grid ──
        p.setPen(QPen(QColor(0, 229, 255, 220), 1))
        p.drawLine(x, 0, x, self.HEADER_HEIGHT + gh)

        # ── Triangle cap on the ruler top edge ──
        # PySide6 drawPolygon requires a QPolygonF; splatting a plain list raises TypeError.
        p.setBrush(QColor(0, 229, 255, 200))
        p.setPen(Qt.NoPen)
        tri = QPolygonF([
            QPointF(x - 5, 0.0),
            QPointF(x + 5, 0.0),
            QPointF(float(x), 10.0),
        ])
        p.drawPolygon(tri)

        p.restore()

    # ------------------------------------------------------------------
    # Mouse interaction
    # ------------------------------------------------------------------

    def _to_beat_pitch(self, x: int, y: int) -> Tuple[float, int]:
        beat  = (x - self.PIANO_WIDTH) / self.BEAT_WIDTH + self._view_x
        pitch = (self.PITCH_MAX - 1
                 - int((y - self.HEADER_HEIGHT) / self.NOTE_HEIGHT)
                 - self._view_y)
        return beat, pitch

    # ── Internal helpers ──────────────────────────────────────────────────

    def _grid_coords(self, sx: int, sy: int) -> Tuple[float, int]:
        """
        Convert screen position (sx, sy) to (beat, pitch) grid coordinates.

        Beat is relative to view_x; pitch is the MIDI note number.
        """
        beat  = (sx - self.PIANO_WIDTH) / self.BEAT_WIDTH + self._view_x
        pitch = (self.PITCH_MAX - 1
                 - int((sy - self.HEADER_HEIGHT) / self.NOTE_HEIGHT)
                 - self._view_y)
        return beat, pitch

    def set_grid_size(self, label: str) -> None:
        """Set the active snap grid by label ('1/16', '1/8T', 'Free', …)."""
        self._snapper.set_grid(label)
        self._grid_beats = self._snapper.grid_beats()
        self.update()

    def set_ruler_mode(self, mode: str) -> None:
        """Set the ruler display mode ('BarsBeats', 'Time', 'SMPTE')."""
        self._snapper.set_ruler_mode(mode)
        self.update()

    def set_ruler_fps(self, fps: float) -> None:
        """Set SMPTE frame rate for the ruler."""
        self._snapper.set_fps(fps)
        self.update()

    def _snap(self, beat: float) -> float:
        return self._snapper.snap(beat)

    def _notes_in_lasso(self) -> set:
        """Return the set of note_ids whose rects overlap the current lasso rect."""
        if not (self._lasso_start and self._lasso_end):
            return set()
        lx1 = min(self._lasso_start.x(), self._lasso_end.x()) - self.PIANO_WIDTH
        ly1 = min(self._lasso_start.y(), self._lasso_end.y()) - self.HEADER_HEIGHT
        lx2 = max(self._lasso_start.x(), self._lasso_end.x()) - self.PIANO_WIDTH
        ly2 = max(self._lasso_start.y(), self._lasso_end.y()) - self.HEADER_HEIGHT
        lasso = QRectF(lx1, ly1, lx2 - lx1, ly2 - ly1)

        notes, _, _ = self._clip_notes_for_display()
        ids = set()
        for note in notes:
            r = self._note_screen_rect(note)
            if lasso.intersects(r):
                ids.add(note.note_id)
        return ids

    # ── Mouse events ──────────────────────────────────────────────────────

    def mousePressEvent(self, ev) -> None:
        # ── Ruler area: click = seek, drag = loop region ─────────────────
        if ev.y() < self.HEADER_HEIGHT and ev.x() > self.PIANO_WIDTH:
            if ev.button() == Qt.LeftButton:
                beat = (ev.x() - self.PIANO_WIDTH) / self.BEAT_WIDTH + self._view_x
                self._ruler_pressed    = True
                self._ruler_press_x    = ev.x()
                self._ruler_press_beat = max(0.0, round(beat * 4) / 4)
                self._loop_drag_origin = None
                self.update()
            return

        # ── Piano keyboard area: preview pitch ────────────────────────────
        if ev.x() < self.PIANO_WIDTH:
            return

        # Translate screen coords to note-grid coords
        gx = ev.x() - self.PIANO_WIDTH
        gy = ev.y() - self.HEADER_HEIGHT
        beat, pitch = self._grid_coords(ev.x(), ev.y())

        # FIX: Force alignment for the very first interaction[cite: 9]
        if self._active_clip is not None:
            # Ensure we can't place notes at negative relative positions
            beat = max(0.0, beat)

        beat = self._snap(beat)
        pitch = max(self.PITCH_MIN, min(self.PITCH_MAX - 1, pitch))

        if ev.button() == Qt.RightButton:
            # Right-click on a note → delete it
            note, _ = self._find_note_at_screen(gx, gy)
            if note:
                self.note_removed.emit(self._active_channel, note.note_id)
            return

        if ev.button() == Qt.LeftButton:
            # Track where the user clicked (used as paste target for Cmd+V).
            self._last_click_beat = beat
            note, zone = self._find_note_at_screen(gx, gy)

            if note and zone == 'resize':
                # ── Resize mode ──────────────────────────────────────────
                self._drag_mode          = self._MODE_RESIZE
                self._resize_note_id     = note.note_id
                self._resize_orig_duration = note.duration
                self._resize_preview_dur = note.duration

            elif note and zone == 'body':
                # ── Move mode ────────────────────────────────────────────
                # If note is not already selected, clear selection and select it
                if note.note_id not in self._selected_ids:
                    self._selected_ids = {note.note_id}

                self._drag_mode          = self._MODE_MOVE
                self._move_origin_beat   = beat
                self._move_origin_pitch  = pitch
                self._move_delta_beat    = 0.0
                self._move_delta_pitch   = 0

                # Snapshot original positions of all notes being moved
                notes, _, _ = self._clip_notes_for_display()
                self._move_notes_state = {
                    n.note_id: (n.start_beat, n.pitch)
                    for n in notes
                    if n.note_id in self._selected_ids
                }

            elif ev.modifiers() & Qt.ShiftModifier or self._edit_mode == "select":
                # ── Lasso mode (Shift held, or SELECT mode active) ────────
                self._drag_mode    = self._MODE_LASSO
                self._lasso_start  = QPointF(ev.x(), ev.y())
                self._lasso_end    = QPointF(ev.x(), ev.y())

            else:
                # ── Draw mode (empty area, no Shift, DRAW mode active) ────
                self._selected_ids.clear()
                self._drag_mode    = self._MODE_DRAW
                self._draw_start   = (beat, pitch)

        self.update()

    def mouseMoveEvent(self, ev) -> None:
        # ── Ruler: start loop drag once mouse moves > 4 px ───────────────
        if self._ruler_pressed and abs(ev.x() - self._ruler_press_x) > 4:
            if self._loop_drag_origin is None:
                self._loop_drag_origin = self._ruler_press_beat
                self._loop_start = self._loop_drag_origin
                self._loop_end   = self._loop_drag_origin + 0.25

        # ── Ruler drag (loop region) ──────────────────────────────────────
        if self._loop_drag_origin is not None:
            beat = (ev.x() - self.PIANO_WIDTH) / self.BEAT_WIDTH + self._view_x
            beat = max(0.0, round(beat * 4) / 4)
            origin = self._loop_drag_origin
            self._loop_start = min(origin, beat)
            self._loop_end   = max(origin + 0.25, beat)
            self.update()
            return

        if self._drag_mode == self._MODE_RESIZE:
            # Resize: compute new duration from cursor's beat position
            cur_beat = self._snap(
                (ev.x() - self.PIANO_WIDTH) / self.BEAT_WIDTH + self._view_x)
            notes, _, _ = self._clip_notes_for_display()
            note = next((n for n in notes if n.note_id == self._resize_note_id),
                        None)
            if note:
                new_dur = max(0.25, cur_beat - note.start_beat)
                self._resize_preview_dur = new_dur
            self.update()

        elif self._drag_mode == self._MODE_MOVE:
            # Move: compute beat/pitch delta relative to drag origin
            cur_beat, cur_pitch = self._grid_coords(ev.x(), ev.y())
            cur_beat  = self._snap(cur_beat)
            cur_pitch = max(self.PITCH_MIN, min(self.PITCH_MAX - 1, cur_pitch))
            self._move_delta_beat  = cur_beat  - self._move_origin_beat
            self._move_delta_pitch = cur_pitch - self._move_origin_pitch
            # Clamp so no note goes below beat 0
            if self._move_notes_state:
                min_beat = min(b for b, _ in self._move_notes_state.values())
                self._move_delta_beat = max(-min_beat, self._move_delta_beat)
            self.update()

        elif self._drag_mode == self._MODE_LASSO:
            self._lasso_end = QPointF(ev.x(), ev.y())
            self.update()

        elif self._drag_mode == self._MODE_DRAW and self._draw_start:
            self.update()   # live note-length preview not implemented; refresh anyway

        # Update cursor shape based on what is under the mouse
        gx = ev.x() - self.PIANO_WIDTH
        gy = ev.y() - self.HEADER_HEIGHT
        if self._drag_mode == self._MODE_NONE and ev.x() > self.PIANO_WIDTH:
            _, zone = self._find_note_at_screen(gx, gy)
            if zone == 'resize':
                self.setCursor(Qt.SizeHorCursor)
            elif zone == 'body':
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.CrossCursor)

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return

        # ── Finalize ruler interaction ────────────────────────────────────
        if self._ruler_pressed:
            if self._loop_drag_origin is not None:
                if self._loop_end - self._loop_start >= 0.25:
                    self.loop_region_changed.emit(self._loop_start, self._loop_end)
                self._loop_drag_origin = None
            else:
                self.seek_requested.emit(self._ruler_press_beat)
            self._ruler_pressed = False
            self.update()
            return

        # ── Finalize note draw ────────────────────────────────────────────
        if self._drag_mode == self._MODE_DRAW and self._draw_start:
            sb, pitch = self._draw_start
            eb, _     = self._grid_coords(ev.x(), ev.y())
            eb  = self._snap(eb)
            grid = getattr(self, "_grid_beats", 0.25)
            dur = max(grid, eb - sb) if eb > sb else grid
            if self.PITCH_MIN <= pitch < self.PITCH_MAX:
                self.note_added.emit(self._active_channel, sb, dur, pitch)
            self._draw_start = None

        # ── Finalize resize ───────────────────────────────────────────────
        elif self._drag_mode == self._MODE_RESIZE:
            new_dur = getattr(self, '_resize_preview_dur', None)
            if new_dur is not None and new_dur > 0:
                self.note_resized.emit(self._active_channel,
                                       self._resize_note_id, new_dur)
            self._resize_note_id = -1

        # ── Finalize move ─────────────────────────────────────────────────
        elif self._drag_mode == self._MODE_MOVE and self._move_notes_state:
            db = getattr(self, '_move_delta_beat',  0.0)
            dp = getattr(self, '_move_delta_pitch', 0)
            for nid, (orig_beat, orig_pitch) in self._move_notes_state.items():
                new_beat  = max(0.0, self._snap(orig_beat  + db))
                new_pitch = max(self.PITCH_MIN, min(self.PITCH_MAX - 1,
                                                    orig_pitch + dp))
                self.note_moved.emit(self._active_channel, nid, new_beat, new_pitch)
            self._move_notes_state.clear()
            self._move_delta_beat  = 0.0
            self._move_delta_pitch = 0

        # ── Finalize lasso ────────────────────────────────────────────────
        elif self._drag_mode == self._MODE_LASSO:
            self._selected_ids = self._notes_in_lasso()
            self._lasso_start = self._lasso_end = None

        self._drag_mode = self._MODE_NONE
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def wheelEvent(self, ev) -> None:
        dx, dy = ev.angleDelta().x(), ev.angleDelta().y()
        if abs(dx) > abs(dy):
            # Horizontal scroll → move beat timeline left/right
            self._view_x = max(0.0, self._view_x - dx / 120)
            self.scroll_x_changed.emit(self._view_x)
        else:
            # Vertical scroll → navigate pitches.
            #
            # Root cause of the previous "scroll up broken" bug:
            #   Python's // is floor-division: -5 // 120 == -1 but 5 // 120 == 0.
            #   That asymmetry caused downward scrolls (negative dy) to always
            #   register at least one step while upward scrolls (positive dy)
            #   did nothing unless dy >= 120.
            #
            # Fix: accumulate the raw delta and consume whole steps symmetrically.
            #   int(x) truncates toward zero, so both directions behave the same.
            self._vy_accum += dy
            steps = int(self._vy_accum / 120)       # truncate, not floor
            self._vy_accum -= steps * 120            # keep the remainder

            if steps != 0:
                gh = max(1, self.height() - self.HEADER_HEIGHT)
                visible = gh // self.NOTE_HEIGHT
                max_vy = max(0, self.PITCH_MAX - self.PITCH_MIN - visible)
                # Positive dy / positive steps → scroll UP → lower view_y → higher notes
                self._view_y = max(0, min(max_vy, self._view_y - steps))
                self.view_y_changed.emit(self._view_y)
        self.update()


# ═══════════════════════════════════════════════════════════════════════════
# Velocity Lane
# ═══════════════════════════════════════════════════════════════════════════

class VelocityLaneWidget(QWidget):
    """
    Velocity editor panel displayed below the piano roll grid.

    Each MIDI note is represented by a vertical bar whose height is
    proportional to its velocity (1–127).  Click or drag on a bar to
    change the velocity live.  The lane shares the same horizontal beat
    scale and view_x as PianoRollWidget and scrolls in sync with it.

    Layout:
        Left stub (PIANO_WIDTH px) — dark label area showing "VEL"
        Right canvas              — gradient bars on a dark grid background
    """

    velocity_changed = Signal(int, int, int)   # channel, note_id, new_velocity

    BEAT_WIDTH:  int = PianoRollWidget.BEAT_WIDTH
    PIANO_WIDTH: int = PianoRollWidget.PIANO_WIDTH

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._notes:        List[MidiNote] = []
        self._color:        QColor         = QColor(C["cyan"])
        self._channel:      int            = 0
        self._view_x:       float          = 0.0
        self._selected_ids: set            = set()

        self._drag_note_id:  int = -1
        self._hover_note_id: int = -1

        self.setMouseTracking(True)
        self.setStyleSheet(f"background:{C['void']};")

    # ── Public API ────────────────────────────────────────────────────────

    def set_notes_data(
        self,
        notes:   List[MidiNote],
        color:   QColor,
        channel: int,
    ) -> None:
        self._notes   = list(notes)
        self._color   = color
        self._channel = channel
        self.update()

    def set_view_x(self, vx: float) -> None:
        self._view_x = max(0.0, vx)
        self.update()

    def set_selected_ids(self, ids: set) -> None:
        self._selected_ids = set(ids)
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        inner_h = max(1, h - 1)   # 1 px top border reserved

        # Left label stub
        p.fillRect(0, 0, self.PIANO_WIDTH, h, QColor(C["abyss"]))
        p.setPen(QColor(C["text_dim"]))
        p.setFont(QFont("Arial", 8, QFont.Bold))
        p.drawText(QRectF(0, 0, self.PIANO_WIDTH, h), Qt.AlignCenter, "VEL")
        p.setPen(QPen(QColor(0, 229, 255, 30), 1))
        p.drawLine(self.PIANO_WIDTH - 1, 0, self.PIANO_WIDTH - 1, h)

        # Grid background
        gw = w - self.PIANO_WIDTH
        p.fillRect(self.PIANO_WIDTH, 0, gw, h, QColor(8, 10, 22))

        # Top border glow
        p.setPen(QPen(QColor(0, 229, 255, 55), 1))
        p.drawLine(0, 0, w, 0)

        # Reference lines at velocity 127, 100, 64
        for ref_vel, alpha in ((127, 38), (100, 24), (64, 16)):
            ry = int(inner_h - ref_vel / 127.0 * inner_h)
            p.setPen(QPen(QColor(0, 229, 255, alpha), 1, Qt.DotLine))
            p.drawLine(self.PIANO_WIDTH, ry, w, ry)
        p.setFont(QFont("Arial", 7))
        for ref_vel in (127, 100, 64):
            ry = int(inner_h - ref_vel / 127.0 * inner_h)
            p.setPen(QColor(C["text_dim"]))
            p.drawText(
                QRectF(self.PIANO_WIDTH + 3, ry - 9, 28, 9),
                Qt.AlignLeft | Qt.AlignVCenter, str(ref_vel),
            )

        # ── Velocity bars ─────────────────────────────────────────────
        layout = self._compute_bar_layout()

        # Notes that share a start_beat with another note need pitch labels
        beat_count: dict = {}
        for note in self._notes:
            key = round(note.start_beat, 4)
            beat_count[key] = beat_count.get(key, 0) + 1
        chord_ids = {
            n.note_id for n in self._notes
            if beat_count.get(round(n.start_beat, 4), 0) > 1
        }

        for note in self._notes:
            if note.note_id not in layout:
                continue
            bar_left, bar_w = layout[note.note_id]

            if bar_left + bar_w < self.PIANO_WIDTH or bar_left > w:
                continue

            bar_h   = max(2, int(note.velocity / 127.0 * inner_h))
            bar_top = inner_h - bar_h

            is_selected = note.note_id in self._selected_ids
            is_dragged  = note.note_id == self._drag_note_id
            is_hovered  = note.note_id == self._hover_note_id

            grad = QLinearGradient(bar_left, bar_top, bar_left, inner_h)
            if is_selected or is_dragged:
                grad.setColorAt(0.0, self._color.lighter(220))
                grad.setColorAt(0.5, self._color.lighter(150))
                grad.setColorAt(1.0, self._color)
            elif is_hovered:
                grad.setColorAt(0.0, self._color.lighter(180))
                grad.setColorAt(1.0, self._color.darker(110))
            else:
                grad.setColorAt(0.0, self._color.lighter(140))
                grad.setColorAt(1.0, self._color.darker(140))
            p.fillRect(QRectF(bar_left, bar_top, bar_w, bar_h), QBrush(grad))

            # Bright cap on top of bar
            cap_alpha = 160 if (is_selected or is_dragged) else 80
            p.fillRect(QRectF(bar_left, bar_top, bar_w, 2),
                       QColor(255, 255, 255, cap_alpha))

            # Pitch label above bar for chord notes
            if note.note_id in chord_ids:
                p.setPen(QColor(C["text_dim"]))
                p.setFont(QFont("Arial", 7))
                pitch_offset = 24.0 if (is_hovered or is_dragged) else 11.0
                pitch_y = max(1.0, bar_top - pitch_offset)
                p.drawText(
                    QRectF(bar_left, pitch_y, bar_w + 8, 10),
                    Qt.AlignLeft, self._pitch_name(note.pitch),
                )

            # Velocity value label when hovered or dragged
            if is_hovered or is_dragged:
                p.setPen(QColor(C["text"]))
                p.setFont(QFont("Arial", 8, QFont.Bold))
                label_y = max(1.0, bar_top - 13.0)
                p.drawText(
                    QRectF(bar_left, label_y, 36.0, 12.0),
                    Qt.AlignLeft, str(note.velocity),
                )

        p.end()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _compute_bar_layout(self) -> dict:
        """
        Returns {note_id: (bar_left, bar_w)}.
        Notes sharing a start_beat are spread side-by-side within the
        space of the longest-duration note in that group.
        """
        GAP = 1.0
        groups: dict = {}
        for note in self._notes:
            key = round(note.start_beat, 4)
            if key not in groups:
                groups[key] = []
            groups[key].append(note)

        layout = {}
        for group in groups.values():
            n = len(group)
            ref = group[0]
            base_left = ((ref.start_beat - self._view_x) * self.BEAT_WIDTH
                         + self.PIANO_WIDTH)
            if n == 1:
                layout[ref.note_id] = (
                    base_left,
                    max(4.0, ref.duration * self.BEAT_WIDTH - 3),
                )
            else:
                max_dur = max(note.duration for note in group)
                natural_w = max(4.0, max_dur * self.BEAT_WIDTH - 3)
                sub_w = max(4.0, (natural_w - (n - 1) * GAP) / n)
                for i, note in enumerate(group):
                    layout[note.note_id] = (base_left + i * (sub_w + GAP), sub_w)
        return layout

    @staticmethod
    def _pitch_name(pitch: int) -> str:
        names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
        return f"{names[pitch % 12]}{pitch // 12 - 1}"

    def _note_at_x(self, screen_x: int) -> Optional[MidiNote]:
        """Return the note whose velocity bar contains screen_x."""
        layout = self._compute_bar_layout()
        for note in reversed(self._notes):
            if note.note_id not in layout:
                continue
            bar_left, bar_w = layout[note.note_id]
            if bar_left <= screen_x <= bar_left + bar_w:
                return note
        return None

    def _vel_from_y(self, y: int) -> int:
        inner_h = max(1, self.height() - 1)
        return max(1, min(127, int((1.0 - y / inner_h) * 127)))

    # ── Mouse events ──────────────────────────────────────────────────────

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton or ev.x() < self.PIANO_WIDTH:
            return
        note = self._note_at_x(ev.x())
        if note:
            self._drag_note_id = note.note_id
            new_vel = self._vel_from_y(ev.y())
            note.velocity = new_vel
            self.velocity_changed.emit(self._channel, note.note_id, new_vel)
            self.update()

    def mouseMoveEvent(self, ev) -> None:
        if self._drag_note_id != -1:
            note = next(
                (n for n in self._notes if n.note_id == self._drag_note_id), None
            )
            if note:
                new_vel = self._vel_from_y(ev.y())
                note.velocity = new_vel
                self.velocity_changed.emit(self._channel, note.note_id, new_vel)
                self.update()
        else:
            note = self._note_at_x(ev.x())
            new_hov = note.note_id if note else -1
            if new_hov != self._hover_note_id:
                self._hover_note_id = new_hov
                self.update()
            self.setCursor(Qt.SizeVerCursor if note else Qt.ArrowCursor)

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton and self._drag_note_id != -1:
            self._drag_note_id = -1
            self.update()


# ═══════════════════════════════════════════════════════════════════════════
# Audio Clip Lane
# ═══════════════════════════════════════════════════════════════════════════

class AudioClipLane(QWidget):
    """
    Horizontal timeline lane for placing audio files (vocals, samples, etc.).

    Layout:
        Left stub (52 px) — labelled "AUDIO", aligns with the piano keyboard.
        Ruler strip       — bar numbers, same beat scale as PianoRollWidget.
        Clip area         — coloured blocks for each placed audio file.

    Interaction:
        Left-click on empty clip area  → open file dialog, place clip at that beat.
        Right-click on an existing clip → remove it.
        Scroll wheel                   → sync horizontal scroll with piano roll.
    """

    clip_added     = Signal(str, float, float)   # path, start_beat, duration_secs
    clip_removed   = Signal(int)                  # clip_id
    scroll_changed = Signal(float)               # view_x

    BEAT_WIDTH:  int = 80   # must match PianoRollWidget.BEAT_WIDTH
    PIANO_WIDTH: int = 52   # must match PianoRollWidget.PIANO_WIDTH
    RULER_H:     int = 22
    CLIP_H:      int = 56   # height of the clip drawing area

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._clips: List[AudioClip] = []
        self._view_x: float  = 0.0
        self._bpm:    float  = 120.0
        self._total_beats: float = 64.0
        self._loop_enabled: bool  = False
        self._loop_start:   float = 0.0
        self._loop_end:     float = 8.0

        self.setFixedHeight(self.RULER_H + self.CLIP_H + 4)
        self.setMouseTracking(True)
        self.setStyleSheet(f"background:{C['abyss']};")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_clips(self, clips: List[AudioClip]) -> None:
        self._clips = list(clips)
        self.update()

    def set_view_x(self, x: float) -> None:
        self._view_x = max(0.0, x)
        self.update()

    def set_bpm(self, bpm: float) -> None:
        self._bpm = max(20.0, bpm)
        self.update()

    def set_loop_region(self, enabled: bool, start: float, end: float) -> None:
        self._loop_enabled = enabled
        self._loop_start   = start
        self._loop_end     = end
        self.update()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        gw = w - self.PIANO_WIDTH

        p.fillRect(0, 0, w, h, QColor(C["abyss"]))

        # Piano-width stub
        p.fillRect(0, 0, self.PIANO_WIDTH - 1, h, QColor(C["deep"]))
        p.setPen(QPen(QColor(0, 229, 255, 40), 1))
        p.drawLine(self.PIANO_WIDTH - 1, 0, self.PIANO_WIDTH - 1, h)
        p.setFont(QFont("Arial", 8, QFont.Bold))
        p.setPen(QColor(C["text_dim"]))
        p.drawText(0, 0, self.PIANO_WIDTH - 2, h, Qt.AlignCenter, "AUDIO\nCLIPS")

        p.save()
        p.translate(self.PIANO_WIDTH, 0)

        # ── Ruler ───────────────────────────────────────────────────────
        p.fillRect(0, 0, gw, self.RULER_H, QColor(C["deep"]))
        p.setPen(QPen(QColor(0, 229, 255, 25), 1))
        p.drawLine(0, self.RULER_H - 1, gw, self.RULER_H - 1)

        p.setFont(QFont("Arial", 8))
        beat = 0
        while beat <= self._total_beats:
            x = int((beat - self._view_x) * self.BEAT_WIDTH)
            if 0 <= x <= gw:
                if beat % 4 == 0:
                    p.setPen(QColor(C["text_dim"]))
                    p.drawText(x + 2, self.RULER_H - 3, f"B{int(beat // 4) + 1}")
                p.setPen(QPen(QColor(0, 229, 255, 20), 1))
                p.drawLine(x, 0, x, self.RULER_H)
            beat += 4

        # ── Loop region highlight ────────────────────────────────────────
        if self._loop_enabled:
            xs = int((self._loop_start - self._view_x) * self.BEAT_WIDTH)
            xe = int((self._loop_end   - self._view_x) * self.BEAT_WIDTH)
            if xs < gw and xe > 0:
                fill_x = max(0, xs)
                fill_w = min(gw, xe) - fill_x
                p.fillRect(fill_x, 0, fill_w, h, QColor(153, 69, 255, 22))
                p.setPen(QPen(QColor(C["purple"]), 1))
                if 0 <= xs <= gw:
                    p.drawLine(xs, 0, xs, h)
                if 0 <= xe <= gw:
                    p.drawLine(xe, 0, xe, h)

        # ── Clip blocks ─────────────────────────────────────────────────
        clip_y     = self.RULER_H + 2
        clip_h     = self.CLIP_H - 4
        secs_per_beat = 60.0 / self._bpm

        for clip in self._clips:
            x = (clip.start_beat - self._view_x) * self.BEAT_WIDTH
            if clip.duration_seconds > 0:
                dur_beats = clip.duration_seconds / secs_per_beat
            else:
                dur_beats = 4.0  # default visual width: one bar
            cw = dur_beats * self.BEAT_WIDTH

            if x + cw < 0 or x > gw:
                continue

            color = QColor(clip.color)
            rect  = QRectF(x, clip_y, max(8.0, cw - 2), clip_h)

            grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            grad.setColorAt(0, QColor(color.red(), color.green(), color.blue(), 180))
            grad.setColorAt(1, QColor(color.red(), color.green(), color.blue(), 80))
            p.fillRect(rect, QBrush(grad))

            # Waveform-like decoration (horizontal stripes)
            p.setPen(QPen(QColor(255, 255, 255, 30), 1))
            for stripe_y in range(int(clip_y) + 4, int(clip_y + clip_h) - 4, 6):
                p.drawLine(int(x) + 2, stripe_y, int(x + cw) - 4, stripe_y)

            p.setPen(QPen(color.lighter(160), 1))
            p.drawRoundedRect(rect, 3, 3)

            # Filename
            p.setPen(QColor(255, 255, 255, 210))
            p.setFont(QFont("Arial", 8, QFont.Bold))
            fname = clip.name if clip.name else clip.path.split("/")[-1]
            if len(fname) > 18:
                fname = fname[:15] + "…"
            p.drawText(
                QRectF(x + 4, clip_y + 2, max(10.0, cw - 8), clip_h - 4),
                Qt.AlignLeft | Qt.AlignVCenter,
                fname,
            )

        # ── Empty-state hint ────────────────────────────────────────────
        if not self._clips:
            p.setPen(QColor(C["text_dim"]))
            p.setFont(QFont("Arial", 9))
            p.drawText(
                QRectF(0, self.RULER_H, gw, self.CLIP_H),
                Qt.AlignCenter,
                "Left-click to place an audio file  ·  Right-click to remove",
            )

        p.restore()

    # ------------------------------------------------------------------
    # Mouse interaction
    # ------------------------------------------------------------------

    def mousePressEvent(self, ev) -> None:
        if ev.x() < self.PIANO_WIDTH:
            return

        beat = (ev.x() - self.PIANO_WIDTH) / self.BEAT_WIDTH + self._view_x
        beat = max(0.0, round(beat * 4) / 4)

        if ev.button() == Qt.RightButton:
            secs_per_beat = 60.0 / self._bpm
            for clip in self._clips:
                x = (clip.start_beat - self._view_x) * self.BEAT_WIDTH
                dur_beats = (clip.duration_seconds / secs_per_beat
                             if clip.duration_seconds > 0 else 4.0)
                cw  = dur_beats * self.BEAT_WIDTH
                abs_x = x + self.PIANO_WIDTH
                if abs_x <= ev.x() <= abs_x + cw and ev.y() >= self.RULER_H:
                    self.clip_removed.emit(clip.clip_id)
                    return

        elif ev.button() == Qt.LeftButton and ev.y() >= self.RULER_H:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Import Audio File",
                os.path.expanduser("~"),
                "Audio Files (*.wav *.mp3 *.ogg *.flac *.aiff *.m4a);;All Files (*)",
            )
            if path:
                dur = self._get_duration(path)
                self.clip_added.emit(path, beat, dur)

    def wheelEvent(self, ev) -> None:
        dx = ev.angleDelta().x()
        dy = ev.angleDelta().y()
        delta = dx if abs(dx) > abs(dy) else -dy
        self._view_x = max(0.0, self._view_x + delta / 120)
        self.scroll_changed.emit(self._view_x)
        self.update()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _get_duration(self, path: str) -> float:
        """Best-effort audio duration in seconds."""
        try:
            import pygame
            sound = pygame.mixer.Sound(path)
            return sound.get_length()
        except Exception:
            pass
        try:
            import wave
            with wave.open(path, "r") as wf:
                return wf.getnframes() / wf.getframerate()
        except Exception:
            return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Mixer Track Strip
# ═══════════════════════════════════════════════════════════════════════════

class _ClickableLabel(QLabel):
    double_clicked = Signal()
    def mouseDoubleClickEvent(self, ev) -> None:
        self.double_clicked.emit()


class MixerStrip(QFrame):
    """
    Crystal-themed vertical channel strip.
    Glowing colour-bar at the top identifies the track at a glance.
    """

    gain_changed   = Signal(int, float)
    pan_changed    = Signal(int, float)
    mute_toggled   = Signal(int, bool)
    solo_toggled   = Signal(int, bool)
    track_selected = Signal(int)
    remove_clicked = Signal(int)
    name_changed   = Signal(int, str)

    def __init__(self, channel: int, name: str, color: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.channel   = channel
        self._building = True
        self._color    = color

        self.setFixedWidth(96)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"""
            MixerStrip {{
                background: {C["abyss"]};
                border: 1px solid rgba(0,229,255,0.1);
                border-radius: 8px;
                margin: 2px;
            }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 5, 5, 6)
        lay.setSpacing(3)

        # Colour glow bar + remove × button
        top = QHBoxLayout()
        bar = QFrame()
        bar.setFixedHeight(5)
        bar.setStyleSheet(
            f"background: {color}; border-radius: 2px;"
            f" border: none;"
        )
        top.addWidget(bar, stretch=1)
        rm = QPushButton("×")
        rm.setFixedSize(16, 16)
        rm.setStyleSheet(
            f"QPushButton {{ background: rgba(255,45,158,0.18); border: none;"
            f" border-radius: 8px; color: {C['pink']}; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {C['pink']}; color: white; }}"
        )
        rm.clicked.connect(lambda: self.remove_clicked.emit(channel))
        top.addWidget(rm)
        lay.addLayout(top)

        # Track name (double-click to rename)
        self._name_label = _ClickableLabel(name[:10])
        self._name_label.setAlignment(Qt.AlignCenter)
        self._name_label.setFont(QFont("Arial", 8, QFont.Bold))
        self._name_label.setStyleSheet(f"color:{color}; background:transparent;")
        self._name_label.setWordWrap(True)
        self._name_label.setToolTip("Double-click to rename")
        self._name_label.double_clicked.connect(self._on_rename)
        lay.addWidget(self._name_label)

        # Instrument subtitle (shows currently loaded preset, separate from track name)
        self._instrument_label = QLabel("")
        self._instrument_label.setAlignment(Qt.AlignCenter)
        self._instrument_label.setStyleSheet(
            f"color:{C['text_dim']}; font-size:8px; background:transparent;")
        self._instrument_label.setWordWrap(True)
        lay.addWidget(self._instrument_label)

        # Channel number
        cl = QLabel(f"CH {channel+1:02d}")
        cl.setAlignment(Qt.AlignCenter)
        cl.setStyleSheet(f"color:{C['text_dim']}; font-size:9px; background:transparent;")
        lay.addWidget(cl)

        # Volume fader
        vol_row = QVBoxLayout()
        vol_row.setSpacing(2)
        lbl_vol = QLabel("VOL")
        lbl_vol.setAlignment(Qt.AlignCenter)
        lbl_vol.setStyleSheet(f"color:{C['text_dim']}; font-size:8px; background:transparent;")
        vol_row.addWidget(lbl_vol)
        self.vol = QSlider(Qt.Vertical)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedHeight(90)
        self.vol.valueChanged.connect(self._on_vol)
        vol_row.addWidget(self.vol, alignment=Qt.AlignCenter)
        lay.addLayout(vol_row)

        # Pan
        lbl_pan = QLabel("PAN")
        lbl_pan.setAlignment(Qt.AlignCenter)
        lbl_pan.setStyleSheet(f"color:{C['text_dim']}; font-size:8px; background:transparent;")
        lay.addWidget(lbl_pan)
        self.pan = QSlider(Qt.Horizontal)
        self.pan.setRange(-50, 50)
        self.pan.setValue(0)
        self.pan.valueChanged.connect(self._on_pan)
        lay.addWidget(self.pan)

        # Mute / Solo
        ms = QHBoxLayout()
        ms.setSpacing(3)
        self.mute_btn = QPushButton("M")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setFixedSize(30, 22)
        self.mute_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; border:1px solid rgba(255,107,43,0.3);"
            f" border-radius:3px; color:{C['text_dim']}; font-size:10px; }}"
            f"QPushButton:checked {{ background:rgba(255,107,43,0.4);"
            f" border-color:{C['orange']}; color:{C['orange']}; }}"
        )
        self.mute_btn.toggled.connect(lambda v: self.mute_toggled.emit(channel, v))
        ms.addWidget(self.mute_btn)

        self.solo_btn = QPushButton("S")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setFixedSize(30, 22)
        self.solo_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; border:1px solid rgba(255,215,0,0.3);"
            f" border-radius:3px; color:{C['text_dim']}; font-size:10px; }}"
            f"QPushButton:checked {{ background:rgba(255,215,0,0.3);"
            f" border-color:{C['gold']}; color:{C['gold']}; }}"
        )
        self.solo_btn.toggled.connect(lambda v: self.solo_toggled.emit(channel, v))
        ms.addWidget(self.solo_btn)
        lay.addLayout(ms)

        # Select button
        self.sel_btn = QPushButton("●")
        self.sel_btn.setCheckable(True)
        self.sel_btn.setFixedHeight(22)
        self.sel_btn.setToolTip("Select — route keyboard/gamepad to this track")
        self.sel_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; border:1px solid rgba(0,229,255,0.2);"
            f" border-radius:3px; color:{C['text_dim']}; font-size:11px; }}"
            f"QPushButton:checked {{ background:rgba(0,229,255,0.2);"
            f" border-color:{C['cyan']}; color:{C['cyan']}; }}"
        )
        self.sel_btn.clicked.connect(lambda: self.track_selected.emit(channel))
        lay.addWidget(self.sel_btn)

        lay.addStretch()
        self._building = False

    # ------------------------------------------------------------------

    def _on_vol(self, v: int) -> None:
        if not self._building:
            self.gain_changed.emit(self.channel, v / 100.0)

    def _on_pan(self, v: int) -> None:
        if not self._building:
            self.pan_changed.emit(self.channel, v / 50.0)

    def set_selected(self, selected: bool) -> None:
        self.sel_btn.setChecked(selected)
        self.setStyleSheet(f"""
            MixerStrip {{
                background: {C['abyss']};
                border: 1px solid {'rgba(0,229,255,0.55)' if selected else 'rgba(0,229,255,0.1)'};
                border-radius: 8px;
                margin: 2px;
            }}
        """)

    def set_name(self, name: str) -> None:
        self._name_label.setText(name[:10])

    def set_instrument_name(self, name: str) -> None:
        """Update the instrument subtitle label (not the track name)."""
        self._instrument_label.setText(name[:12])
        self._instrument_label.setVisible(bool(name))

    def _on_rename(self) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Rename Track", "Track name:",
            text=self._name_label.text(),
        )
        if ok and new_name.strip():
            self.set_name(new_name.strip())
            self.name_changed.emit(self.channel, new_name.strip())


# ═══════════════════════════════════════════════════════════════════════════
# Transport Bar
# ═══════════════════════════════════════════════════════════════════════════

class TransportBar(QWidget):
    play_clicked   = Signal()
    stop_clicked   = Signal()
    record_clicked = Signal(bool)
    bpm_changed    = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background:{C['abyss']};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(8)

        def _tb(text: str, tip: str = "", checkable: bool = False,
                color: str = "") -> QPushButton:
            b = QPushButton(text)
            b.setFixedHeight(34)
            b.setCheckable(checkable)
            b.setToolTip(tip)
            if color:
                b.setStyleSheet(
                    f"QPushButton {{ background:{C['deep']}; color:{color};"
                    f" border:1px solid rgba(0,229,255,0.3); border-radius:5px; }}"
                    f"QPushButton:hover {{ border-color:{color}; }}"
                    f"QPushButton:checked {{ background:rgba(255,45,158,0.3);"
                    f" border-color:{C['pink']}; color:{C['pink']}; }}"
                )
            return b

        self.play_btn   = _tb("▶  PLAY",   "Play from start")
        self.stop_btn   = _tb("■  STOP",   "Stop + rewind")
        self.record_btn = _tb("⏺  REC",    "Record", True, C["orange"])

        self.play_btn  .clicked.connect(self.play_clicked)
        self.stop_btn  .clicked.connect(self.stop_clicked)
        self.record_btn.toggled.connect(self.record_clicked)

        lay.addWidget(self.play_btn)
        lay.addWidget(self.stop_btn)
        lay.addWidget(self.record_btn)

        lay.addSpacing(20)

        # BPM
        lay.addWidget(_label("BPM", C["text_dim"], 10))
        self.bpm_spin = QDoubleSpinBox()
        self.bpm_spin.setRange(20, 300)
        self.bpm_spin.setValue(120)
        self.bpm_spin.setSingleStep(1)
        self.bpm_spin.setFixedWidth(72)
        self.bpm_spin.valueChanged.connect(self.bpm_changed)
        lay.addWidget(self.bpm_spin)

        lay.addStretch()

        # Composition mode toggle
        self.comp_mode_btn = QPushButton("◈  COMP VIEW")
        self.comp_mode_btn.setCheckable(True)
        self.comp_mode_btn.setFixedHeight(34)
        self.comp_mode_btn.setToolTip("Show all tracks simultaneously in piano roll")
        lay.addWidget(self.comp_mode_btn)

        # Loop toggle
        self.loop_btn = QPushButton("⟳  LOOP")
        self.loop_btn.setCheckable(True)
        self.loop_btn.setFixedHeight(34)
        self.loop_btn.setToolTip(
            "Loop playback between markers — drag the ruler in the piano roll to set the region"
        )
        self.loop_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:{C['purple']};"
            f" border:1px solid rgba(153,69,255,0.4); border-radius:5px; }}"
            f"QPushButton:hover {{ border-color:{C['purple']}; }}"
            f"QPushButton:checked {{ background:rgba(153,69,255,0.3);"
            f" border-color:{C['purple']}; color:{C['purple']}; }}"
        )
        lay.addWidget(self.loop_btn)

        lay.addSpacing(12)

        # Scale / Root / Octave
        lay.addWidget(_label("SCALE", C["text_dim"], 10))
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(sorted(SCALES.keys()))
        self.scale_combo.setCurrentText("major")
        self.scale_combo.setFixedWidth(120)
        lay.addWidget(self.scale_combo)

        lay.addWidget(_label("ROOT", C["text_dim"], 10))
        self.root_combo = QComboBox()
        self.root_combo.addItems(
            [f"{n}4" for n in
             ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]]
        )
        self.root_combo.setCurrentText("C4")
        self.root_combo.setFixedWidth(58)
        lay.addWidget(self.root_combo)

        lay.addWidget(_label("OCT", C["text_dim"], 10))
        self.octave_spin = QSpinBox()
        self.octave_spin.setRange(-3, 3)
        self.octave_spin.setValue(0)
        self.octave_spin.setFixedWidth(48)
        lay.addWidget(self.octave_spin)

        lay.addSpacing(12)

        self.panic_btn = QPushButton("⚡ PANIC")
        self.panic_btn.setFixedHeight(34)
        self.panic_btn.setStyleSheet(
            f"QPushButton {{ background:#1A0000; color:{C['orange']};"
            f" border:1px solid rgba(255,107,43,0.5); border-radius:5px; }}"
            f"QPushButton:hover {{ background:rgba(255,107,43,0.3); }}"
        )
        self.panic_btn.setToolTip("All notes off — emergency silence")
        lay.addWidget(self.panic_btn)


# ═══════════════════════════════════════════════════════════════════════════
# Instrument Selector Dialog  (SF2 / SFZ / VST3)
# ═══════════════════════════════════════════════════════════════════════════

class InstrumentSelectorDialog(QDialog):
    """
    Unified instrument selector for existing tracks.  Three pages toggled by
    buttons at the top: SF2 soundfont, SFZ instrument, VST3 plugin.

    result_info() returns one of:
        ("sf2",  sf2_path, bank, preset, preset_name)
        ("sfz",  sfz_path, instrument_name)
        ("vst3", plugin_path, plugin_name)
    Returns None if cancelled.
    """

    # Tab labels and matching accent colours for each instrument source.
    _TAB_LABELS = ["SF2  SOUNDFONT", "SFZ  INSTRUMENT",
                   "VST3  PLUGIN",   "DS  DECENT SAMPLER"]
    _TAB_COLORS = [C["cyan"], C["lime"], C["purple"], C["gold"]]

    def __init__(self, engine: AudioEngine, track_name: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._engine    = engine
        self._result:   Optional[tuple] = None
        self._sfz_path: str = ""
        self._vst3_path: str = ""
        self._ds_path:  str = ""   # Absolute path to the selected .dspreset file.

        self.setWindowTitle(f"Change Instrument  —  {track_name}")
        self.setMinimumSize(680, 540)
        self.setStyleSheet(STYLESHEET)

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(14, 14, 14, 14)

        hdr = _label("CHANGE INSTRUMENT", C["cyan"], 13, True)
        hdr.setStyleSheet(
            f"color:{C['cyan']}; font-size:13px; font-weight:bold;"
            f" letter-spacing:3px; background:transparent;"
        )
        root.addWidget(hdr)

        # ── Tab selector ───────────────────────────────────────────────────
        tab_row = QHBoxLayout()
        tab_row.setSpacing(2)
        self._tab_btns: List[QPushButton] = []
        for i, (lbl, col) in enumerate(zip(self._TAB_LABELS, self._TAB_COLORS)):
            btn = QPushButton(lbl)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _checked, idx=i: self._switch_tab(idx))
            tab_row.addWidget(btn)
            self._tab_btns.append(btn)
        tab_row.addStretch()
        root.addLayout(tab_row)

        # ── Stacked pages ──────────────────────────────────────────────────
        self._stack = QStackedWidget()
        root.addWidget(self._stack, stretch=1)
        self._stack.addWidget(self._build_sf2_page())
        self._stack.addWidget(self._build_sfz_page())
        self._stack.addWidget(self._build_vst3_page())
        # Decent Sampler page — file picker + zone summary table.
        self._stack.addWidget(self._build_ds_page())

        # ── Bottom buttons ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        self._ok_btn = QPushButton("✦  Apply Instrument")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(self._ok_btn)
        root.addLayout(btn_row)

        self._switch_tab(0)
        self._cat_list.setCurrentRow(0)

    # ── Tab switching ──────────────────────────────────────────────────────

    def _switch_tab(self, idx: int) -> None:
        for i, (btn, col) in enumerate(zip(self._tab_btns, self._TAB_COLORS)):
            active = (i == idx)
            btn.setChecked(active)
            if active:
                btn.setStyleSheet(
                    f"QPushButton {{ background:rgba(0,0,0,0.3); color:{col};"
                    f" border:1px solid {col}; border-radius:4px;"
                    f" padding:4px 16px; font-size:10px; font-weight:bold; }}"
                    f"QPushButton:hover {{ background:rgba(0,0,0,0.5); }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background:{C['deep']}; color:{C['text_dim']};"
                    f" border:1px solid rgba(255,255,255,0.1); border-radius:4px;"
                    f" padding:4px 16px; font-size:10px; }}"
                    f"QPushButton:hover {{ background:{C['surface']}; color:{C['text']}; }}"
                )
        self._stack.setCurrentIndex(idx)
        col = self._TAB_COLORS[idx]
        self._ok_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(0,0,0,0.2); border:1px solid {col};"
            f" color:{col}; border-radius:5px; padding:6px 18px; }}"
            f"QPushButton:hover {{ background:rgba(0,0,0,0.4); }}"
        )

    # ── Page builders ──────────────────────────────────────────────────────

    def _build_sf2_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        sf2_row = QHBoxLayout()
        sf2_row.addWidget(_label("SOUNDFONT", C["text_dim"], 10))
        self._sf2_combo = QComboBox()
        self._sf2_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        sf2_files = AudioEngine.get_available_sf2_files()
        for p in sf2_files:
            self._sf2_combo.addItem(os.path.basename(p), userData=p)
        if not sf2_files:
            self._sf2_combo.addItem("— no SF2 found —", userData="")
        sf2_row.addWidget(self._sf2_combo)
        browse_sf2 = QPushButton("Browse…")
        browse_sf2.setFixedWidth(90)
        browse_sf2.clicked.connect(self._browse_sf2)
        sf2_row.addWidget(browse_sf2)
        lay.addLayout(sf2_row)

        sp = QSplitter(Qt.Orientation.Horizontal)
        self._cat_list = QListWidget()
        self._cat_list.setFixedWidth(155)
        for cat in GM_INSTRUMENTS:
            self._cat_list.addItem(QListWidgetItem(cat))
        self._cat_list.currentRowChanged.connect(self._on_cat)
        sp.addWidget(self._cat_list)
        self._pre_list = QListWidget()
        sp.addWidget(self._pre_list)
        sp.setStretchFactor(0, 0)
        sp.setStretchFactor(1, 1)
        lay.addWidget(sp, stretch=1)

        lay.addWidget(_label(
            "PREVIEW  ·  click keys or use A–K / W–P on your keyboard",
            C["text_dim"], 9,
        ))
        self._preview = InstrumentPreviewWidget(self)
        self._preview.set_note_callbacks(
            on_note_on=self._engine.preview_note_on,
            on_note_off=self._engine.preview_note_off,
        )
        lay.addWidget(self._preview)
        self._sf2_combo.currentIndexChanged.connect(self._reload_preview)
        self._pre_list.currentRowChanged.connect(self._reload_preview)
        return page

    def _build_sfz_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        browse_row = QHBoxLayout()
        browse_row.addWidget(_label("SFZ FILE", C["text_dim"], 10))
        self._sfz_path_lbl = QLabel("(no file selected)")
        self._sfz_path_lbl.setStyleSheet(
            f"color:{C['text_dim']}; font-size:10px; background:transparent;")
        self._sfz_path_lbl.setWordWrap(True)
        browse_row.addWidget(self._sfz_path_lbl, stretch=1)
        browse_sfz_btn = QPushButton("Browse SFZ…")
        browse_sfz_btn.setFixedWidth(110)
        browse_sfz_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:{C['lime']};"
            f" border:1px solid rgba(57,255,20,0.4); border-radius:4px;"
            f" padding:4px 10px; font-size:10px; }}"
            f"QPushButton:hover {{ background:rgba(57,255,20,0.1); }}"
        )
        browse_sfz_btn.clicked.connect(self._browse_sfz)
        browse_row.addWidget(browse_sfz_btn)
        lay.addLayout(browse_row)

        self._sfz_keyboard = SfzKeyRangeWidget()
        lay.addWidget(self._sfz_keyboard)

        self._sfz_table = QTableWidget(0, 5)
        self._sfz_table.setHorizontalHeaderLabels(
            ["Key Lo", "Key Hi", "Vel Lo", "Vel Hi", "Sample"])
        self._sfz_table.horizontalHeader().setStretchLastSection(True)
        self._sfz_table.setAlternatingRowColors(True)
        self._sfz_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._sfz_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._sfz_table.setStyleSheet(
            f"QTableWidget {{ background:{C['deep']}; color:{C['text']};"
            f" gridline-color:{C['surface']};"
            f" border:1px solid rgba(57,255,20,0.2); }}"
            f"QHeaderView::section {{ background:{C['abyss']}; color:{C['text_dim']};"
            f" border:none; padding:2px; }}"
            f"QTableWidget::item:alternate {{ background:{C['abyss']}; }}"
        )
        lay.addWidget(self._sfz_table, stretch=1)

        self._sfz_name_lbl = _label("", C["lime"], 11)
        self._sfz_name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._sfz_name_lbl)
        return page

    def _build_vst3_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        scan_row = QHBoxLayout()
        scan_btn = QPushButton("↺  Scan System Paths")
        scan_btn.setFixedWidth(180)
        scan_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:{C['purple']};"
            f" border:1px solid rgba(153,69,255,0.4); border-radius:4px;"
            f" padding:4px 12px; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.2); }}"
        )
        scan_btn.clicked.connect(self._scan_vst3)
        scan_row.addWidget(scan_btn)
        self._vst3_hint = QLabel("")
        self._vst3_hint.setStyleSheet(
            f"color:{C['text_dim']}; font-size:10px;")
        scan_row.addWidget(self._vst3_hint)
        scan_row.addStretch()
        lay.addLayout(scan_row)

        self._vst3_list = QListWidget()
        self._vst3_list.setStyleSheet(
            f"QListWidget {{ background:{C['deep']};"
            f" border:1px solid rgba(153,69,255,0.3); border-radius:4px; }}"
            f"QListWidget::item {{ color:{C['text']}; padding:4px 8px; }}"
            f"QListWidget::item:selected {{ background:rgba(153,69,255,0.25);"
            f" color:{C['purple']}; }}"
        )
        self._vst3_list.currentRowChanged.connect(self._on_vst3_row_changed)
        lay.addWidget(self._vst3_list, stretch=1)

        browse_vst_row = QHBoxLayout()
        browse_vst_row.addWidget(
            _label("Or choose a file manually:", C["text_dim"], 10))
        browse_vst_btn = QPushButton("Browse…")
        browse_vst_btn.setFixedWidth(100)
        browse_vst_btn.clicked.connect(self._browse_vst3)
        browse_vst_row.addWidget(browse_vst_btn)
        browse_vst_row.addStretch()
        lay.addLayout(browse_vst_row)

        self._scan_vst3()
        return page

    def _build_ds_page(self) -> QWidget:
        """
        Decent Sampler tab page.

        Lets the user browse for a .dspreset file and shows a summary table
        of the zones parsed from it (key range, velocity range, sample path).
        The actual audio loading happens after the dialog is accepted.
        """
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        # ── File picker row ────────────────────────────────────────────────
        browse_row = QHBoxLayout()
        browse_row.addWidget(_label("DSPRESET FILE", C["text_dim"], 10))
        # Shows the file name after the user picks one.
        self._ds_path_lbl = QLabel("(no file selected)")
        self._ds_path_lbl.setStyleSheet(
            f"color:{C['text_dim']}; font-size:10px; background:transparent;")
        self._ds_path_lbl.setWordWrap(True)
        browse_row.addWidget(self._ds_path_lbl, stretch=1)
        browse_ds_btn = QPushButton("Browse DS…")
        browse_ds_btn.setFixedWidth(110)
        browse_ds_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:{C['gold']};"
            f" border:1px solid rgba(255,215,0,0.4); border-radius:4px;"
            f" padding:4px 10px; font-size:10px; }}"
            f"QPushButton:hover {{ background:rgba(255,215,0,0.1); }}"
        )
        browse_ds_btn.clicked.connect(self._browse_ds)
        browse_row.addWidget(browse_ds_btn)
        lay.addLayout(browse_row)

        # ── Instrument name label (populated after parsing) ────────────────
        self._ds_name_lbl = _label("", C["gold"], 11)
        self._ds_name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._ds_name_lbl)

        # ── Zone summary table ─────────────────────────────────────────────
        # Shows one row per zone so the user can confirm they picked the right file.
        self._ds_table = QTableWidget(0, 4)
        self._ds_table.setHorizontalHeaderLabels(
            ["Key Lo", "Key Hi", "Vel Lo", "Sample"])
        self._ds_table.horizontalHeader().setStretchLastSection(True)
        self._ds_table.setAlternatingRowColors(True)
        self._ds_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._ds_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._ds_table.setStyleSheet(
            f"QTableWidget {{ background:{C['deep']}; color:{C['text']};"
            f" gridline-color:{C['surface']};"
            f" border:1px solid rgba(255,215,0,0.2); }}"
            f"QHeaderView::section {{ background:{C['abyss']}; color:{C['text_dim']};"
            f" border:none; padding:2px; }}"
            f"QTableWidget::item:alternate {{ background:{C['abyss']}; }}"
        )
        lay.addWidget(self._ds_table, stretch=1)

        # ── Info footer (zone count, group count) ──────────────────────────
        self._ds_info_lbl = _label("", C["text_dim"], 9)
        lay.addWidget(self._ds_info_lbl)

        return page

    # ── Decent Sampler slots ───────────────────────────────────────────────

    def _browse_ds(self) -> None:
        """Open a file picker for a .dspreset file and parse it for preview."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Decent Sampler Preset",
            os.path.expanduser("~"),
            "Decent Sampler (*.dspreset);;All Files (*)",
        )
        if not path:
            return
        self._ds_path = path
        self._ds_path_lbl.setText(os.path.basename(path))

        # Parse the preset so the table can be populated for the user to verify.
        try:
            info = parse_dspreset(path)
        except Exception as exc:
            logger.warning("InstrumentSelectorDialog: dspreset parse failed: %s", exc)
            self._ds_name_lbl.setText("(parse error — check the file)")
            return

        # Show instrument name and zone/group stats in the footer.
        self._ds_name_lbl.setText(info.name)
        self._ds_info_lbl.setText(
            f"{info.num_zones} zones  ·  {info.num_groups} groups")

        # Populate the zone table with key range, velocity range, and sample path.
        self._ds_table.setRowCount(0)
        for zone in info.zones:
            row = self._ds_table.rowCount()
            self._ds_table.insertRow(row)
            self._ds_table.setItem(row, 0, QTableWidgetItem(str(zone.lo_note)))
            self._ds_table.setItem(row, 1, QTableWidgetItem(str(zone.hi_note)))
            self._ds_table.setItem(row, 2, QTableWidgetItem(str(zone.lo_vel)))
            self._ds_table.setItem(row, 3,
                QTableWidgetItem(os.path.basename(zone.path)))

    # ── SF2 slots ──────────────────────────────────────────────────────────

    def _on_cat(self, row: int) -> None:
        if row < 0:
            return
        cat = self._cat_list.item(row).text()
        self._pre_list.clear()
        for preset, bank, name in GM_INSTRUMENTS.get(cat, []):
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, (preset, bank, name))
            self._pre_list.addItem(item)
        self._pre_list.setCurrentRow(0)

    def _browse_sf2(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SoundFont", os.path.expanduser("~"),
            "SoundFont Files (*.sf2);;All Files (*)"
        )
        if path:
            self._sf2_combo.insertItem(0, os.path.basename(path), userData=path)
            self._sf2_combo.setCurrentIndex(0)

    def _reload_preview(self) -> None:
        sf2: str = self._sf2_combo.currentData() or ""
        pre_item = self._pre_list.currentItem()
        if not sf2 or not os.path.isfile(sf2) or pre_item is None:
            return
        preset, bank, _ = pre_item.data(Qt.ItemDataRole.UserRole)
        self._engine.load_preview_instrument(sf2, bank, preset)

    # ── SFZ slots ──────────────────────────────────────────────────────────

    def _browse_sfz(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open SFZ Instrument", os.path.expanduser("~"),
            "SFZ Instruments (*.sfz);;All Files (*)"
        )
        if path:
            self._load_sfz_preview(path)

    def _load_sfz_preview(self, path: str) -> None:
        self._sfz_path = path
        self._sfz_path_lbl.setText(os.path.basename(path))
        self._sfz_path_lbl.setStyleSheet(
            f"color:{C['text']}; font-size:10px; background:transparent;")
        try:
            info = parse_sfz(path)
            self._sfz_keyboard.set_regions(info.regions)
            self._sfz_table.setRowCount(len(info.regions))
            for row, r in enumerate(info.regions):
                self._sfz_table.setItem(
                    row, 0, QTableWidgetItem(str(r.key_range.lo)))
                self._sfz_table.setItem(
                    row, 1, QTableWidgetItem(str(r.key_range.hi)))
                self._sfz_table.setItem(
                    row, 2, QTableWidgetItem(str(r.vel_range.lo)))
                self._sfz_table.setItem(
                    row, 3, QTableWidgetItem(str(r.vel_range.hi)))
                self._sfz_table.setItem(row, 4, QTableWidgetItem(r.sample))
            self._sfz_name_lbl.setText(
                f"{info.name}  ·  {info.num_regions} regions,"
                f" {info.num_groups} groups"
            )
        except Exception as exc:
            self._sfz_name_lbl.setText(f"Parse error: {exc}")

    # ── VST3 slots ─────────────────────────────────────────────────────────

    def _scan_vst3(self) -> None:
        self._vst3_list.clear()
        plugins = scan_vst_paths()
        if plugins:
            self._vst3_hint.setText(f"{len(plugins)} plugin(s) found")
            for p in plugins:
                item = QListWidgetItem(os.path.basename(p))
                item.setData(Qt.ItemDataRole.UserRole, p)
                self._vst3_list.addItem(item)
        else:
            self._vst3_hint.setText("No plugins found — use Browse…")
            self._vst3_list.addItem(
                QListWidgetItem("— No plugins found in standard directories —"))

    def _on_vst3_row_changed(self, row: int) -> None:
        item = self._vst3_list.item(row)
        if item:
            path = item.data(Qt.ItemDataRole.UserRole)
            if path:
                self._vst3_path = path

    def _browse_vst3(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select VST3 Plugin", os.path.expanduser("~"),
            "Plugin Files (*.vst3 *.component);;All Files (*)",
        )
        if path:
            self._vst3_path = path
            item = QListWidgetItem(os.path.basename(path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            self._vst3_list.insertItem(0, item)
            self._vst3_list.setCurrentRow(0)

    # ── Apply / lifecycle ──────────────────────────────────────────────────

    def _on_ok(self) -> None:
        idx = self._stack.currentIndex()
        if idx == 0:  # SF2
            sf2: str = self._sf2_combo.currentData() or ""
            if not sf2 or not os.path.isfile(sf2):
                QMessageBox.warning(self, "No SoundFont",
                    "Please select a valid .sf2 file or click Browse…")
                return
            pre_item = self._pre_list.currentItem()
            if pre_item is None:
                QMessageBox.warning(self, "No Instrument",
                    "Please select an instrument.")
                return
            preset, bank, preset_name = pre_item.data(Qt.ItemDataRole.UserRole)
            self._result = ("sf2", sf2, bank, preset, preset_name)
        elif idx == 1:  # SFZ
            if not self._sfz_path or not os.path.isfile(self._sfz_path):
                QMessageBox.warning(self, "No SFZ File",
                    "Please browse and select a .sfz instrument file.")
                return
            name = os.path.splitext(os.path.basename(self._sfz_path))[0]
            self._result = ("sfz", self._sfz_path, name)
        elif idx == 2:  # VST3
            if not self._vst3_path or not os.path.isfile(self._vst3_path):
                QMessageBox.warning(self, "No Plugin",
                    "Please select a plugin from the list or use Browse…")
                return
            name = os.path.splitext(os.path.basename(self._vst3_path))[0]
            self._result = ("vst3", self._vst3_path, name)
        else:           # DS — Decent Sampler (.dspreset)
            if not self._ds_path or not os.path.isfile(self._ds_path):
                QMessageBox.warning(self, "No Preset",
                    "Please browse and select a .dspreset file.")
                return
            name = os.path.splitext(os.path.basename(self._ds_path))[0]
            self._result = ("ds", self._ds_path, name)
        self._cleanup_preview()
        self.accept()

    def _cleanup_preview(self) -> None:
        self._preview.silence_all()
        self._engine.unload_preview_instrument()

    def done(self, result: int) -> None:
        self._cleanup_preview()
        super().done(result)

    def keyPressEvent(self, event) -> None:
        from PySide6.QtWidgets import QLineEdit
        if self._stack.currentIndex() == 0 and not isinstance(
                self.focusWidget(), QLineEdit):
            self._preview.keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        from PySide6.QtWidgets import QLineEdit
        if self._stack.currentIndex() == 0 and not isinstance(
                self.focusWidget(), QLineEdit):
            self._preview.keyReleaseEvent(event)
        else:
            super().keyReleaseEvent(event)

    def result_info(self) -> Optional[tuple]:
        return self._result

    # Backward-compat alias kept so any external code still compiles.
    def result_plugin_info(self) -> Optional[tuple]:
        return self._result


# Keep the old name available in case anything imports it directly.
ChangeInstrumentDialog = InstrumentSelectorDialog


# ═══════════════════════════════════════════════════════════════════════════
# VST Browser Dialog
# ═══════════════════════════════════════════════════════════════════════════

class VstBrowserDialog(QDialog):
    """
    File browser for selecting a VST3 or Audio Unit plugin.

    On open the dialog automatically scans the standard system plugin
    directories and lists every found plugin.  The user may also locate a
    plugin manually via "Browse…".

    Returns the selected plugin path and a user-supplied track name via
    result_path() / result_name() after Accepted.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("✦  Load VST3 / AU Plugin")
        self.setMinimumSize(660, 500)
        self.setStyleSheet(STYLESHEET)

        self._result_path: str = ""
        self._result_name: str = ""

        root = QVBoxLayout(self)
        root.setSpacing(10)

        root.addWidget(_label("SELECT A VST3 / AU PLUGIN", C["purple"], 12, True))

        # Scan button + status hint
        scan_row = QHBoxLayout()
        scan_btn = QPushButton("↺  Scan System Paths")
        scan_btn.setFixedWidth(180)
        scan_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:{C['purple']};"
            f" border:1px solid rgba(153,69,255,0.4); border-radius:4px;"
            f" padding:4px 12px; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.2); }}"
        )
        scan_btn.clicked.connect(self._scan)
        scan_row.addWidget(scan_btn)
        self._scan_hint = QLabel("")
        self._scan_hint.setStyleSheet(f"color:{C['text_dim']}; font-size:10px;")
        scan_row.addWidget(self._scan_hint)
        scan_row.addStretch()
        root.addLayout(scan_row)

        # Plugin list
        self._plugin_list = QListWidget()
        self._plugin_list.setStyleSheet(
            f"QListWidget {{ background:{C['deep']}; border:1px solid rgba(153,69,255,0.3);"
            f" border-radius:4px; }}"
            f"QListWidget::item {{ color:{C['text']}; padding:4px 8px; }}"
            f"QListWidget::item:selected {{ background:rgba(153,69,255,0.25);"
            f" color:{C['purple']}; }}"
        )
        self._plugin_list.currentRowChanged.connect(self._on_row_changed)
        root.addWidget(self._plugin_list, stretch=1)

        # Manual browse row
        browse_row = QHBoxLayout()
        browse_row.addWidget(
            _label("Or choose a file manually:", C["text_dim"], 10))
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(100)
        browse_btn.clicked.connect(self._browse)
        browse_row.addWidget(browse_btn)
        browse_row.addStretch()
        root.addLayout(browse_row)

        # Track name input
        nm_row = QHBoxLayout()
        nm_row.addWidget(_label("TRACK NAME", C["text_dim"], 10))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g.  My Synth Lead")
        nm_row.addWidget(self._name_edit)
        root.addLayout(nm_row)

        # Dialog buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        self._ok_btn = QPushButton("✦  Load Plugin")
        self._ok_btn.setDefault(True)
        self._ok_btn.setEnabled(False)
        self._ok_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(153,69,255,0.18);"
            f" border:1px solid {C['purple']}; color:{C['purple']};"
            f" border-radius:5px; padding:6px 18px; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.38); }}"
            f"QPushButton:disabled {{ color:rgba(153,69,255,0.3);"
            f" border-color:rgba(153,69,255,0.15); }}"
        )
        self._ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(self._ok_btn)
        root.addLayout(btn_row)

        # Populate list immediately
        self._scan()

    # ------------------------------------------------------------------

    def _scan(self) -> None:
        """Scan OS plugin paths and refresh the list widget."""
        self._plugin_list.clear()
        plugins = scan_vst_paths()
        if plugins:
            self._scan_hint.setText(f"{len(plugins)} plugin(s) found")
            for p in plugins:
                item = QListWidgetItem(os.path.basename(p))
                item.setData(Qt.UserRole, p)
                self._plugin_list.addItem(item)
        else:
            self._scan_hint.setText("No plugins found in system paths — use Browse…")
            self._plugin_list.addItem(
                QListWidgetItem("— No plugins found in standard directories —"))

    def _on_row_changed(self, row: int) -> None:
        item = self._plugin_list.item(row)
        if not item:
            return
        path = item.data(Qt.UserRole)
        if not path:
            return
        self._result_path = path
        if not self._name_edit.text():
            self._name_edit.setText(
                os.path.splitext(os.path.basename(path))[0])
        self._ok_btn.setEnabled(True)

    def _browse(self) -> None:
        start = "/Library/Audio/Plug-Ins/VST3" if os.path.isdir(
            "/Library/Audio/Plug-Ins/VST3") else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select VST3 / AU Plugin", start,
            "Plugin Files (*.vst3 *.component);;All Files (*)",
        )
        if path:
            self._result_path = path
            if not self._name_edit.text():
                self._name_edit.setText(
                    os.path.splitext(os.path.basename(path))[0])
            self._ok_btn.setEnabled(True)

    def _on_ok(self) -> None:
        if not self._result_path:
            QMessageBox.warning(self, "No Plugin Selected",
                "Please pick a plugin from the list or use Browse…")
            return
        self._result_name = (
            self._name_edit.text().strip()
            or os.path.splitext(os.path.basename(self._result_path))[0]
        )
        self.accept()

    def result_path(self) -> str: return self._result_path
    def result_name(self) -> str: return self._result_name


# ═══════════════════════════════════════════════════════════════════════════
# VST Parameter Panel
# ═══════════════════════════════════════════════════════════════════════════

class VstParameterPanel(QDialog):
    """
    Live parameter editor for a loaded VST plugin.

    Displays every plugin parameter as a labelled horizontal slider.
    Slider changes are forwarded immediately to VstManager.set_parameter
    so the plugin hears the new value without requiring an OK button.

    The "Open Native Editor" button opens the plugin's own GUI in a
    background thread so the Qt event loop stays responsive.
    """

    def __init__(
        self,
        vst_manager: VstManager,
        channel:     int,
        controller,                    # ControllerManager — for key monitor routing
        parent:      Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._vst_manager = vst_manager
        self._channel     = channel
        self._controller  = controller

        track = vst_manager.get_track(channel)
        name  = track.name if track else f"Channel {channel + 1}"

        self.setWindowTitle(f"VST Parameters — {name}")
        self.setMinimumSize(480, 520)
        self.setStyleSheet(STYLESHEET)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        root.addWidget(_label(f"PARAMETERS  —  {name}", C["purple"], 12, True))

        params = vst_manager.get_parameters(channel)

        if not params:
            msg = ("No parameters available.\n\n"
                   "Either the plugin has no exposed parameters, or "
                   "pedalboard could not read them.")
            root.addWidget(_label(msg, C["text_dim"], 11))
        else:
            # Scrollable grid of parameter sliders
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet(
                f"QScrollArea {{ background:{C['deep']}; border:none; }}")
            inner = QWidget()
            inner.setStyleSheet(f"background:{C['deep']};")
            grid = QGridLayout(inner)
            grid.setSpacing(6)
            grid.setContentsMargins(8, 8, 8, 8)

            for row, (pname, value) in enumerate(params.items()):
                lbl = QLabel(pname[:28])
                lbl.setStyleSheet(
                    f"color:{C['text']}; font-size:11px;")

                slider = QSlider(Qt.Horizontal)
                slider.setRange(0, 1000)
                slider.setValue(int(value * 1000))

                val_lbl = QLabel(f"{value:.3f}")
                val_lbl.setFixedWidth(46)
                val_lbl.setStyleSheet(
                    f"color:{C['cyan']}; font-size:10px;")

                # Capture the parameter name and value label in a closure
                def _handler(v: int, p: str = pname, vl: QLabel = val_lbl) -> None:
                    nv = v / 1000.0
                    self._vst_manager.set_parameter(self._channel, p, nv)
                    vl.setText(f"{nv:.3f}")

                slider.valueChanged.connect(_handler)

                grid.addWidget(lbl,     row, 0)
                grid.addWidget(slider,  row, 1)
                grid.addWidget(val_lbl, row, 2)

            scroll.setWidget(inner)
            root.addWidget(scroll, stretch=1)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        native_btn = QPushButton("⬡  Open Native Editor")
        native_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(153,69,255,0.15);"
            f" color:{C['purple']}; border:1px solid rgba(153,69,255,0.4);"
            f" border-radius:4px; padding:6px 14px; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.35); }}"
        )
        native_btn.clicked.connect(self._open_native)
        btn_row.addWidget(native_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def _open_native(self) -> None:
        """
        Open the plugin's own GUI editor.

        Strategy (macOS / JUCE / CoreAudio):
          - Stop the CoreAudio stream BEFORE show_editor() so JUCE can
            initialise its editor without a concurrent audio callback.
          - Schedule a stream restart 200 ms in via QTimer (which fires
            inside show_editor()'s CFRunLoop) so QWERTY notes play while
            the editor is open.
          - Install a PyObjC NSEvent local monitor so keyboard events reach
            our callbacks even when the plugin editor has Cocoa focus.
          - On close, stop immediately so process() cannot race with JUCE's
            editor teardown, then restart 300 ms later.
        """
        track = self._vst_manager.get_track(self._channel)
        if not track or not track._plugin:
            QMessageBox.warning(self, "Native Editor",
                "No VST plugin is loaded on this channel.")
            return
        if not hasattr(track._plugin, "show_editor"):
            QMessageBox.warning(self, "Native Editor",
                f"'{track.name}' does not expose a native GUI editor.\n\n"
                "Use the parameter sliders above to adjust the sound.")
            return

        self._controller.set_active_channel(self._channel)
        self._controller.reset_key_states()

        player = self._vst_manager._rt_players.get(self._channel)

        # Phase 1 — stop before open (prevents JUCE-init / CoreAudio conflict).
        if player:
            player.stop()

        # Phase 2 — schedule audio restart inside the editor session.
        # show_editor() runs the macOS CFRunLoop so Qt timers fire normally.
        # 200 ms gives JUCE enough time to finish editor init before the audio
        # callback starts calling process() concurrently.
        _start_timer: Optional[QTimer] = None
        if player:
            _start_timer = QTimer()
            _start_timer.setSingleShot(True)
            _start_timer.timeout.connect(player.start)
            _start_timer.start(200)

        from . import macos_key_hook
        _key_monitor = macos_key_hook.install(
            self._controller.handle_key_press,
            self._controller.handle_key_release,
        )

        try:
            track._plugin.show_editor()
        except Exception as exc:
            QMessageBox.warning(self, "Native Editor",
                f"Could not open native editor:\n{exc}")
        finally:
            # Cancel the restart timer if the editor was closed within 200 ms.
            if _start_timer is not None:
                _start_timer.stop()

            macos_key_hook.remove(_key_monitor)
            self._controller.reset_key_states()

            # Phase 3 — stop immediately on close so process() cannot race
            # with JUCE's editor teardown, then restart after 300 ms.
            if player:
                player.stop()
            if player:
                QTimer.singleShot(300, player.start)

        # Restore Qt keyboard focus.
        main_win = self.parent()
        while main_win is not None and main_win.parent() is not None:
            main_win = main_win.parent()
        if main_win is not None:
            main_win.activateWindow()
            main_win.raise_()


# ═══════════════════════════════════════════════════════════════════════════
# Export Dialog
# ═══════════════════════════════════════════════════════════════════════════

class ExportDialog(QDialog):
    """Choose export format (WAV / MP3 / AAC), filename, and render."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("✦  Export Audio")
        self.setMinimumWidth(440)
        self.setStyleSheet(STYLESHEET)

        self._fmt: str = "wav"

        root = QVBoxLayout(self)
        root.setSpacing(12)

        root.addWidget(_label("EXPORT AUDIO", C["gold"], 13, True))

        # ── Format ────────────────────────────────────────────────────────
        fmt_box = QGroupBox("Format")
        fmt_box.setStyleSheet(
            f"QGroupBox {{ color:{C['text_dim']}; border:1px solid rgba(0,229,255,0.2);"
            f" border-radius:5px; margin-top:8px; }}"
            f"QGroupBox::title {{ subcontrol-origin:margin; left:8px; }}")
        fmt_lay = QHBoxLayout(fmt_box)
        self._wav_radio = QRadioButton("WAV  (lossless)")
        self._mp3_radio = QRadioButton("MP3  (192 kbps)")
        self._aac_radio = QRadioButton("AAC  (.m4a, 192 kbps)")
        self._wav_radio.setChecked(True)
        for rb in (self._wav_radio, self._mp3_radio, self._aac_radio):
            rb.setStyleSheet(f"color:{C['text']};")
            fmt_lay.addWidget(rb)
        self._wav_radio.toggled.connect(lambda c: self._on_fmt_changed("wav") if c else None)
        self._mp3_radio.toggled.connect(lambda c: self._on_fmt_changed("mp3") if c else None)
        self._aac_radio.toggled.connect(lambda c: self._on_fmt_changed("aac") if c else None)
        root.addWidget(fmt_box)

        # ── Filename ──────────────────────────────────────────────────────
        file_box = QGroupBox("Output file")
        file_box.setStyleSheet(
            f"QGroupBox {{ color:{C['text_dim']}; border:1px solid rgba(0,229,255,0.2);"
            f" border-radius:5px; margin-top:8px; }}"
            f"QGroupBox::title {{ subcontrol-origin:margin; left:8px; }}")
        file_lay = QHBoxLayout(file_box)
        self._path_edit = QLineEdit(os.path.expanduser("~/Untitled.wav"))
        self._path_edit.setStyleSheet(
            f"background:{C['deep']}; color:{C['text']};"
            f" border:1px solid rgba(0,229,255,0.2); border-radius:4px; padding:4px;")
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse)
        file_lay.addWidget(self._path_edit, stretch=1)
        file_lay.addWidget(browse_btn)
        root.addWidget(file_box)

        root.addStretch()

        # ── Buttons ───────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        export = QPushButton("Export")
        export.setStyleSheet(
            f"QPushButton {{ background:rgba(255,170,0,0.15);"
            f" color:{C['gold']}; border:1px solid rgba(255,170,0,0.45);"
            f" border-radius:4px; padding:6px 20px; }}"
            f"QPushButton:hover {{ background:rgba(255,170,0,0.35); }}")
        export.clicked.connect(self.accept)
        btn_row.addWidget(cancel)
        btn_row.addWidget(export)
        root.addLayout(btn_row)

    def _on_fmt_changed(self, fmt: str) -> None:
        self._fmt = fmt
        ext = {"mp3": ".mp3", "aac": ".m4a"}.get(fmt, ".wav")
        self._path_edit.setText(os.path.splitext(self._path_edit.text())[0] + ext)

    def _browse(self) -> None:
        filters = {
            "mp3": ("MP3 Audio (*.mp3);;All Files (*)", ".mp3"),
            "aac": ("AAC Audio (*.m4a);;All Files (*)", ".m4a"),
        }
        filt, ext = filters.get(self._fmt, ("WAV Audio (*.wav);;All Files (*)", ".wav"))
        default = os.path.splitext(self._path_edit.text())[0] + ext
        path, _ = QFileDialog.getSaveFileName(self, "Export Audio", default, filt)
        if path:
            self._path_edit.setText(path)

    def result_path(self) -> str:
        return self._path_edit.text()

    def result_format(self) -> str:
        if self._mp3_radio.isChecked():
            return "mp3"
        if self._aac_radio.isChecked():
            return "aac"
        return "wav"


# ═══════════════════════════════════════════════════════════════════════════
# Add Track Dialog  (re-styled but same logic)
# ═══════════════════════════════════════════════════════════════════════════

class AddTrackDialog(QDialog):
    """GM instrument browser with crystal theme."""

    def __init__(self, engine: AudioEngine,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._result_track:     Optional[MidiTrack]       = None
        self._result_plugin:    Optional[InstrumentPlugin] = None
        self._result_vst_track: Optional[VstTrack]        = None
        self._result_sfz_track: Optional[MidiTrack]       = None
        self._result_sfz_path:  str                       = ""
        # Decent Sampler track result (set when user picks a .dspreset file).
        self._result_ds_track:  Optional[MidiTrack]       = None
        self._result_ds_path:   str                       = ""

        self.setWindowTitle("✦  Add Instrument Track")
        self.setMinimumSize(600, 500)
        self.setStyleSheet(STYLESHEET)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # Header
        hdr = _label("SELECT INSTRUMENT", C["cyan"], 13, True)
        hdr.setStyleSheet(
            f"color:{C['cyan']}; font-size:13px; font-weight:bold;"
            f" letter-spacing:3px; background:transparent;"
        )
        root.addWidget(hdr)

        # SF2 picker row
        sf2_row = QHBoxLayout()
        sf2_row.addWidget(_label("SOUNDFONT", C["text_dim"], 10))
        self._sf2_combo = QComboBox()
        self._sf2_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sf2_files = AudioEngine.get_available_sf2_files()
        for p in sf2_files:
            self._sf2_combo.addItem(os.path.basename(p), userData=p)
        if not sf2_files:
            self._sf2_combo.addItem("— no SF2 found —", userData="")
        sf2_row.addWidget(self._sf2_combo)
        browse = QPushButton("Browse…")
        browse.setFixedWidth(90)
        browse.clicked.connect(self._browse)
        sf2_row.addWidget(browse)
        root.addLayout(sf2_row)

        # Category / Preset splitter
        sp = QSplitter(Qt.Horizontal)
        self._cat_list = QListWidget()
        self._cat_list.setFixedWidth(155)
        for cat in GM_INSTRUMENTS:
            self._cat_list.addItem(QListWidgetItem(cat))
        self._cat_list.currentRowChanged.connect(self._on_cat)
        sp.addWidget(self._cat_list)

        self._pre_list = QListWidget()
        self._pre_list.currentRowChanged.connect(self._on_pre)
        sp.addWidget(self._pre_list)

        sp.setStretchFactor(0, 0)
        sp.setStretchFactor(1, 1)
        root.addWidget(sp, stretch=1)

        # ── Live preview keyboard ──────────────────────────────────────────
        prev_lbl = _label(
            "PREVIEW  ·  click keys or use A–K / W–P on your keyboard",
            C["text_dim"], 9,
        )
        root.addWidget(prev_lbl)
        self._preview = InstrumentPreviewWidget(self)
        self._preview.set_note_callbacks(
            on_note_on  = self._engine.preview_note_on,
            on_note_off = self._engine.preview_note_off,
        )
        root.addWidget(self._preview)
        # Reload preview whenever SF2 or preset selection changes.
        self._sf2_combo.currentIndexChanged.connect(self._reload_preview)

        # Track name row
        nm_row = QHBoxLayout()
        nm_row.addWidget(_label("TRACK NAME", C["text_dim"], 10))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g.  Crystal Piano Melody")
        nm_row.addWidget(self._name_edit)
        root.addLayout(nm_row)

        # OK / Cancel
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        ok = QPushButton("✦  Add SF2 Track")
        ok.setDefault(True)
        ok.setStyleSheet(
            f"QPushButton {{ background:rgba(0,229,255,0.15);"
            f" border:1px solid {C['cyan']}; color:{C['cyan']};"
            f" border-radius:5px; padding:6px 18px; }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.3); }}"
        )
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(ok)
        root.addLayout(btn_row)

        # Alternative instrument sources: SFZ and VST3
        alt_sep = QFrame()
        alt_sep.setFrameShape(QFrame.Shape.HLine)
        alt_sep.setStyleSheet("color:rgba(153,69,255,0.3);")
        root.addWidget(alt_sep)

        alt_row = QHBoxLayout()
        alt_row.addWidget(
            _label("No SoundFont?  Use SFZ, DS or VST3 instead:", C["text_dim"], 10))
        alt_row.addStretch()
        sfz_btn = QPushButton("♪  Load SFZ Instrument…")
        sfz_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(57,255,20,0.08);"
            f" color:{C['lime']}; border:1px solid rgba(57,255,20,0.4);"
            f" border-radius:4px; padding:5px 14px; }}"
            f"QPushButton:hover {{ background:rgba(57,255,20,0.22); }}"
        )
        sfz_btn.clicked.connect(self._on_load_sfz)
        alt_row.addWidget(sfz_btn)

        # Decent Sampler (.dspreset) track button.
        ds_btn = QPushButton("◈  Load DS Preset…")
        ds_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(255,215,0,0.08);"
            f" color:{C['gold']}; border:1px solid rgba(255,215,0,0.4);"
            f" border-radius:4px; padding:5px 14px; }}"
            f"QPushButton:hover {{ background:rgba(255,215,0,0.22); }}"
        )
        ds_btn.clicked.connect(self._on_load_ds)
        alt_row.addWidget(ds_btn)

        vst_btn = QPushButton("⬡  Load VST / AU Plugin…")
        vst_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(153,69,255,0.12);"
            f" color:{C['purple']}; border:1px solid rgba(153,69,255,0.4);"
            f" border-radius:4px; padding:5px 14px; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.28); }}"
        )
        vst_btn.clicked.connect(self._on_load_vst)
        alt_row.addWidget(vst_btn)
        root.addLayout(alt_row)

        self._cat_list.setCurrentRow(0)

    def _on_cat(self, row: int) -> None:
        if row < 0:
            return
        cat = self._cat_list.item(row).text()
        self._pre_list.clear()
        for preset, bank, name in GM_INSTRUMENTS.get(cat, []):
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, (preset, bank, name))
            self._pre_list.addItem(item)
        self._pre_list.setCurrentRow(0)

    def _on_pre(self, row: int) -> None:
        if row < 0:
            return
        item = self._pre_list.item(row)
        if item:
            self._name_edit.setText(item.data(Qt.UserRole)[2])
            # Trigger live preview for the newly highlighted preset.
            self._reload_preview()

    def _reload_preview(self) -> None:
        """Load the currently highlighted preset into the preview channel."""
        sf2: str = self._sf2_combo.currentData() or ""
        pre_item = self._pre_list.currentItem()
        if not sf2 or not os.path.isfile(sf2) or pre_item is None:
            return
        preset, bank, _ = pre_item.data(Qt.UserRole)
        self._engine.load_preview_instrument(sf2, bank, preset)

    def _cleanup_preview(self) -> None:
        """Silence all preview notes and release the temporary soundfont."""
        self._preview.silence_all()
        self._engine.unload_preview_instrument()

    def done(self, result: int) -> None:
        """Ensure preview is cleaned up on any close path (accept or reject)."""
        self._cleanup_preview()
        super().done(result)

    # Forward dialog-level keyboard events to the preview widget so note
    # shortcuts work even when a list widget or other control has focus.
    def keyPressEvent(self, event) -> None:
        from PySide6.QtWidgets import QLineEdit
        if not isinstance(self.focusWidget(), QLineEdit):
            self._preview.keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        from PySide6.QtWidgets import QLineEdit
        if not isinstance(self.focusWidget(), QLineEdit):
            self._preview.keyReleaseEvent(event)
        else:
            super().keyReleaseEvent(event)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SoundFont", os.path.expanduser("~"),
            "SoundFont Files (*.sf2);;All Files (*)"
        )
        if path:
            self._sf2_combo.insertItem(0, os.path.basename(path), userData=path)
            self._sf2_combo.setCurrentIndex(0)

    def _on_ok(self) -> None:
        sf2: str = self._sf2_combo.currentData() or ""
        if not sf2 or not os.path.isfile(sf2):
            QMessageBox.warning(self, "No SoundFont",
                "Please select a valid .sf2 file or click Browse…")
            return
        pre_item = self._pre_list.currentItem()
        if pre_item is None:
            QMessageBox.warning(self, "No Instrument",
                "Please select an instrument.")
            return
        preset, bank, preset_name = pre_item.data(Qt.UserRole)
        name  = self._name_edit.text().strip() or preset_name
        is_dm = (bank == 128)
        ch    = self._engine.next_free_channel(is_drums=is_dm)
        if ch == -1:
            QMessageBox.warning(self, "No Free Channel",
                "All 16 MIDI channels are occupied. Remove a track first.")
            return
        col = C["tracks"][ch % len(C["tracks"])]
        self._result_track  = MidiTrack(name=name, channel=ch, color=col)
        self._result_plugin = InstrumentPlugin(
            name=name, sf2_path=sf2, bank=bank, preset=preset, channel=ch)
        self._cleanup_preview()
        self.accept()

    def _on_load_sfz(self) -> None:
        """Open a file picker for an SFZ file, then accept with an SFZ track result."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open SFZ Instrument", os.path.expanduser("~"),
            "SFZ Instruments (*.sfz);;All Files (*)",
        )
        if not path:
            return
        name_edit = self._name_edit.text().strip()
        name = name_edit or os.path.splitext(os.path.basename(path))[0]
        ch = self._engine.next_free_channel(is_drums=False)
        if ch == -1:
            QMessageBox.warning(self, "No Free Channel",
                "All 16 MIDI channels are occupied. Remove a track first.")
            return
        col = C["tracks"][ch % len(C["tracks"])]
        self._result_sfz_track = MidiTrack(name=name, channel=ch, color=col)
        self._result_sfz_path  = path
        self._cleanup_preview()
        self.accept()

    def _on_load_vst(self) -> None:
        """Open the VST browser; if a plugin is chosen, create a VstTrack and accept."""
        dlg = VstBrowserDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        path = dlg.result_path()
        name = dlg.result_name()
        if not path:
            return
        ch = self._engine.next_free_channel(is_drums=False)
        if ch == -1:
            QMessageBox.warning(self, "No Free Channel",
                "All 16 MIDI channels are occupied. Remove a track first.")
            return
        col = C["tracks"][ch % len(C["tracks"])]
        self._result_vst_track = VstTrack(
            name=name, plugin_path=path, channel=ch, color=col)
        self.accept()

    def _on_load_ds(self) -> None:
        """
        Open a file picker for a .dspreset file, then accept the dialog
        with a Decent Sampler track result.
        """
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Decent Sampler Preset",
            os.path.expanduser("~"),
            "Decent Sampler (*.dspreset);;All Files (*)",
        )
        if not path:
            return
        # Derive track name: prefer what the user typed, fall back to file stem.
        name_edit = self._name_edit.text().strip()
        name = name_edit or os.path.splitext(os.path.basename(path))[0]
        ch = self._engine.next_free_channel(is_drums=False)
        if ch == -1:
            QMessageBox.warning(self, "No Free Channel",
                "All 16 MIDI channels are occupied. Remove a track first.")
            return
        col = C["tracks"][ch % len(C["tracks"])]
        self._result_ds_track = MidiTrack(name=name, channel=ch, color=col)
        self._result_ds_path  = path
        self._cleanup_preview()
        self.accept()

    # ── Result accessors ───────────────────────────────────────────────────

    def result_track(self)     -> Optional[MidiTrack]:       return self._result_track
    def result_plugin(self)    -> Optional[InstrumentPlugin]: return self._result_plugin
    def result_vst_track(self) -> Optional[VstTrack]:        return self._result_vst_track
    def result_sfz_track(self) -> Optional[MidiTrack]:       return self._result_sfz_track
    def result_sfz_path(self)  -> str:                       return self._result_sfz_path
    def result_ds_track(self)  -> Optional[MidiTrack]:       return self._result_ds_track
    def result_ds_path(self)   -> str:                       return self._result_ds_path


# ═══════════════════════════════════════════════════════════════════════════
# Track Arrangement View
# ═══════════════════════════════════════════════════════════════════════════

class TrackArrangeView(QWidget):
    """
    Arrangement timeline — one horizontal lane per MIDI track plus one lane
    per imported audio track.

    MIDI track lanes show individual MidiClip blocks.  Each block is
    draggable (body) and resizable (right-edge grip).  Left-clicking a
    clip selects it and opens it in the piano roll.  Right-clicking shows
    a context menu (Delete Clip / Duplicate Clip).  Clicking an empty
    part of the track lane creates a new empty clip.

    Audio track lanes show their AudioClip block.  Right-clicking the
    track header or the clip removes the audio track.

    Left column  : track name / channel / note-count labels.
                   Right-click MIDI track header → change instrument or remove.
    Right area   : ruler + timeline with clip blocks.
    """

    track_selected               = Signal(int)          # channel
    audio_track_selected         = Signal(int)          # track_id (audio)
    files_dropped                = Signal(list, float)  # [paths], beat_position
    clip_selected                = Signal(int, int)     # channel, clip_id
    clip_edit_requested          = Signal(int, int)     # channel, clip_id  (simple click, no drag)
    clip_moved                   = Signal(int, int, float)   # channel, clip_id, new_start_beat
    clip_resized                 = Signal(int, int, float)   # channel, clip_id, new_duration_beats
    clip_deleted                 = Signal(int, int)     # channel, clip_id
    clip_duplicated              = Signal(int, int)     # channel, clip_id
    clip_create_requested        = Signal(int, float)   # channel, beat_position
    audio_clip_moved             = Signal(int, int, float)   # track_id, clip_id, new_start_beat
    audio_clip_resized           = Signal(int, int, float)   # track_id, clip_id, new_duration_secs
    audio_clip_deleted           = Signal(int, int)     # track_id, clip_id
    audio_clip_duplicated        = Signal(int, int)     # track_id, clip_id
    scroll_x_changed             = Signal(float)        # view_x
    loop_region_changed          = Signal(float, float) # start_beat, end_beat
    seek_requested               = Signal(float)        # beat position
    change_instrument_requested  = Signal(int)          # channel
    remove_track_requested       = Signal(int)          # channel
    audio_track_remove_requested = Signal(int)          # track_id
    audio_clip_drop_requested    = Signal(float)        # kept for toolbar-flow compat
    midi_fx_requested            = Signal(int)          # MIDI channel — open FX panel
    # Emitted when the user clicks the "AUTO" button in a track header.
    # Payload: (channel_or_track_id, kind) where kind = "midi" or "audio".
    automation_toggled           = Signal(int, str)
    # Emitted whenever the beat-width (zoom) changes so AutomationPanel can sync.
    zoom_changed                 = Signal(int)          # new beat_width in pixels

    TRACK_HEIGHT    : int = 60
    AUDIO_ROW_HEIGHT: int = 52
    HEADER_WIDTH    : int = 168
    BEAT_WIDTH      : int = 18   # default; overridden per-instance for zoom
    RULER_HEIGHT    : int = 24
    RESIZE_GRIP     : int = 8    # right-edge resize handle width (pixels)

    ZOOM_MIN: int = 4
    ZOOM_MAX: int = 80

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._tracks:         List[MidiTrack]  = []
        self._audio_tracks:   List[AudioTrack] = []
        self._active_channel: int   = 0
        self._selected_clip_id: int = -1   # clip_id of the highlighted clip
        self._view_x:         float = 0.0
        self._total_beats:    float = 32.0
        self._bpm:            float = 120.0
        self.BEAT_WIDTH:      int   = 18   # instance copy — zoom changes this

        # Loop region display
        self._loop_enabled: bool  = False
        self._loop_start:   float = 0.0
        self._loop_end:     float = 8.0

        # Ruler interaction (click = seek, hold+drag = loop)
        self._ruler_pressed:      bool  = False
        self._ruler_press_x:      int   = 0
        self._ruler_press_beat:   float = 0.0
        self._ruler_loop_origin:  Optional[float] = None  # set when drag starts

        # Waveform peak generator — set by MainWindow via set_waveform_generator().
        # Handles background loading and caching; None until wired up.
        self._waveform_gen: Optional[WaveformPeakGenerator] = None

        # Clip drag / resize state (shared by MIDI and audio clips)
        self._clip_drag_mode:       str   = "none"   # "move" | "resize" | "none"
        self._drag_track_key:       int   = -1   # channel (MIDI) or track_id (audio)
        self._drag_clip_id:         int   = -1
        self._drag_kind:            str   = "midi"   # "midi" | "audio"
        self._drag_beat_offset:     float = 0.0   # click offset within clip (beats)
        self._drag_clip_orig_start: float = 0.0
        self._drag_clip_orig_dur:   float = 4.0   # beats (MIDI) or seconds (audio)
        self._drag_start_x:         int   = 0

        # Kept for legacy (was _drag_channel)
        self._drag_channel:         int   = -1

        # Current playhead beat — updated from MainWindow._on_refresh_tick.
        self._playhead_beat: float = 0.0

        # Set of track identifiers (channel for MIDI, track_id for audio) that
        # currently have an AutomationLane visible.  Used to highlight the
        # AUTO button and toggle visibility via automation_toggled signal.
        self._auto_tracks: set = set()

        self.setMouseTracking(True)
        self.setStyleSheet(f"background:{C['abyss']};")
        self.setAcceptDrops(True)   # Enable file drag-drop from OS file manager.
        self._resize_widget()

    # ── Public API ─────────────────────────────────────────────────────

    def set_tracks(self, tracks: List[MidiTrack]) -> None:
        self._tracks = list(tracks)
        self._resize_widget()
        self.update()

    def set_audio_tracks(self, audio_tracks: List[AudioTrack]) -> None:
        self._audio_tracks = list(audio_tracks)
        self._resize_widget()
        self.update()

    def set_audio_clips(self, clips) -> None:
        """Legacy stub — audio clips now live in per-track AudioTrack objects."""
        pass

    def set_active_channel(self, ch: int) -> None:
        self._active_channel = ch
        self.update()

    def set_view_x(self, vx: float) -> None:
        self._view_x = max(0.0, vx)
        self.update()

    def set_total_beats(self, beats: float) -> None:
        self._total_beats = beats
        self.update()

    def set_bpm(self, bpm: float) -> None:
        self._bpm = max(20.0, bpm)
        self.update()

    def set_waveform_generator(self, gen: WaveformPeakGenerator) -> None:
        """Wire the background peak loader so audio clips display waveforms."""
        self._waveform_gen = gen

    def set_loop_region(self, enabled: bool, start: float, end: float) -> None:
        self._loop_enabled = enabled
        self._loop_start   = max(0.0, start)
        self._loop_end     = max(self._loop_start + 0.25, end)
        self.update()

    def set_zoom(self, beat_width: int) -> None:
        self.BEAT_WIDTH = max(self.ZOOM_MIN, min(self.ZOOM_MAX, beat_width))
        self.zoom_changed.emit(self.BEAT_WIDTH)  # Sync automation panel zoom
        self.update()

    def zoom_in(self) -> None:
        steps = [4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80]
        for s in steps:
            if s > self.BEAT_WIDTH:
                self.set_zoom(s)
                return

    def zoom_out(self) -> None:
        steps = [4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80]
        for s in reversed(steps):
            if s < self.BEAT_WIDTH:
                self.set_zoom(s)
                return

    def set_playhead_beat(self, beat: float) -> None:
        """Update the playhead position (in beats) and schedule a repaint."""
        self._playhead_beat = beat
        self.update()

    # ── Internal geometry ──────────────────────────────────────────────

    def _resize_widget(self) -> None:
        h = (self.RULER_HEIGHT
             + len(self._tracks) * self.TRACK_HEIGHT
             + len(self._audio_tracks) * self.AUDIO_ROW_HEIGHT)
        self.setFixedHeight(max(80, h))

    def _midi_track_y(self, idx: int) -> int:
        return self.RULER_HEIGHT + idx * self.TRACK_HEIGHT

    def _audio_track_y(self, idx: int) -> int:
        return (self.RULER_HEIGHT
                + len(self._tracks) * self.TRACK_HEIGHT
                + idx * self.AUDIO_ROW_HEIGHT)

    def _beat_to_x(self, beat: float) -> int:
        return self.HEADER_WIDTH + int((beat - self._view_x) * self.BEAT_WIDTH)

    def _x_to_beat(self, x: int) -> float:
        return (x - self.HEADER_WIDTH) / self.BEAT_WIDTH + self._view_x

    def _find_clip_at(self, mx: int, my: int):
        """
        Hit-test (mx, my) against all visible clip blocks.

        Returns (channel_or_id, clip, row_type, hit_type) where:
            row_type = "midi" | "audio"
            hit_type = "body" | "resize"
        or (None, None, None, None) if nothing was hit.
        """
        for idx, track in enumerate(self._tracks):
            y = self._midi_track_y(idx)
            if not (y <= my < y + self.TRACK_HEIGHT):
                continue
            for clip in track.clips:
                cx = self._beat_to_x(clip.start_beat)
                cw = max(20, int(clip.duration * self.BEAT_WIDTH))
                if cx <= mx <= cx + cw:
                    hit = ("resize"
                           if mx >= cx + cw - self.RESIZE_GRIP
                           else "body")
                    return track.channel, clip, "midi", hit
            return None, None, None, None

        for idx, atrack in enumerate(self._audio_tracks):
            y = self._audio_track_y(idx)
            if not (y <= my < y + self.AUDIO_ROW_HEIGHT):
                continue
            secs_per_beat = 60.0 / max(20.0, self._bpm)
            for aclip in atrack.clips:
                cx = self._beat_to_x(aclip.start_beat)
                dur_b = (aclip.duration_seconds / secs_per_beat
                         if aclip.duration_seconds > 0 else 4.0)
                cw = max(40, int(dur_b * self.BEAT_WIDTH))
                if cx <= mx <= cx + cw:
                    hit = ("resize"
                           if mx >= cx + cw - self.RESIZE_GRIP
                           else "body")
                    return atrack.track_id, aclip, "audio", hit
            return None, None, None, None

        return None, None, None, None

    # ── Painting ───────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        # Wrap everything in try/finally so p.end() is always called.
        # A painter left active on the backing store silently eats mouse
        # events on subsequent repaints, making toolbar buttons unresponsive.
        try:
            p.setRenderHint(QPainter.Antialiasing)
            w = self.width()
            p.fillRect(0, 0, w, self.height(), QColor(C["abyss"]))
            p.setPen(QPen(QColor(0, 229, 255, 30), 1))
            p.drawLine(self.HEADER_WIDTH, 0, self.HEADER_WIDTH, self.height())
            self._draw_ruler(p, w)
            for idx, track in enumerate(self._tracks):
                self._draw_midi_lane(p, track, self._midi_track_y(idx), w)
            for idx, atrack in enumerate(self._audio_tracks):
                self._draw_audio_lane(p, atrack, self._audio_track_y(idx), w)
            # Playhead drawn last so it appears on top of all track content.
            self._draw_playhead(p, w)
        finally:
            p.end()

    def _draw_ruler(self, p: QPainter, w: int) -> None:
        p.fillRect(0, 0, w, self.RULER_HEIGHT, QColor(C["void"]))
        p.fillRect(0, 0, self.HEADER_WIDTH, self.RULER_HEIGHT, QColor(C["abyss"]))

        # Loop region overlay
        if self._loop_enabled or self._ruler_loop_origin is not None:
            xs = self._beat_to_x(self._loop_start)
            xe = self._beat_to_x(self._loop_end)
            if xs < w and xe > self.HEADER_WIDTH:
                fill_x = max(self.HEADER_WIDTH, xs)
                fill_w = min(w, xe) - fill_x
                p.fillRect(fill_x, 0, fill_w, self.RULER_HEIGHT,
                           QColor(153, 69, 255, 50))
                p.setPen(QPen(QColor(C["purple"]), 2))
                if self.HEADER_WIDTH <= xs <= w:
                    p.drawLine(xs, 0, xs, self.RULER_HEIGHT)
                if self.HEADER_WIDTH <= xe <= w:
                    p.drawLine(xe, 0, xe, self.RULER_HEIGHT)

        p.setFont(QFont("Arial", 8))
        beat = 0
        while beat <= self._total_beats + 8:
            x = self._beat_to_x(beat)
            if self.HEADER_WIDTH <= x <= w:
                if beat % 4 == 0:
                    p.setPen(QColor(C["cyan"]))
                    p.drawLine(x, 8, x, self.RULER_HEIGHT - 1)
                    p.drawText(x + 3, self.RULER_HEIGHT - 4,
                               f"{int(beat // 4) + 1}")
                else:
                    p.setPen(QPen(QColor(0, 229, 255, 22), 1))
                    p.drawLine(x, 14, x, self.RULER_HEIGHT - 1)
            beat += 1
        p.setPen(QPen(QColor(0, 229, 255, 40), 1))
        p.drawLine(self.HEADER_WIDTH, self.RULER_HEIGHT - 1,
                   w, self.RULER_HEIGHT - 1)

    def _draw_midi_lane(self, p: QPainter, track: MidiTrack,
                        y: int, w: int) -> None:
        is_active = (track.channel == self._active_channel)
        color = QColor(track.color)
        h = self.TRACK_HEIGHT

        # Header
        hbg = QColor(C["surface"]) if is_active else QColor(C["deep"])
        p.fillRect(0, y, self.HEADER_WIDTH, h, hbg)
        p.fillRect(0, y + 1, 4, h - 2, color)
        name_col = color if is_active else QColor(C["text"])
        p.setPen(name_col)
        p.setFont(QFont("Arial", 10, QFont.Bold if is_active else QFont.Normal))
        p.drawText(10, y + 18, track.name[:20])
        p.setPen(QColor(C["text_dim"]))
        p.setFont(QFont("Arial", 8))
        p.drawText(10, y + 32, f"CH {track.channel + 1:02d}")
        total_notes = sum(len(c.notes) for c in track.clips)
        hint = (f"{total_notes} notes · {len(track.clips)} clip(s)"
                if track.clips else "right-click → change instrument")
        p.drawText(10, y + 46, hint)

        # ── AUTO button — toggles automation lane for this track ───────────
        auto_btn_x = self.HEADER_WIDTH - 68
        auto_btn_y = y + h - 18
        auto_btn_w = 28
        auto_btn_h = 14
        auto_active = track.channel in self._auto_tracks
        auto_bg = QColor(0, 229, 255, 80) if auto_active else QColor(0, 80, 100, 120)
        p.fillRect(auto_btn_x, auto_btn_y, auto_btn_w, auto_btn_h, auto_bg)
        p.setPen(QColor(0, 229, 255, 220 if auto_active else 140))
        p.setFont(QFont("Arial", 7, QFont.Bold))
        p.drawText(auto_btn_x + 2, auto_btn_y + auto_btn_h - 3, "AUTO")
        p.setPen(QPen(QColor(0, 200, 220, 120), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(auto_btn_x, auto_btn_y, auto_btn_w, auto_btn_h)

        # ── FX button — bottom-right corner of the header ──────────────────
        # Clicking this opens the AudioFxPanel for this MIDI track so the user
        # can add C++ insert effects to its audio output.
        fx_btn_x = self.HEADER_WIDTH - 34
        fx_btn_y = y + h - 18
        fx_btn_w = 28
        fx_btn_h = 14
        p.fillRect(fx_btn_x, fx_btn_y, fx_btn_w, fx_btn_h,
                   QColor(0, 140, 160, 160))
        p.setPen(QColor(0, 229, 255, 220))
        p.setFont(QFont("Arial", 7, QFont.Bold))
        p.drawText(fx_btn_x + 2, fx_btn_y + fx_btn_h - 3, "FX")
        p.setPen(QPen(QColor(0, 200, 220, 120), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(fx_btn_x, fx_btn_y, fx_btn_w, fx_btn_h)

        # Timeline background
        p.fillRect(self.HEADER_WIDTH, y, w - self.HEADER_WIDTH, h,
                   QColor(C["abyss"]))

        # Faint beat grid when empty
        if not track.clips:
            p.setPen(QPen(QColor(0, 229, 255, 7), 1))
            beat = 0
            while beat <= self._total_beats:
                x = self._beat_to_x(beat)
                if self.HEADER_WIDTH <= x <= w:
                    p.drawLine(x, y, x, y + h)
                beat += 4

        # Clip blocks
        for clip in track.clips:
            self._draw_clip_block(p, clip, track, y, h)

        p.setPen(QPen(QColor(0, 229, 255, 14), 1))
        p.drawLine(0, y + h - 1, w, y + h - 1)

    def _draw_clip_block(self, p: QPainter, clip: MidiClip,
                         track: MidiTrack, y: int, h: int) -> None:
        color = QColor(track.color)
        is_sel = (clip.clip_id == self._selected_clip_id)

        bx = self._beat_to_x(clip.start_beat)
        bw = max(20, int(clip.duration * self.BEAT_WIDTH))

        alpha_bg  = 60 if is_sel else 28
        alpha_bdr = 220 if is_sel else 110
        bg_col  = QColor(color.red(), color.green(), color.blue(), alpha_bg)
        bdr_col = QColor(color.red(), color.green(), color.blue(), alpha_bdr)
        block = QRectF(bx, y + 3, bw, h - 6)
        p.fillRect(block, bg_col)
        p.setPen(QPen(bdr_col, 1.5 if is_sel else 1.0))
        p.drawRoundedRect(block, 4, 4)

        clip_label = clip.name if clip.name else track.name[:16]
        p.setFont(QFont("Arial", 8))
        p.setPen(QColor(color.red(), color.green(), color.blue(), 210))
        p.drawText(bx + 6, y + 16, clip_label[:20])

        # Mini note preview inside the clip block
        if clip.notes:
            pmin = min(n.pitch for n in clip.notes)
            pmax = max(n.pitch for n in clip.notes)
            span = max(pmax - pmin, 12)
            area_h = h - 20
            p.setPen(QPen(color, 1.5))
            for note in clip.notes:
                nx = bx + int(note.start_beat * self.BEAT_WIDTH)
                nw_px = max(2, int(note.duration * self.BEAT_WIDTH) - 1)
                nt = y + 8 + area_h - int((note.pitch - pmin) / span * area_h)
                if bx - 2 <= nx <= bx + bw + 2:
                    p.drawLine(nx, nt, min(nx + nw_px, bx + bw - 2), nt)

        # Resize grip indicator — bright line on right edge
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 160), 2))
        p.drawLine(bx + bw - 2, y + 6, bx + bw - 2, y + h - 6)

    def _draw_audio_lane(self, p: QPainter, atrack: AudioTrack,
                         y: int, w: int) -> None:
        h = self.AUDIO_ROW_HEIGHT
        color = QColor(atrack.color)

        # Header
        p.fillRect(0, y, self.HEADER_WIDTH, h, QColor(C["deep"]))
        p.fillRect(0, y + 1, 4, h - 2, color)
        p.setPen(color)
        p.setFont(QFont("Arial", 9, QFont.Bold))
        p.drawText(10, y + 18, atrack.name[:20])
        p.setPen(QColor(C["text_dim"]))
        p.setFont(QFont("Arial", 8))
        n_clips = len(atrack.clips)
        p.drawText(10, y + 32,
                   f"{n_clips} clip(s)  ·  right-click header to remove")

        # Timeline background
        p.fillRect(self.HEADER_WIDTH, y, w - self.HEADER_WIDTH, h,
                   QColor(C["abyss"]))

        # Audio clip blocks — width reflects actual audio duration
        secs_per_beat = 60.0 / max(20.0, self._bpm)
        for aclip in atrack.clips:
            cx = self._beat_to_x(aclip.start_beat)
            dur_b = (aclip.duration_seconds / secs_per_beat
                     if aclip.duration_seconds > 0 else 4.0)
            cw = max(40, int(dur_b * self.BEAT_WIDTH))
            cy, ch2 = y + 4, h - 8

            # Block fill + border
            p.fillRect(cx, cy, cw, ch2,
                       QColor(color.red(), color.green(), color.blue(), 28))
            p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 140), 1))
            p.drawRoundedRect(QRectF(cx, cy, cw, ch2), 3, 3)

            # ── Waveform rendering ─────────────────────────────────────
            # Request peaks from the background generator.  Returns None
            # while the daemon thread is still loading.
            peaks = (self._waveform_gen.get_peaks(aclip.path)
                     if self._waveform_gen is not None else None)

            wave_margin = 3
            wave_area_h = ch2 - 2 * wave_margin
            wave_mid    = cy + wave_margin + wave_area_h // 2
            r, g, b     = color.red(), color.green(), color.blue()

            if peaks and wave_area_h > 2 and cw > 0:
                n      = len(peaks)
                pk_arr = np.asarray(peaks, dtype=np.float32)

                # Only draw the VISIBLE portion of the waveform.
                # Drawing min(cw, 3000) pixels starting from cx caused a flat
                # line whenever the user scrolled past that fixed pixel budget.
                draw_x0 = max(cx, self.HEADER_WIDTH)        # left visible pixel
                draw_x1 = min(cx + cw, w)                   # right visible pixel
                if draw_x0 < draw_x1:
                    # Map the visible pixel range to the corresponding peak slice.
                    clip_px0    = draw_x0 - cx              # offset within clip
                    clip_px1    = draw_x1 - cx
                    pixel_count = min(draw_x1 - draw_x0, 3000)
                    peak_lo     = int(clip_px0 * n / cw)
                    peak_hi     = min(int(clip_px1 * n / cw) + 1, n)
                    indices     = np.clip(
                        np.linspace(peak_lo, max(peak_lo, peak_hi - 1),
                                    pixel_count).astype(np.int32),
                        0, n - 1,
                    )
                    pixel_peaks = pk_arr[indices]
                    halves      = np.maximum(
                        1, (pixel_peaks * wave_area_h * 0.48).astype(np.int32)
                    )
                    from PySide6.QtCore import QLine
                    lines = [
                        QLine(draw_x0 + px, wave_mid - int(halves[px]),
                              draw_x0 + px, wave_mid + int(halves[px]))
                        for px in range(pixel_count)
                    ]
                    p.setPen(QPen(QColor(r, g, b, 195), 1))
                    p.drawLines(lines)

                # Subtle centre reference line across the full clip width.
                p.setPen(QPen(QColor(255, 255, 255, 22), 1))
                p.drawLine(cx, wave_mid, cx + cw - 1, wave_mid)

            else:
                # File is loading or unreadable — draw a dim placeholder line.
                p.setPen(QPen(QColor(r, g, b, 45), 1))
                p.drawLine(cx + 2, wave_mid, cx + cw - 2, wave_mid)

            # Label + duration hint (drawn on top of waveform)
            p.setPen(color)
            p.setFont(QFont("Arial", 8))
            p.drawText(cx + 5, cy + 13, aclip.name[:20])
            if aclip.duration_seconds > 0:
                p.setPen(QColor(color.red(), color.green(), color.blue(), 150))
                p.setFont(QFont("Arial", 7))
                p.drawText(cx + 5, cy + ch2 - 3,
                           f"{aclip.duration_seconds:.1f}s")

            # Resize grip indicator — bright right edge
            p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 180), 2))
            p.drawLine(cx + cw - 2, cy + 3, cx + cw - 2, cy + ch2 - 3)

        p.setPen(QPen(QColor(0, 229, 255, 18), 1))
        p.drawLine(0, y + h - 1, w, y + h - 1)

    def _draw_playhead(self, p: QPainter, w: int) -> None:
        """
        Draw the real-time playhead as a bright neon vertical line across the
        full height of the arrangement timeline.

        The line is drawn AFTER all track content so it always appears on top.
        A small downward triangle sits in the ruler to mark the exact beat.
        Only drawn when the playhead x-position falls within the visible area
        (i.e. to the right of the track-header column).
        """
        x = self._beat_to_x(self._playhead_beat)

        # Skip when outside the visible timeline area.
        if x < self.HEADER_WIDTH or x > w:
            return

        h = self.height()

        # ── Glow: wide translucent band to make the line easy to see ──
        p.setPen(QPen(QColor(0, 229, 255, 35), 5))
        p.drawLine(x, self.RULER_HEIGHT, x, h)

        # ── Main 1-px cyan line ──
        p.setPen(QPen(QColor(0, 229, 255, 220), 1))
        p.drawLine(x, self.RULER_HEIGHT, x, h)

        # ── Triangle cap in the ruler ──
        # PySide6 drawPolygon requires a QPolygonF; splatting a plain list raises TypeError.
        p.setBrush(QColor(0, 229, 255, 200))
        p.setPen(Qt.NoPen)
        tri = QPolygonF([
            QPointF(x - 5, 0.0),
            QPointF(x + 5, 0.0),
            QPointF(float(x), 11.0),
        ])
        p.drawPolygon(tri)

    # ── Mouse events ────────────────────────────────────────────────────

    def mousePressEvent(self, ev) -> None:
        mx, my = ev.x(), ev.y()

        if my < self.RULER_HEIGHT:
            if ev.button() == Qt.LeftButton and mx > self.HEADER_WIDTH:
                beat = max(0.0, self._x_to_beat(mx))
                self._ruler_pressed    = True
                self._ruler_press_x    = mx
                self._ruler_press_beat = beat
                self._ruler_loop_origin = None
                self.update()
            return

        # MIDI track rows
        for idx, track in enumerate(self._tracks):
            y = self._midi_track_y(idx)
            if not (y <= my < y + self.TRACK_HEIGHT):
                continue

            if ev.button() == Qt.RightButton and mx < self.HEADER_WIDTH:
                menu = QMenu(self)
                instr_act  = menu.addAction("Change Instrument…")
                menu.addSeparator()
                remove_act = menu.addAction("Remove Track")
                action = menu.exec(ev.globalPosition().toPoint())
                if action == instr_act:
                    self.change_instrument_requested.emit(track.channel)
                elif action == remove_act:
                    self.remove_track_requested.emit(track.channel)
                return

            if ev.button() == Qt.LeftButton and mx < self.HEADER_WIDTH:
                # Check whether the click landed on the FX button.
                # The button rect mirrors what _draw_midi_lane() paints.
                h_lane    = self.TRACK_HEIGHT
                fx_btn_x  = self.HEADER_WIDTH - 34
                fx_btn_y  = y + h_lane - 18
                fx_btn_x2 = fx_btn_x + 28
                fx_btn_y2 = fx_btn_y + 14
                # Check the AUTO button (to the left of the FX button)
                auto_btn_x  = self.HEADER_WIDTH - 68
                auto_btn_x2 = auto_btn_x + 28
                if fx_btn_x <= mx <= fx_btn_x2 and fx_btn_y <= my <= fx_btn_y2:
                    # Open the FX rack panel for this MIDI track.
                    self.midi_fx_requested.emit(track.channel)
                elif auto_btn_x <= mx <= auto_btn_x2 and fx_btn_y <= my <= fx_btn_y2:
                    # Toggle the automation lane for this MIDI track.
                    if track.channel in self._auto_tracks:
                        self._auto_tracks.discard(track.channel)
                    else:
                        self._auto_tracks.add(track.channel)
                    self.automation_toggled.emit(track.channel, "midi")
                    self.update()
                else:
                    # Normal header click: select track for piano roll.
                    self.track_selected.emit(track.channel)
                return

            _, clip, _, hit = self._find_clip_at(mx, my)

            if clip is not None and ev.button() == Qt.LeftButton:
                self._selected_clip_id = clip.clip_id
                self.clip_selected.emit(track.channel, clip.clip_id)
                if hit == "resize":
                    self._clip_drag_mode     = "resize"
                    self._drag_kind          = "midi"
                    self._drag_channel       = track.channel
                    self._drag_track_key     = track.channel
                    self._drag_clip_id       = clip.clip_id
                    self._drag_clip_orig_dur = clip.duration
                    self._drag_start_x       = mx
                else:
                    self._clip_drag_mode       = "move"
                    self._drag_kind            = "midi"
                    self._drag_channel         = track.channel
                    self._drag_track_key       = track.channel
                    self._drag_clip_id         = clip.clip_id
                    self._drag_clip_orig_start = clip.start_beat
                    self._drag_beat_offset     = self._x_to_beat(mx) - clip.start_beat
                    self._drag_start_x         = mx
                self.update()
                return

            if clip is not None and ev.button() == Qt.RightButton:
                menu = QMenu(self)
                del_act = menu.addAction("Delete Clip")
                dup_act = menu.addAction("Duplicate Clip")
                action  = menu.exec(ev.globalPosition().toPoint())
                if action == del_act:
                    self.clip_deleted.emit(track.channel, clip.clip_id)
                elif action == dup_act:
                    self.clip_duplicated.emit(track.channel, clip.clip_id)
                return

            # Click on empty track timeline → create new clip
            if ev.button() == Qt.LeftButton:
                beat = max(0.0, self._x_to_beat(mx))
                self.clip_create_requested.emit(track.channel, beat)
                self.track_selected.emit(track.channel)
            return

        # Audio track rows
        for idx, atrack in enumerate(self._audio_tracks):
            y = self._audio_track_y(idx)
            if not (y <= my < y + self.AUDIO_ROW_HEIGHT):
                continue

            # Left-click on header -> select track (show its FX panel).
            if ev.button() == Qt.LeftButton and mx < self.HEADER_WIDTH:
                self.audio_track_selected.emit(atrack.track_id)
                return

            # Right-click track header -> context menu.
            if ev.button() == Qt.RightButton and mx < self.HEADER_WIDTH:
                menu = QMenu(self)
                rem_act = menu.addAction("Remove Audio Track")
                action = menu.exec(ev.globalPosition().toPoint())
                if action == rem_act:
                    self.audio_track_remove_requested.emit(atrack.track_id)
                return

            _, aclip, _, hit = self._find_clip_at(mx, my)

            if aclip is not None and ev.button() == Qt.LeftButton:
                if hit == "resize":
                    secs_per_beat = 60.0 / max(20.0, self._bpm)
                    self._clip_drag_mode     = "resize"
                    self._drag_kind          = "audio"
                    self._drag_track_key     = atrack.track_id
                    self._drag_clip_id       = aclip.clip_id
                    self._drag_clip_orig_dur = aclip.duration_seconds
                    self._drag_start_x       = mx
                else:
                    self._clip_drag_mode       = "move"
                    self._drag_kind            = "audio"
                    self._drag_track_key       = atrack.track_id
                    self._drag_clip_id         = aclip.clip_id
                    self._drag_clip_orig_start = aclip.start_beat
                    self._drag_beat_offset     = self._x_to_beat(mx) - aclip.start_beat
                    self._drag_start_x         = mx
                self.update()
                return

            if aclip is not None and ev.button() == Qt.RightButton:
                menu = QMenu(self)
                dup_act = menu.addAction("Duplicate Clip")
                del_act = menu.addAction("Delete Clip")
                action  = menu.exec(ev.globalPosition().toPoint())
                if action == dup_act:
                    self.audio_clip_duplicated.emit(atrack.track_id, aclip.clip_id)
                elif action == del_act:
                    self.audio_clip_deleted.emit(atrack.track_id, aclip.clip_id)
                return

            return

    def mouseMoveEvent(self, ev) -> None:
        mx = ev.x()

        # Ruler: once dragged > 4px, switch from seek to loop-creation
        if self._ruler_pressed and abs(mx - self._ruler_press_x) > 4:
            if self._ruler_loop_origin is None:
                self._ruler_loop_origin = self._ruler_press_beat
                self._loop_start = self._ruler_loop_origin
                self._loop_end   = self._ruler_loop_origin + 0.25
            beat = max(0.0, round(self._x_to_beat(mx) * 4) / 4)
            origin = self._ruler_loop_origin
            self._loop_start = min(origin, beat)
            self._loop_end   = max(origin + 0.25, beat)
            self.update()
            return

        if self._clip_drag_mode == "move" and self._drag_clip_id >= 0:
            new_beat = max(0.0, self._x_to_beat(mx) - self._drag_beat_offset)
            new_beat = round(new_beat)   # snap to whole beat
            if self._drag_kind == "audio":
                self.audio_clip_moved.emit(self._drag_track_key,
                                           self._drag_clip_id, float(new_beat))
            else:
                self.clip_moved.emit(self._drag_channel, self._drag_clip_id,
                                     float(new_beat))
            self.update()
            return

        if self._clip_drag_mode == "resize" and self._drag_clip_id >= 0:
            dx_beats = (mx - self._drag_start_x) / self.BEAT_WIDTH
            if self._drag_kind == "audio":
                secs_per_beat = 60.0 / max(20.0, self._bpm)
                dx_secs = dx_beats * secs_per_beat
                new_dur = max(0.1, self._drag_clip_orig_dur + dx_secs)
                self.audio_clip_resized.emit(self._drag_track_key,
                                             self._drag_clip_id, new_dur)
            else:
                new_dur = max(1.0, round(self._drag_clip_orig_dur + dx_beats))
                self.clip_resized.emit(self._drag_channel, self._drag_clip_id,
                                       float(new_dur))
            self.update()
            return

        # Update cursor to hint at resize grip
        _, clip, _, hit = self._find_clip_at(mx, ev.y())
        if clip is not None and hit == "resize":
            self.setCursor(Qt.SizeHorCursor)
        elif clip is not None:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, ev) -> None:
        if self._ruler_pressed:
            if self._ruler_loop_origin is not None:
                # Drag ended: emit loop region
                if self._loop_end - self._loop_start >= 0.25:
                    self.loop_region_changed.emit(self._loop_start, self._loop_end)
            else:
                # Simple click: seek
                self.seek_requested.emit(self._ruler_press_beat)
            self._ruler_pressed     = False
            self._ruler_loop_origin = None
            self.update()
            return

        # Simple click on a MIDI clip (no drag/resize) → open piano roll
        if (ev.button() == Qt.LeftButton
                and self._clip_drag_mode == "move"
                and self._drag_kind == "midi"
                and self._drag_clip_id >= 0
                and self._drag_channel >= 0
                and abs(ev.x() - self._drag_start_x) < 5):
            self.clip_edit_requested.emit(self._drag_channel, self._drag_clip_id)

        self._clip_drag_mode = "none"
        self._drag_clip_id   = -1
        self._drag_track_key = -1
        self._drag_channel   = -1
        self._drag_kind      = "midi"
        self.setCursor(Qt.ArrowCursor)

    def wheelEvent(self, ev) -> None:
        dx = ev.angleDelta().x()
        dy = ev.angleDelta().y()
        if ev.modifiers() & Qt.ControlModifier:
            # Ctrl+scroll -> zoom
            if dy > 0:
                self.zoom_in()
            elif dy < 0:
                self.zoom_out()
            return
        delta = dx if abs(dx) > abs(dy) else -dy
        self._view_x = max(0.0, self._view_x + delta / 120)
        self.scroll_x_changed.emit(self._view_x)
        self.update()

    # -- Drag-and-drop --------------------------------------------------------

    def dragEnterEvent(self, ev) -> None:
        """
        Accept a drag event when at least one dragged URL is a known MIDI or
        audio file. The cursor changes to a copy icon to signal acceptance.
        """
        if ev.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in ev.mimeData().urls()]
            if any(detect_file_type(p) is not None for p in paths):
                ev.acceptProposedAction()
                return
        ev.ignore()

    def dragMoveEvent(self, ev) -> None:
        """
        Keep accepting the drag as the cursor moves over the widget so the OS
        maintains the copy cursor. No visual beat-line is drawn here to keep
        the implementation simple.
        """
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev) -> None:
        """
        Collect dropped file paths, compute the beat position from the cursor
        X coordinate, and emit files_dropped(paths, beat).

        The MainWindow slot _on_files_dropped() does the actual importing so
        this widget stays decoupled from the data layer.
        """
        if not ev.mimeData().hasUrls():
            ev.ignore()
            return

        paths = [u.toLocalFile() for u in ev.mimeData().urls()]
        paths = [p for p in paths if detect_file_type(p) is not None]

        if not paths:
            ev.ignore()
            return

        # Translate cursor X into a beat position (snapped to whole beats).
        beat = max(0.0, round(self._x_to_beat(ev.position().x())))
        ev.acceptProposedAction()
        self.files_dropped.emit(paths, beat)


# ═══════════════════════════════════════════════════════════════════════════
# Main Window
# ═══════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    """
    Crystal-themed top-level window.

    New features vs previous revision:
        • Right dock: EffectsPanel (per-instrument EQ/Reverb/Comp/Chorus).
        • Piano roll: ghost-note display for non-active tracks.
        • Composition-mode toggle on transport bar.
        • WAV export via FluidSynth CLI.
        • New Project — clears everything and resets.
        • Effect chains stored per-channel in _effect_chains dict.
    """

    def __init__(
        self,
        engine: AudioEngine,
        midi: MidiLogic,
        controller: ControllerManager,
    ) -> None:
        super().__init__()
        self._engine       = engine
        self._midi         = midi
        self._controller   = controller
        self._vst_manager  = VstManager()           # VST plugin host
        self._selected_channel: int = 0
        self._active_clip: Optional[MidiClip] = None   # clip open in piano roll
        self._effect_chains: Dict[int, EffectChain] = {}
        self._pygame_sounds: Dict[str, object] = {}
        self._gamepad_timer: Optional[QTimer] = None   # drives controller.poll_once()
        self._instrument_names: Dict[int, str] = {}    # channel -> current preset name
        self._sfz_engines:     Dict[int, object] = {}  # channel -> SfizRealTimePlayer
        self._ds_engines:      Dict[int, object] = {}  # channel -> DsRealTimePlayer
        self._note_clipboard:  List[dict]     = []    # copy/paste buffer for piano roll notes

        # Per-audio-track FX engine and FX chain registry.
        self._audio_player  = AudioFilePlayer()
        self._audio_fx_chains: Dict[int, AudioFxChain] = {}  # track_id -> chain

        # Per-MIDI-track FX chain registry (keyed by MIDI channel number).
        # Each entry is an AudioFxChain whose plugins may include instrument
        # plugins (e.g. SamplerPlugin) as well as C++ insert effects.
        self._midi_fx_chains: Dict[int, AudioFxChain] = {}   # channel -> chain

        # Per-MIDI-track real-time renderers for instrument plugins.
        # An InstrumentRenderer owns a sounddevice OutputStream that renders
        # the matching AudioFxChain in real time.
        self._instrument_renderers: Dict[int, object] = {}   # channel -> renderer

        # Centralised import helper.
        self._import_manager = ImportManager()

        # Channel Rack window (created early so _wire_signals can connect it).
        self._channel_rack = ChannelRackWindow(self)
        # Populate with a default set of drum rows.
        # Each row uses a unique row_id (assigned automatically, ≥ 32) so that
        # Copy-to-Timeline creates one separate MidiTrack per drum.
        _default_rack_rows = [
            ChannelStepData(name="Kick",    channel=9, note=36, velocity=110),
            ChannelStepData(name="Snare",   channel=9, note=38, velocity=100),
            ChannelStepData(name="Hi-Hat",  channel=9, note=42, velocity=80),
            ChannelStepData(name="Open HH", channel=9, note=46, velocity=75),
        ]
        self._channel_rack.set_rows(_default_rack_rows)
        # Register the shared step-row list with MidiLogic so events are baked
        # into _build_flat_events() for sample-accurate timing.
        self._midi.set_step_rows(self._channel_rack.get_rows())

        # Per-row sample engine for the Channel Rack.
        # Each rack row can have an audio sample loaded; this engine plays it
        # in real-time when a step fires (independently of FluidSynth).
        self._rack_engine = RackSamplerEngine()

        # Mapping of rack row_id → MIDI channel used as FluidSynth fallback
        # when no sample is loaded for that row.
        self._rack_row_fluid_ch: dict = {
            row.row_id: row.channel for row in _default_rack_rows
        }
        # Mapping of rack row_id → root MIDI note for pitch-shifted sample playback.
        self._rack_row_note: dict = {
            row.row_id: row.note for row in _default_rack_rows
        }

        # Waveform peak generator — loads audio peaks on daemon threads so the
        # arrange view can render waveforms without blocking the GUI.
        self._waveform_gen = WaveformPeakGenerator()

        # Per-channel velocity humanizers: channel → VelocityHumanizer instance.
        # Created on demand when the user enables humanization for a track.
        self._midi_humanizers: dict = {}
        # Saved humanizer parameter dicts: channel → params dict.
        # Persisted here so settings survive track deselection / panel reloads.
        self._humanizer_params: dict = {}

        # Export worker reference -- kept alive to prevent mid-run GC.
        self._export_worker = None

        # Real-time audio telemetry — TelemetryManager owns the C++ analyzer,
        # five QDockWidget panels, and the 30-FPS polling loop.
        self._telemetry = TelemetryManager(self)

        # C++ timeline engine bridge — handles MIDI scheduling and audio mixing.
        # Initialised before the toolbar so BPM changes from the transport bar
        # can propagate to the bridge immediately.
        self._timeline_bridge = None

        self._init_audio_clip_playback()
        self._init_timeline_bridge()

        self.setWindowTitle("SBS-Synth Master  ✦  Crystal DAW")
        self.setMinimumSize(1200, 750)
        self.setStyleSheet(STYLESHEET)
        self.setFocusPolicy(Qt.StrongFocus)

        self._setup_toolbar()
        self._setup_transport()
        self._setup_piano_roll()
        self._setup_mixer()
        self._setup_effects_dock()
        self._setup_status_bar()
        self._wire_signals()
        self._telemetry.setup_docks()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(50)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start()

    # ------------------------------------------------------------------
    # Audio clip playback initialisation
    # ------------------------------------------------------------------

    def _init_audio_clip_playback(self) -> None:
        """
        Wire audio-clip callbacks into MidiLogic.  pygame.mixer is intentionally
        skipped — sounddevice already owns CoreAudio on macOS (and WASAPI on
        Windows), so initialising pygame.mixer would block indefinitely.
        AudioFilePlayer falls back to sounddevice when pygame is unavailable.
        """
        self._midi.set_audio_callback(self._play_audio_file)
        self._midi.set_stop_audio_callback(self._audio_player.stop_all)
        logger.info("Audio clip playback wired (sounddevice backend).")

    def _init_timeline_bridge(self) -> None:
        """
        Create and attach the C++ TimelineEngineBridge to MidiLogic.

        On success the bridge owns the sounddevice output stream and all
        MIDI events from the C++ engine are routed through _note_event_callback.
        If the C++ extension is not built the bridge degrades to a no-op and
        MidiLogic falls back to its Python playback loop transparently.
        """
        try:
            from .timeline_engine_bridge import TimelineEngineBridge
            bpm = float(self._midi.bpm)
            bridge = TimelineEngineBridge(sample_rate=44100, bpm=bpm)
            bridge.set_midi_dispatch(self._note_event_callback)
            if bridge.is_available:
                bridge.open_stream()
            self._timeline_bridge = bridge
            self._midi.attach_bridge(bridge)
            if bridge.is_available:
                logger.info("C++ TimelineEngineBridge active — "
                            "using hardware-accelerated audio/MIDI scheduling.")
            else:
                logger.info("TimelineEngineBridge created in no-op mode — "
                            "C++ extension not built; Python playback loop in use.")
        except Exception as exc:
            logger.warning("_init_timeline_bridge failed: %s", exc)

    def _play_audio_file(self, track_id: int, path: str,
                         duration_secs: float = 0.0,
                         start_offset_secs: float = 0.0) -> None:
        """
        Audio callback fired from the MidiLogic playback thread.

        Routes playback through AudioFilePlayer so per-track DSP effects
        (EQ, reverb, compressor, chorus, volume, pan) are applied before
        the audio reaches the pygame mixer channel.

        Args:
            track_id          : AudioTrack.track_id -- selects the FX chain.
            path              : Absolute path to the audio file.
            duration_secs     : Maximum play time (0 = full file).
            start_offset_secs : Seconds to skip at the start of the file.
                                Non-zero when the user seeked into a clip.
        """
        self._audio_player._play_clip_from_offset(
            track_id, path, duration_secs, start_offset_secs)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_toolbar(self) -> None:
        tb = QToolBar("Actions")
        tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        def _btn(label: str, tip: str, slot,
                 color: str = C["cyan"]) -> QPushButton:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(32)
            b.setStyleSheet(
                f"QPushButton {{ background:{C['deep']}; color:{color};"
                f" border:1px solid rgba(0,229,255,0.2); border-radius:5px;"
                f" padding:0 12px; }}"
                f"QPushButton:hover {{ border-color:{color};"
                f" background:rgba(0,229,255,0.1); color:{color}; }}"
            )
            b.clicked.connect(slot)
            return b

        tb.addWidget(_btn("✦ NEW PROJECT",  "Clear all tracks and start fresh",
                          self._on_new_project, C["purple"]))
        tb.addWidget(_btn("💾 SAVE PROJECT", "Save full project to .dawproj",
                          self._on_save_project, C["cyan"]))
        tb.addWidget(_btn("📂 LOAD PROJECT", "Load a .dawproj project file",
                          self._on_load_project, C["cyan"]))
        tb.addSeparator()
        # Channel Rack placed early so it is always visible even on narrow screens.
        tb.addWidget(_btn("🎹 CHANNEL RACK",
                          "Open / close the step sequencer (Channel Rack)",
                          self._on_toggle_channel_rack, C["pink"]))
        tb.addSeparator()
        tb.addWidget(_btn("+ ADD TRACK",    "Add a new instrument track",
                          self._on_add_track))
        tb.addSeparator()
        tb.addWidget(_btn("💾 SAVE MIDI",   "Export tracks to .mid",
                          self._on_save_midi))
        tb.addWidget(_btn("📂 LOAD MIDI",
                          "Import MIDI file(s) — each track gets its own "
                          "GM instrument automatically. Use NEW PROJECT first "
                          "to replace the current project.",
                          self._on_import_midi_files, C["cyan"]))
        tb.addWidget(_btn("🎵+ IMPORT AUDIO",
                          "Add multiple audio files — each gets its own track",
                          self._on_import_audio_files, C["gold"]))
        tb.addWidget(_btn("🎹 GM DEFAULTS",
                          "Set default SFZ / VST3 instruments for MIDI drag-and-drop",
                          self._on_gm_defaults, C["purple"]))
        tb.addWidget(_btn("🎵 EXPORT",      "Export to WAV or MP3",
                          self._on_export, C["gold"]))
        tb.addWidget(_btn("🎚 MASTER",      "Multi-format mastering export (MP3 / WAV / Stems)",
                          self._on_master_export, C["cyan"]))
        tb.addSeparator()
        tb.addWidget(_btn("🎮 GAMEPAD",     "Connect PS5 DualSense",
                          self._on_connect_gamepad))
        tb.addWidget(_btn("⌨ KEY MAP",      "Show keyboard mapping",
                          self._on_show_map))

    def _setup_transport(self) -> None:
        self._transport = TransportBar()
        dock = QDockWidget("TRANSPORT", self)
        dock.setWidget(self._transport)
        dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        self.addDockWidget(Qt.TopDockWidgetArea, dock)

    def _setup_piano_roll(self) -> None:
        self._piano_roll   = PianoRollWidget()
        self._arrange_view = TrackArrangeView()
        # Wire the background waveform loader so audio clips show peaks.
        self._arrange_view.set_waveform_generator(self._waveform_gen)
        self._clip_lane    = AudioClipLane()   # data backend kept alive

        # ── Page 0: Arrangement view ────────────────────────────────
        arrange_scroll = QScrollArea()
        arrange_scroll.setWidget(self._arrange_view)
        arrange_scroll.setWidgetResizable(True)
        arrange_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        arrange_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        arrange_scroll.setStyleSheet(f"background:{C['abyss']}; border:none;")

        self._arrange_hscroll = QScrollBar(Qt.Horizontal)
        self._arrange_hscroll.setRange(0, int(self._arrange_view._total_beats * 4))
        self._arrange_hscroll.setSingleStep(4)
        self._arrange_hscroll.setPageStep(32)
        self._arrange_hscroll.setStyleSheet(f"""
            QScrollBar:horizontal {{
                background:{C['abyss']}; height:12px; border:none;
            }}
            QScrollBar::handle:horizontal {{
                background:{C['cyan']}; border-radius:5px; min-width:24px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background:{C['pink']};
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width:0px;
            }}
        """)

        # Zoom toolbar for arrange view
        _zoom_btn_ss = (
            f"QPushButton {{ background:{C['deep']}; color:{C['cyan']};"
            f" border:1px solid rgba(0,229,255,0.25); border-radius:4px;"
            f" padding:0 10px; font-size:13px; font-weight:bold; }}"
            f"QPushButton:hover {{ border-color:{C['cyan']}; background:rgba(0,229,255,0.12); }}"
        )
        zoom_in_btn  = QPushButton("+")
        zoom_out_btn = QPushButton("−")
        zoom_in_btn .setFixedHeight(22)
        zoom_out_btn.setFixedHeight(22)
        zoom_in_btn .setFixedWidth(32)
        zoom_out_btn.setFixedWidth(32)
        zoom_in_btn .setStyleSheet(_zoom_btn_ss)
        zoom_out_btn.setStyleSheet(_zoom_btn_ss)
        zoom_in_btn .setToolTip("Zoom in  (Ctrl+scroll)")
        zoom_out_btn.setToolTip("Zoom out  (Ctrl+scroll)")
        zoom_in_btn .clicked.connect(self._arrange_view.zoom_in)
        zoom_out_btn.clicked.connect(self._arrange_view.zoom_out)

        zoom_lbl = QLabel("ZOOM")
        zoom_lbl.setStyleSheet(
            f"color:{C['text_dim']}; font-size:10px; background:transparent;")

        zoom_row = QWidget()
        zoom_row.setFixedHeight(26)
        zoom_row.setStyleSheet(
            f"background:{C['abyss']}; border-bottom:1px solid rgba(0,229,255,0.12);")
        zr_lay = QHBoxLayout(zoom_row)
        zr_lay.setContentsMargins(8, 2, 8, 2)
        zr_lay.setSpacing(4)
        zr_lay.addStretch()
        zr_lay.addWidget(zoom_lbl)
        zr_lay.addWidget(zoom_out_btn)
        zr_lay.addWidget(zoom_in_btn)

        # AutomationPanel sits in a vertical splitter directly below the
        # arrangement view.  It is hidden until the first automation lane
        # is opened via the AUTO button in a track header.
        self._auto_panel = AutomationPanel()

        # Vertical splitter: arrangement on top, automation panel below
        self._arrange_splitter = QSplitter(Qt.Vertical)
        self._arrange_splitter.setHandleWidth(4)
        self._arrange_splitter.setStyleSheet(
            f"QSplitter::handle {{ background:{C['surface']}; }}")
        self._arrange_splitter.addWidget(arrange_scroll)
        self._arrange_splitter.addWidget(self._auto_panel)
        # Give all available space to the arrangement view initially
        self._arrange_splitter.setSizes([600, 0])
        self._arrange_splitter.setCollapsible(0, False)

        arrange_panel = QWidget()
        arrange_panel.setStyleSheet(f"background:{C['abyss']};")
        ap_lay = QVBoxLayout(arrange_panel)
        ap_lay.setContentsMargins(0, 0, 0, 0)
        ap_lay.setSpacing(0)
        ap_lay.addWidget(zoom_row)
        ap_lay.addWidget(self._arrange_splitter, stretch=1)
        ap_lay.addWidget(self._arrange_hscroll)

        # ── Page 1: Piano roll ──────────────────────────────────────

        # Disable the QScrollArea's own vertical scrollbar.  _view_y in
        # PianoRollWidget drives rendering; the separate _piano_vscroll
        # QScrollBar widget (below) provides the visual scroll track that
        # spans the complete 128-note MIDI range.
        piano_scroll = QScrollArea()
        piano_scroll.setWidget(self._piano_roll)
        piano_scroll.setWidgetResizable(True)
        piano_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        piano_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        piano_scroll.setStyleSheet(f"background:{C['void']}; border:none;")
        self._piano_scroll = piano_scroll

        # Vertical scrollbar spanning the full MIDI range (C-1 … G9 = 0 … 127).
        # Range max = 127 so the thumb position matches view_y directly.
        self._piano_vscroll = QScrollBar(Qt.Vertical)
        self._piano_vscroll.setRange(
            0, PianoRollWidget.PITCH_MAX - PianoRollWidget.PITCH_MIN - 1)
        self._piano_vscroll.setSingleStep(1)
        self._piano_vscroll.setPageStep(20)
        self._piano_vscroll.setStyleSheet(f"""
            QScrollBar:vertical {{
                background:{C['abyss']}; width:12px; border:none;
            }}
            QScrollBar::handle:vertical {{
                background:{C['cyan']}; border-radius:5px; min-height:24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background:{C['pink']};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height:0px;
            }}
        """)

        # Wrapper: piano scroll area + manual scrollbar side-by-side
        piano_with_scroll = QWidget()
        piano_with_scroll.setStyleSheet(f"background:{C['void']};")
        phs_lay = QHBoxLayout(piano_with_scroll)
        phs_lay.setContentsMargins(0, 0, 0, 0)
        phs_lay.setSpacing(0)
        phs_lay.addWidget(piano_scroll, stretch=1)
        phs_lay.addWidget(self._piano_vscroll)

        # Back button + track title bar
        self._back_btn = QPushButton("← ARRANGEMENT")
        self._back_btn.setFixedHeight(28)
        self._back_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(0,229,255,0.08);"
            f" color:{C['cyan']}; border:1px solid rgba(0,229,255,0.35);"
            f" border-radius:4px; padding:0 10px; font-size:11px; }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.22); }}"
        )

        # VST editor button (visible only when a VST track is open)
        self._vst_edit_btn = QPushButton("⚙  VST EDITOR")
        self._vst_edit_btn.setFixedHeight(28)
        self._vst_edit_btn.setVisible(False)
        self._vst_edit_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(153,69,255,0.12);"
            f" color:{C['purple']}; border:1px solid rgba(153,69,255,0.45);"
            f" border-radius:4px; padding:0 10px; font-size:11px; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.32); }}"
        )

        self._piano_roll_title = QLabel("PIANO ROLL")
        self._piano_roll_title.setStyleSheet(
            f"color:{C['cyan']}; font-size:11px; font-weight:bold;"
            f" padding:0 12px; background:transparent;"
        )

        title_bar = QWidget()
        title_bar.setFixedHeight(34)
        title_bar.setStyleSheet(
            f"background:{C['abyss']};"
            f" border-bottom:1px solid rgba(0,229,255,0.2);"
        )
        # Draw / Select mode toggle
        self._draw_mode_btn = QPushButton("✏ DRAW")
        self._draw_mode_btn.setCheckable(True)
        self._draw_mode_btn.setChecked(True)
        self._draw_mode_btn.setFixedHeight(28)
        self._draw_mode_btn.setToolTip(
            "DRAW: click to add notes  |  SELECT: drag to select a group of notes")
        self._draw_mode_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(0,229,255,0.08); color:{C['cyan']};"
            f" border:1px solid rgba(0,229,255,0.35); border-radius:4px;"
            f" padding:0 10px; font-size:11px; }}"
            f"QPushButton:checked {{ background:rgba(0,229,255,0.22);"
            f" color:{C['cyan']}; border-color:{C['cyan']}; }}"
            f"QPushButton:!checked {{ background:rgba(153,69,255,0.1);"
            f" color:{C['purple']}; border-color:rgba(153,69,255,0.4); }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.18); }}"
        )
        self._draw_mode_btn.toggled.connect(self._on_draw_mode_toggled)

        # Grid settings panel — note values, triplets/dotted/Free, ruler mode.
        self._grid_panel = GridSettingsPanel()
        self._grid_panel.grid_changed.connect(self._piano_roll.set_grid_size)
        self._grid_panel.ruler_changed.connect(self._piano_roll.set_ruler_mode)
        self._grid_panel.fps_changed.connect(self._piano_roll.set_ruler_fps)

        tlay = QHBoxLayout(title_bar)
        tlay.setContentsMargins(6, 3, 6, 3)
        tlay.setSpacing(8)
        tlay.addWidget(self._back_btn)
        tlay.addWidget(self._vst_edit_btn)
        tlay.addWidget(self._piano_roll_title, stretch=1)
        tlay.addWidget(self._draw_mode_btn)
        tlay.addWidget(self._grid_panel)

        # Velocity lane toggle button
        self._vel_toggle_btn = QPushButton("≡ VEL")
        self._vel_toggle_btn.setCheckable(True)
        self._vel_toggle_btn.setChecked(True)
        self._vel_toggle_btn.setFixedHeight(28)
        self._vel_toggle_btn.setToolTip(
            "Show / hide the velocity lane below the piano roll")
        self._vel_toggle_btn.setStyleSheet(
            f"QPushButton {{ background:rgba(153,69,255,0.10); color:{C['purple']};"
            f" border:1px solid rgba(153,69,255,0.40); border-radius:4px;"
            f" padding:0 10px; font-size:11px; }}"
            f"QPushButton:checked {{ background:rgba(153,69,255,0.25);"
            f" color:{C['purple']}; border-color:{C['purple']}; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.22); }}"
        )
        tlay.addWidget(self._vel_toggle_btn)

        # ── Velocity lane ────────────────────────────────────────────
        self._velocity_lane = VelocityLaneWidget()
        self._velocity_lane.setFixedHeight(80)

        # Vertical splitter: piano roll on top, velocity lane on bottom
        self._piano_vel_splitter = QSplitter(Qt.Vertical)
        self._piano_vel_splitter.setHandleWidth(4)
        self._piano_vel_splitter.setStyleSheet(
            f"QSplitter::handle {{ background:rgba(153,69,255,0.30); }}"
            f"QSplitter::handle:hover {{ background:{C['purple']}; }}"
        )
        self._piano_vel_splitter.addWidget(piano_with_scroll)
        self._piano_vel_splitter.addWidget(self._velocity_lane)
        self._piano_vel_splitter.setStretchFactor(0, 1)
        self._piano_vel_splitter.setStretchFactor(1, 0)
        self._piano_vel_splitter.setSizes([600, 80])

        piano_panel = QWidget()
        piano_panel.setStyleSheet(f"background:{C['void']};")
        pvlay = QVBoxLayout(piano_panel)
        pvlay.setContentsMargins(0, 0, 0, 0)
        pvlay.setSpacing(0)
        pvlay.addWidget(title_bar)
        pvlay.addWidget(self._piano_vel_splitter, stretch=1)

        # ── Stacked widget ───────────────────────────────────────────
        self._view_stack = QStackedWidget()
        self._view_stack.addWidget(arrange_panel)   # index 0
        self._view_stack.addWidget(piano_panel)     # index 1
        self._view_stack.setCurrentIndex(0)

        central = QWidget()
        central.setStyleSheet(f"background:{C['void']};")
        vlay = QVBoxLayout(central)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)
        vlay.addWidget(self._view_stack)
        self.setCentralWidget(central)

    def _setup_mixer(self) -> None:
        self._mixer_strips: Dict[int, MixerStrip] = {}
        # Separate registry for audio-track strips so MIDI and audio strips
        # can be managed independently (different key spaces: channel vs track_id).
        self._audio_mixer_strips: Dict[int, AudioMixerStrip] = {}
        self._mixer_inner  = QWidget()
        self._mixer_inner.setStyleSheet(f"background:{C['abyss']};")
        self._mixer_layout = QHBoxLayout(self._mixer_inner)
        self._mixer_layout.setAlignment(Qt.AlignLeft)
        self._mixer_layout.setContentsMargins(6, 6, 6, 6)
        self._mixer_layout.setSpacing(3)

        mixer_scroll = QScrollArea()
        mixer_scroll.setWidget(self._mixer_inner)
        mixer_scroll.setWidgetResizable(True)
        mixer_scroll.setFixedHeight(310)
        mixer_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        mixer_scroll.setStyleSheet(f"background:{C['abyss']}; border:none;")

        # Create the C++ MasterBus (or Python fallback) and connect to the player.
        self._master_bus = get_master_bus(44100.0)
        self._audio_player.set_master_bus(self._master_bus)

        # Master bus channel strip — fixed width, always visible at the far right.
        self._master_bus_channel = MasterBusChannel(self._master_bus)

        # Thin vertical separator between track strips and master strip.
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color: rgba(0,229,255,0.25); background: rgba(0,229,255,0.12);")
        sep.setFixedWidth(2)

        # Outer container: scrollable track area + separator + master strip.
        mixer_outer = QWidget()
        mixer_outer.setStyleSheet(f"background:{C['abyss']};")
        outer_layout = QHBoxLayout(mixer_outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        outer_layout.addWidget(mixer_scroll, stretch=1)
        outer_layout.addWidget(sep)
        outer_layout.addWidget(self._master_bus_channel)

        dock = QDockWidget("MIXER", self)
        dock.setWidget(mixer_outer)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)

    def _setup_effects_dock(self) -> None:
        # -- MIDI instrument FX panel -----------------------------------------
        self._effects_panel = EffectsPanel()
        scroll = QScrollArea()
        scroll.setWidget(self._effects_panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(240)
        scroll.setStyleSheet(f"background:{C['abyss']}; border:none;")

        self._midi_fx_dock = QDockWidget("EFFECTS", self)
        self._midi_fx_dock.setWidget(scroll)
        self.addDockWidget(Qt.RightDockWidgetArea, self._midi_fx_dock)

        # -- Audio track FX panel ---------------------------------------------
        # Shown when the user clicks the FX button on an AudioMixerStrip.
        self._audio_fx_panel = AudioFxPanel()
        self._audio_fx_dock = QDockWidget("AUDIO FX", self)
        self._audio_fx_dock.setWidget(self._audio_fx_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self._audio_fx_dock)
        # Tabify the two right-side docks so they share the same space.
        self.tabifyDockWidget(self._midi_fx_dock, self._audio_fx_dock)

        # -- Velocity Humanizer dock ------------------------------------------
        # One shared HumanizerPanel reloaded per track.  Tabified with the
        # other right-side docks so it does not take extra screen space.
        self._humanizer_panel = HumanizerPanel()
        self._humanizer_dock  = QDockWidget("HUMANIZE", self)
        self._humanizer_dock.setWidget(self._humanizer_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self._humanizer_dock)
        self.tabifyDockWidget(self._audio_fx_dock, self._humanizer_dock)

        # -- Decent Sampler preset panel ------------------------------------
        # DsPresetPanel is a self-contained dock with a "Load .dspreset…"
        # button, dynamic knob/slider/button widgets, and MIDI CC routing.
        # The engine reference is set to None here; _start_ds_player() wires
        # in the real C++ DecentSamplerEngine once a track is loaded.
        self._ds_panel = DsPresetPanel(ds_engine=None, parent=self)
        self._ds_panel.parameter_changed.connect(self._on_ds_parameter_changed)
        self.addDockWidget(Qt.RightDockWidgetArea, self._ds_panel)
        self.tabifyDockWidget(self._humanizer_dock, self._ds_panel)

        # Default to showing the MIDI effects panel.
        self._midi_fx_dock.raise_()

    def _setup_status_bar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._st_engine = QLabel("Engine: —")
        self._st_scale  = QLabel("Scale: —")
        self._st_beat   = QLabel("♩ 0.00")
        for w in (self._st_engine, self._st_scale, self._st_beat):
            w.setStyleSheet(f"color:{C['text_dim']}; font-size:11px; padding:0 8px;")
        sb.addPermanentWidget(self._st_engine)
        sb.addPermanentWidget(self._st_scale)
        sb.addPermanentWidget(self._st_beat)

        rand_btn = QPushButton("🎲 Auto-Assign Instruments")
        rand_btn.setToolTip(
            "Randomly assign role-appropriate Soundfonts/presets\n"
            "to all loaded MIDI tracks for maximum variety."
        )
        rand_btn.setFixedHeight(22)
        rand_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:#FFD700;"
            f" border:1px solid rgba(255,215,0,0.35); border-radius:4px;"
            f" padding:0 8px; font-size:11px; }}"
            f"QPushButton:hover {{ border-color:#FFD700;"
            f" background:rgba(255,215,0,0.08); }}"
        )
        rand_btn.clicked.connect(self._on_randomize_instruments)
        sb.addPermanentWidget(rand_btn)

        ai_btn = QPushButton("🤖 AI MIX")
        ai_btn.setToolTip(
            "Apply a genre-specific FX template to all tracks\n"
            "(EQ · Compression · Reverb · Stereo · Spectral Panning)"
        )
        ai_btn.setFixedHeight(22)
        ai_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:#9945FF;"
            f" border:1px solid rgba(153,69,255,0.40); border-radius:4px;"
            f" padding:0 8px; font-size:11px; }}"
            f"QPushButton:hover {{ border-color:#9945FF;"
            f" background:rgba(153,69,255,0.10); }}"
        )
        ai_btn.clicked.connect(self._on_ai_mix)
        sb.addPermanentWidget(ai_btn)

        wav_master_btn = QPushButton("✨ Master WAV")
        wav_master_btn.setToolTip(
            "Master an external .wav file\n"
            "Genre EQ · M/S width · LUFS · Brickwall limiter"
        )
        wav_master_btn.setFixedHeight(22)
        wav_master_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:#00FF88;"
            f" border:1px solid rgba(0,255,136,0.35); border-radius:4px;"
            f" padding:0 8px; font-size:11px; }}"
            f"QPushButton:hover {{ border-color:#00FF88;"
            f" background:rgba(0,255,136,0.08); }}"
        )
        wav_master_btn.clicked.connect(self._on_auto_master)
        sb.addPermanentWidget(wav_master_btn)

        tel_btn = QPushButton("📊 TELEMETRY")
        tel_btn.setToolTip(
            "Show / hide real-time audio telemetry panels\n"
            "(Waveform · Freq Bands · Chroma · Waterfall · H/P)"
        )
        tel_btn.setFixedHeight(22)
        tel_btn.setStyleSheet(
            f"QPushButton {{ background:{C['deep']}; color:{C['lime']};"
            f" border:1px solid rgba(0,229,255,0.2); border-radius:4px;"
            f" padding:0 8px; font-size:11px; }}"
            f"QPushButton:hover {{ border-color:{C['lime']};"
            f" background:rgba(0,229,255,0.08); }}"
        )
        tel_btn.clicked.connect(self._telemetry.toggle)
        sb.addPermanentWidget(tel_btn)

    def _wire_signals(self) -> None:
        t = self._transport
        t.play_clicked.connect(self._on_play)
        t.stop_clicked.connect(self._on_stop)
        t.record_clicked.connect(self._on_record_toggled)
        t.bpm_changed.connect(lambda v: setattr(self._midi, "bpm", v))
        t.bpm_changed.connect(lambda v: self._timeline_bridge.set_bpm(v)
                              if self._timeline_bridge is not None else None)
        t.bpm_changed.connect(self._clip_lane.set_bpm)
        t.bpm_changed.connect(self._channel_rack.set_bpm)
        t.panic_btn.clicked.connect(self._engine.all_notes_off)
        t.scale_combo.currentTextChanged.connect(self._on_scale_changed)
        t.root_combo .currentTextChanged.connect(self._on_scale_changed)
        t.octave_spin.valueChanged.connect(self._on_octave_changed)
        # D-pad octave changes come from the poll thread — use singleShot to
        # safely marshal the update back onto the GUI thread.
        self._controller.on_octave_changed = lambda o: QTimer.singleShot(
            0, lambda val=o: t.octave_spin.setValue(val)
        )
        t.bpm_changed.connect(self._arrange_view.set_bpm)
        t.comp_mode_btn.toggled.connect(self._piano_roll.set_composition_mode)
        t.loop_btn.toggled.connect(self._on_loop_toggled)

        self._back_btn.clicked.connect(self._show_arrange_view)
        self._vst_edit_btn.clicked.connect(self._on_open_vst_editor)

        # Piano roll vertical scrollbar — bidirectional sync with _view_y.
        # view_y_changed fires from wheelEvent and jump_to_pitch so the scrollbar
        # thumb follows; valueChanged fires when the user drags the thumb so
        # the piano roll rendering follows.
        self._piano_roll.view_y_changed.connect(self._piano_vscroll.setValue)
        self._piano_vscroll.valueChanged.connect(self._on_piano_vscroll)

        self._piano_roll.note_added         .connect(self._on_note_added)
        self._piano_roll.note_removed       .connect(self._on_note_removed)
        self._piano_roll.note_moved         .connect(self._on_note_moved)
        self._piano_roll.note_resized       .connect(self._on_note_resized)
        self._piano_roll.loop_region_changed.connect(self._on_loop_region_changed)
        self._piano_roll.seek_requested     .connect(self._on_seek)
        self._piano_roll.scroll_x_changed   .connect(self._arrange_view.set_view_x)
        self._piano_roll.scroll_x_changed   .connect(self._clip_lane.set_view_x)
        self._piano_roll.scroll_x_changed   .connect(self._velocity_lane.set_view_x)

        self._velocity_lane.velocity_changed.connect(self._on_velocity_changed)
        self._vel_toggle_btn.toggled        .connect(self._on_vel_lane_toggled)

        self._arrange_view.track_selected             .connect(self._on_track_header_selected)
        self._arrange_view.audio_track_selected       .connect(self._on_audio_track_selected)
        self._arrange_view.files_dropped              .connect(self._on_files_dropped)
        self._arrange_view.clip_selected              .connect(self._on_clip_selected)
        self._arrange_view.clip_edit_requested        .connect(self._on_clip_edit_requested)
        self._arrange_view.clip_moved                 .connect(self._on_midi_clip_moved)
        self._arrange_view.clip_resized               .connect(self._on_midi_clip_resized)
        self._arrange_view.clip_deleted               .connect(self._on_midi_clip_deleted)
        self._arrange_view.clip_duplicated            .connect(self._on_midi_clip_duplicated)
        self._arrange_view.clip_create_requested      .connect(self._on_clip_create_requested)
        self._arrange_view.audio_track_remove_requested.connect(self._on_audio_track_remove)
        self._arrange_view.audio_clip_moved           .connect(self._on_audio_clip_moved)
        self._arrange_view.audio_clip_resized         .connect(self._on_audio_clip_resized)
        self._arrange_view.audio_clip_deleted         .connect(self._on_audio_clip_deleted)
        self._arrange_view.audio_clip_duplicated      .connect(self._on_audio_clip_duplicated)
        self._arrange_view.scroll_x_changed           .connect(self._piano_roll.set_view_x)
        self._arrange_view.scroll_x_changed           .connect(self._clip_lane.set_view_x)
        self._arrange_view.scroll_x_changed           .connect(self._on_arrange_scroll_x)
        self._arrange_view.loop_region_changed        .connect(self._on_loop_region_changed)
        self._arrange_view.seek_requested             .connect(self._on_seek)
        self._arrange_view.change_instrument_requested.connect(self._on_change_instrument)
        self._arrange_view.remove_track_requested     .connect(self._on_remove_track)
        self._arrange_view.midi_fx_requested          .connect(self._on_midi_track_fx_requested)
        self._arrange_hscroll.valueChanged            .connect(self._on_arrange_hscroll)
        # Automation panel scroll + zoom synchronisation
        self._arrange_view.scroll_x_changed .connect(self._auto_panel.set_view_x)
        self._arrange_view.zoom_changed     .connect(self._auto_panel.set_beat_width)
        # Automation toggle from track header AUTO button
        self._arrange_view.automation_toggled.connect(self._on_automation_toggled)
        # Channel Rack: note events route to RackSamplerEngine (or FluidSynth fallback)
        self._channel_rack.note_on_requested    .connect(self._on_rack_note_on)
        self._channel_rack.note_off_requested   .connect(self._on_rack_note_off)
        self._channel_rack.copy_requested       .connect(self._on_rack_copy_to_timeline)
        self._channel_rack.sample_load_requested.connect(self._on_rack_sample_loaded)

        # Audio FX panel: push chain updates to the player in real time.
        self._audio_fx_panel.chain_changed.connect(self._on_audio_fx_changed)

        # Humanizer panel: update the per-channel humanizer whenever the user
        # changes a slider or toggles enable/disable.
        self._humanizer_panel.params_changed.connect(self._on_humanizer_params_changed)

        self._clip_lane.clip_added    .connect(self._on_clip_added)
        self._clip_lane.clip_removed  .connect(self._on_clip_removed)
        self._clip_lane.scroll_changed.connect(self._on_clip_lane_scroll)

        self._midi.set_note_callback(self._note_event_callback)

    # ------------------------------------------------------------------
    # Track management
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Audio track FX slots
    # ------------------------------------------------------------------

    @Slot(int)
    def _on_audio_track_selected(self, track_id: int) -> None:
        """
        Show the AudioFxPanel for the selected audio track.

        Creates a neutral AudioFxChain on first selection so the panel always
        has something to display. The chain is registered with AudioFilePlayer
        so subsequent play_clip() calls use the correct DSP.
        """
        atrack = self._midi.get_audio_track(track_id)
        if atrack is None:
            return

        # Create a chain if this track has not been configured before.
        if track_id not in self._audio_fx_chains:
            chain = AudioFxChain(track_id=track_id)
            self._audio_fx_chains[track_id] = chain
            self._audio_player.register_track(track_id, chain)

        chain = self._audio_fx_chains[track_id]
        self._audio_fx_panel.load_chain(chain, atrack.name)
        # Bring the Audio FX tab to the front so the user can see the controls.
        self._audio_fx_dock.raise_()

        # Ensure the panel's change signal is wired to the audio-track handler.
        # If a MIDI track was previously selected, the signal may be connected
        # to _on_midi_fx_changed instead — swap back to the audio handler.
        try:
            self._audio_fx_panel.chain_changed.disconnect(self._on_midi_fx_changed)
        except Exception:
            pass
        try:
            self._audio_fx_panel.chain_changed.disconnect(self._on_audio_fx_changed)
        except Exception:
            pass
        self._audio_fx_panel.chain_changed.connect(self._on_audio_fx_changed)

    @Slot(int)
    def _on_midi_track_fx_requested(self, channel: int) -> None:
        """
        Show the AudioFxPanel for a MIDI instrument track.

        Creates an AudioFxChain for the channel on first request and starts
        an InstrumentRenderer so that instrument plugins (e.g. SamplerPlugin)
        in the rack produce real-time audio output.  Subsequent FX changes on
        this chain are handled by _on_midi_fx_changed().

        Args:
            channel: MIDI channel number (0-based) of the track.
        """
        track = self._midi.get_track(channel)
        if track is None:
            return

        # Create the FX chain for this MIDI channel if it doesn't exist yet.
        if channel not in self._midi_fx_chains:
            chain = AudioFxChain(track_id=channel)
            self._midi_fx_chains[channel] = chain

        chain = self._midi_fx_chains[channel]

        # Ensure a real-time InstrumentRenderer exists for this channel so
        # instrument plugins added to the chain produce audio output.
        if channel not in self._instrument_renderers:
            renderer = InstrumentRenderer()
            renderer.set_chain(chain)
            renderer.start()
            self._instrument_renderers[channel] = renderer
        else:
            # Renderer already exists — make sure it has the current chain ref.
            self._instrument_renderers[channel].set_chain(chain)

        # Load the chain into the shared AudioFxPanel and bring it forward.
        self._audio_fx_panel.load_chain(chain, track.name)
        self._audio_fx_dock.raise_()

        # Also populate the humanizer panel for this channel so the user can
        # switch to the HUMANIZE tab to configure velocity variation.
        self._humanizer_panel.load_channel(
            channel,
            track.name,
            self._humanizer_params.get(channel),
        )

        # Connect the panel's change signal to the MIDI-specific handler so
        # parameter edits don't accidentally restart audio-file playback.
        # (Disconnect first to avoid double-connecting on repeated clicks.)
        try:
            self._audio_fx_panel.chain_changed.disconnect(self._on_audio_fx_changed)
        except Exception:
            pass
        try:
            self._audio_fx_panel.chain_changed.disconnect(self._on_midi_fx_changed)
        except Exception:
            pass
        self._audio_fx_panel.chain_changed.connect(self._on_midi_fx_changed)

    @Slot(int)
    def _on_midi_fx_changed(self, channel: int) -> None:
        """
        Push updated MIDI-track FX chain to the InstrumentRenderer.

        Called whenever the user adds, removes, or tweaks a plugin in the FX
        rack while a MIDI track is selected.  The renderer's set_chain() call
        is cheap (single reference swap) and takes effect on the next audio
        block — no audio is interrupted.
        """
        chain = self._midi_fx_chains.get(channel)
        if chain is None:
            return

        renderer = self._instrument_renderers.get(channel)
        if renderer is not None:
            renderer.set_chain(chain)

        # Reconnect the panel's change signal back to the audio-track handler
        # when the user later selects an audio track (handled in
        # _on_audio_track_selected which calls load_chain and reconnects).

    @Slot(int)
    def _on_audio_fx_changed(self, track_id: int) -> None:
        """
        Push the updated AudioFxChain to AudioFilePlayer after any GUI edit.

        Changes take effect on the next clip playback. The player does not
        re-render already-playing audio (offline DSP model).
        Also syncs the AudioMixerStrip so its volume/pan/mute/solo sliders
        reflect any edits made via the AudioFxPanel (e.g. volume in the panel).
        """
        chain = self._audio_fx_chains.get(track_id)
        if chain is not None:
            self._audio_player.update_fx_chain(track_id, chain)
            strip = self._audio_mixer_strips.get(track_id)
            if strip is not None:
                strip.sync_from_chain(chain)

    @Slot(int, dict)
    def _on_humanizer_params_changed(self, channel: int, params: dict) -> None:
        """
        Called whenever the user moves a slider or toggles enable in the
        HumanizerPanel.  Saves the params and rebuilds the per-channel
        VelocityHumanizer instance so the next note event uses the new values.
        """
        # Persist the params dict so they survive panel reloads.
        self._humanizer_params[channel] = params

        if params.get("enabled", False):
            # Recreate the humanizer with the new parameters.
            # get_humanizer() tries C++ first, falls back to Python.
            self._midi_humanizers[channel] = get_humanizer(
                sigma             = params.get("sigma",             8.0),
                downbeat_boost    = params.get("downbeat_boost",    0.15),
                offbeat_reduction = params.get("offbeat_reduction", 0.08),
                time_sig_num      = params.get("time_sig_num",      4),
                time_sig_denom    = params.get("time_sig_denom",    4),
            )
        else:
            # Disabled — remove the instance so _note_event_callback skips it.
            self._midi_humanizers.pop(channel, None)

    def _register_audio_track_with_player(self, track_id: int) -> None:
        """
        Ensure an AudioTrack has a chain, is registered with AudioFilePlayer,
        and has an AudioMixerStrip visible in the mixer panel.

        Idempotent — safe to call multiple times for the same track_id.
        """
        # Create FX chain if this is the first registration.
        if track_id not in self._audio_fx_chains:
            chain = AudioFxChain(track_id=track_id)
            self._audio_fx_chains[track_id] = chain
        self._audio_player.register_track(
            track_id, self._audio_fx_chains[track_id])

        # Create mixer strip only once per track.
        if track_id not in self._audio_mixer_strips:
            atrack = self._midi.get_audio_track(track_id)
            name   = atrack.name  if atrack else f"Audio {track_id}"
            color  = atrack.color if atrack else C["gold"]

            strip = AudioMixerStrip(track_id, name, color)
            # Volume and pan feed directly into AudioFilePlayer / AudioFxChain.
            strip.volume_changed.connect(self._audio_player.set_volume)
            strip.pan_changed   .connect(self._audio_player.set_pan)
            strip.mute_toggled  .connect(self._audio_player.set_mute)
            strip.solo_toggled  .connect(self._audio_player.set_solo)
            # Remove button triggers the same slot as the arrangement-view header.
            strip.remove_clicked.connect(self._on_audio_track_remove)
            # FX button opens the AudioFxPanel in the side dock.
            strip.fx_clicked    .connect(self._on_audio_track_selected)

            strip.sync_from_chain(self._audio_fx_chains[track_id])
            self._audio_mixer_strips[track_id] = strip
            self._mixer_layout.addWidget(strip)

    # ------------------------------------------------------------------
    # Import slots (toolbar buttons and drag-drop)
    # ------------------------------------------------------------------

    @Slot(list, float)
    def _on_files_dropped(self, paths: list, beat: float) -> None:
        """
        Handle files dragged and dropped onto the arrangement view.

        MIDI files are appended as new tracks. Audio files each become a
        new audio track placed at the drop beat position.
        """
        if not paths:
            return

        midi_tracks, audio_tracks = self._import_manager.import_files(
            paths, self._midi, start_beat=beat, append=True,
        )

        # Register new MIDI tracks in the engine with a default instrument.
        for track in midi_tracks:
            chain = EffectChain(channel=track.channel)
            self._effect_chains[track.channel] = chain
            self._engine.apply_effect_chain(chain)   # override FluidSynth defaults
            col = C["tracks"][track.channel % len(C["tracks"])]
            track.color = col
            strip = self._make_mixer_strip(track.channel, track.name, col)
            self._mixer_strips[track.channel] = strip
            self._mixer_layout.addWidget(strip)

        # Auto-load SFZ instruments for every imported MIDI track.
        # _parse_midi_file extracts the GM program ID from the file and maps it
        # to an SFZ path via GmDefaultsManager.  We match payloads → MidiTrack
        # objects by track name (both readers use the same MIDI track_name meta).
        if midi_tracks:
            _gm_mgr    = GmDefaultsManager()
            _overrides = _gm_mgr.load()
            # Build a name→MidiTrack lookup for fast matching.
            _name_to_track = {t.name: t for t in midi_tracks}
            _sf2_files = AudioEngine.get_available_sf2_files()
            _sf2       = _sf2_files[0] if _sf2_files else ""
            midi_paths = [p for p in paths if detect_file_type(p) == "midi"]
            for mpath in midi_paths:
                result = _parse_midi_file(mpath, _overrides)
                if result is None:
                    continue
                _bpm, payloads = result
                for payload in payloads:
                    track = _name_to_track.get(payload.name)
                    if track is None:
                        continue
                    _entry = _gm_mgr.get_override_entry(payload.gm_program_id, _overrides)
                    if _entry:
                        _etype = _entry.get("type", "sfz")
                        _epath = _entry.get("path", "")
                        if _epath and os.path.isfile(_epath):
                            if _etype == "sfz":
                                self._start_sfz_player(track.channel, _epath)
                                _pl = self._sfz_engines.get(track.channel)
                                if _pl is not None:
                                    _pl._telemetry_push = self._telemetry.push_audio
                            elif _etype == "sf2":
                                _is_drums = (payload.gm_program_id == 128) or (track.channel == 9)
                                _ov = InstrumentPlugin(
                                    name=payload.name, sf2_path=_epath,
                                    bank=_entry.get("bank", 128 if _is_drums else 0),
                                    preset=_entry.get("preset", 0),
                                    channel=track.channel,
                                )
                                self._engine.register_instrument(_ov)
                        else:
                            logger.warning(
                                "_on_files_dropped: override file not found for track '%s' "
                                "(type=%s, path=%s)",
                                payload.name, _etype, _epath,
                            )
                    # Register a FluidSynth fallback so mastering export works.
                    if _sf2:
                        _drums  = (payload.gm_program_id == 128)
                        _preset = 0 if _drums else payload.gm_program_id
                        _fb     = InstrumentPlugin(
                            name=payload.name, sf2_path=_sf2,
                            bank=128 if _drums else 0, preset=_preset,
                            channel=track.channel,
                        )
                        self._engine.register_instrument(_fb)

        # Register new audio tracks with the player.
        for atrack in audio_tracks:
            self._register_audio_track_with_player(atrack.track_id)

        self._refresh_piano_roll()

    @Slot()
    def _on_import_midi_files(self) -> None:
        """
        Open a multi-file dialog for MIDI files and append them to the project.

        Each file's tracks are added without clearing existing tracks. Channel
        collisions are resolved automatically by ImportManager.
        """
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import MIDI Files (append to project)",
            os.path.expanduser("~"),
            "MIDI Files (*.mid *.midi);;All Files (*)",
        )
        if not paths:
            return

        new_tracks = self._import_manager.import_midi_files(
            paths, self._midi, append=True,
        )

        for track in new_tracks:
            chain = EffectChain(channel=track.channel)
            self._effect_chains[track.channel] = chain
            self._engine.apply_effect_chain(chain)   # override FluidSynth defaults
            col = C["tracks"][track.channel % len(C["tracks"])]
            track.color = col
            strip = self._make_mixer_strip(track.channel, track.name, col)
            self._mixer_strips[track.channel] = strip
            self._mixer_layout.addWidget(strip)

        # Auto-load SFZ instruments (same logic as _on_files_dropped).
        if new_tracks:
            _gm_mgr    = GmDefaultsManager()
            _overrides = _gm_mgr.load()
            _name_to_track = {t.name: t for t in new_tracks}
            _sf2_files = AudioEngine.get_available_sf2_files()
            _sf2       = _sf2_files[0] if _sf2_files else ""
            for mpath in paths:
                result = _parse_midi_file(mpath, _overrides)
                if result is None:
                    continue
                _bpm, payloads = result
                for payload in payloads:
                    track = _name_to_track.get(payload.name)
                    if track is None:
                        continue
                    _entry2 = _gm_mgr.get_override_entry(payload.gm_program_id, _overrides)
                    if _entry2:
                        _etype2 = _entry2.get("type", "sfz")
                        _epath2 = _entry2.get("path", "")
                        if _epath2 and os.path.isfile(_epath2):
                            if _etype2 == "sfz":
                                self._start_sfz_player(track.channel, _epath2)
                                _pl = self._sfz_engines.get(track.channel)
                                if _pl is not None:
                                    _pl._telemetry_push = self._telemetry.push_audio
                            elif _etype2 == "sf2":
                                _is_drums2 = (payload.gm_program_id == 128) or (track.channel == 9)
                                _ov2 = InstrumentPlugin(
                                    name=payload.name, sf2_path=_epath2,
                                    bank=_entry2.get("bank", 128 if _is_drums2 else 0),
                                    preset=_entry2.get("preset", 0),
                                    channel=track.channel,
                                )
                                self._engine.register_instrument(_ov2)
                    # Register a FluidSynth fallback so mastering export works
                    # even when the real-time player is SFZ / DS / VST3.
                    if _sf2:
                        _drums  = (payload.gm_program_id == 128)
                        _preset = 0 if _drums else payload.gm_program_id
                        _fb     = InstrumentPlugin(
                            name=payload.name, sf2_path=_sf2,
                            bank=128 if _drums else 0, preset=_preset,
                            channel=track.channel,
                        )
                        self._engine.register_instrument(_fb)

        self._refresh_piano_roll()

        if new_tracks:
            self._on_track_selected(new_tracks[0].channel)

    @Slot()
    def _on_import_audio_files(self) -> None:
        """
        Open a multi-file dialog for audio files.

        Every selected file becomes its own audio track placed at beat 0.
        The user can then drag the clip to the desired position.
        """
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Audio Files",
            os.path.expanduser("~"),
            "Audio Files (*.wav *.mp3 *.ogg *.flac *.aiff *.m4a);;All Files (*)",
        )
        if not paths:
            return

        new_audio_tracks = self._import_manager.import_audio_files(
            paths, self._midi, start_beat=0.0,
        )

        for atrack in new_audio_tracks:
            self._register_audio_track_with_player(atrack.track_id)

        self._refresh_piano_roll()

    def _make_mixer_strip(self, channel: int, name: str, color: str) -> "MixerStrip":
        """
        Create and wire a MixerStrip without adding a MIDI track.

        Used when importing MIDI files via ImportManager (the track already
        exists in midi_logic; only the UI strip needs to be created).
        """
        strip = MixerStrip(channel, name, color)
        strip.gain_changed  .connect(self._engine.set_gain)
        strip.pan_changed   .connect(self._engine.set_pan)
        strip.mute_toggled  .connect(self._engine.set_mute)
        strip.solo_toggled  .connect(self._engine.set_solo)
        strip.track_selected.connect(self._on_track_selected)
        strip.remove_clicked.connect(self._on_remove_track)
        strip.name_changed  .connect(self._on_track_renamed)
        return strip

    def add_track(self, track: MidiTrack, plugin: InstrumentPlugin) -> None:
        """Add a track to the sequencer, engine, and mixer UI."""
        self._midi.add_track(track)

        if plugin.sf2_path and os.path.isfile(plugin.sf2_path):
            self._engine.register_instrument(plugin)

        # Create default effect chain and push it to FluidSynth immediately.
        # Without this, FluidSynth runs with its built-in defaults (heavy reverb
        # at level ~0.9) until the user touches a slider, making the Effects panel
        # appear broken.
        self._effect_chains[track.channel] = EffectChain(channel=track.channel)
        self._engine.apply_effect_chain(self._effect_chains[track.channel])

        strip = MixerStrip(track.channel, track.name, track.color)
        strip.gain_changed  .connect(self._engine.set_gain)
        strip.pan_changed   .connect(self._engine.set_pan)
        strip.mute_toggled  .connect(self._engine.set_mute)
        strip.solo_toggled  .connect(self._engine.set_solo)
        strip.track_selected.connect(self._on_track_selected)
        strip.remove_clicked.connect(self._on_remove_track)
        strip.name_changed  .connect(self._on_track_renamed)

        # The initial track name IS the instrument preset name
        self._instrument_names[track.channel] = track.name
        strip.set_instrument_name(track.name)

        self._mixer_strips[track.channel] = strip
        self._mixer_layout.addWidget(strip)

        # Always activate the newly added track so recording targets it.
        self._active_clip = None
        self._piano_roll.set_active_clip(None)
        self._on_track_selected(track.channel)

        self._refresh_piano_roll()

    def _remove_track(self, channel: int) -> None:
        self._engine.all_notes_off(channel)
        self._engine.unregister_vst_player(channel)
        self._engine.unregister_instrument(channel)
        self._vst_manager.remove_track(channel)   # also stops real-time player
        # Stop and unregister any SFZ player on this channel.
        sfz_player = self._sfz_engines.pop(channel, None)
        if sfz_player is not None:
            self._engine.unregister_sfz_player(channel)
            try:
                sfz_player.stop()
            except Exception:
                pass

        # Stop and unregister any Decent Sampler player on this channel.
        ds_player = self._ds_engines.pop(channel, None)
        if ds_player is not None:
            self._engine.unregister_ds_player(channel)
            try:
                ds_player.stop()
            except Exception:
                pass

        self._midi.remove_track(channel)
        self._effect_chains.pop(channel, None)
        self._instrument_names.pop(channel, None)

        # Stop and discard any InstrumentRenderer for this channel so the
        # sounddevice stream is closed and its thread exits cleanly.
        renderer = self._instrument_renderers.pop(channel, None)
        if renderer is not None:
            renderer.stop()
        self._midi_fx_chains.pop(channel, None)

        strip = self._mixer_strips.pop(channel, None)
        if strip:
            self._mixer_layout.removeWidget(strip)
            strip.deleteLater()

        if self._selected_channel == channel:
            remaining = list(self._mixer_strips.keys())
            if remaining:
                self._on_track_selected(remaining[0])
            else:
                self._selected_channel = 0
                self._effects_panel.load_chain(
                    EffectChain(channel=0), self._engine, "No track")

        self._refresh_piano_roll()

    def _refresh_piano_roll(self) -> None:
        tracks = self._midi.get_all_tracks()
        self._piano_roll.set_tracks(tracks)
        self._arrange_view.set_tracks(tracks)
        self._arrange_view.set_audio_tracks(self._midi.get_audio_tracks())
        self._arrange_view.set_bpm(self._midi.bpm)

        # Extend beat range to cover all MIDI clips and audio tracks
        total = 32.0
        for track in tracks:
            for clip in track.clips:
                total = max(total, clip.start_beat + clip.duration + 8.0)
                for note in clip.notes:
                    total = max(total, clip.start_beat + note.start_beat + note.duration + 8.0)
        for atrack in self._midi.get_audio_tracks():
            for aclip in atrack.clips:
                dur_beats = aclip.duration_seconds * self._midi.bpm / 60.0
                total = max(total, aclip.start_beat + dur_beats + 8.0)
        self._piano_roll.set_total_beats(total)
        self._arrange_view.set_total_beats(total)
        self._arrange_hscroll.setMaximum(int(total * 4))
        self._refresh_velocity_lane()

    def _refresh_velocity_lane(self) -> None:
        """Sync the velocity lane with the current piano roll note data."""
        notes, color, _ = self._piano_roll._clip_notes_for_display()
        self._velocity_lane.set_notes_data(
            list(notes), color, self._piano_roll._active_channel
        )
        self._velocity_lane.set_view_x(self._piano_roll._view_x)
        self._velocity_lane.set_selected_ids(set(self._piano_roll._selected_ids))

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Velocity lane slots
    # ------------------------------------------------------------------

    @Slot(int, int, int)
    def _on_velocity_changed(self, channel: int, note_id: int, velocity: int) -> None:
        """Persist a velocity change from the velocity lane to the data model."""
        self._midi.set_note_velocity(note_id, velocity)
        # Refresh only the velocity lane — no need to rebuild the full piano roll
        self._refresh_velocity_lane()

    @Slot(bool)
    def _on_vel_lane_toggled(self, checked: bool) -> None:
        """Show or hide the velocity lane, expanding the piano roll to fill the space."""
        self._velocity_lane.setVisible(checked)
        if checked:
            total = self._piano_vel_splitter.height()
            if total > 0:
                vel_h = min(120, max(60, total // 6))
                self._piano_vel_splitter.setSizes([total - vel_h, vel_h])
            else:
                self._piano_vel_splitter.setSizes([600, 80])

    # ------------------------------------------------------------------
    # Loop slots
    # ------------------------------------------------------------------

    @Slot(bool)
    def _on_loop_toggled(self, enabled: bool) -> None:
        self._midi.set_loop_region(enabled, self._midi._loop_start, self._midi._loop_end)
        self._piano_roll  .set_loop_region(enabled, self._midi._loop_start, self._midi._loop_end)
        self._clip_lane   .set_loop_region(enabled, self._midi._loop_start, self._midi._loop_end)
        self._arrange_view.set_loop_region(enabled, self._midi._loop_start, self._midi._loop_end)

    @Slot(float, float)
    def _on_loop_region_changed(self, start: float, end: float) -> None:
        enabled = self._transport.loop_btn.isChecked()
        self._midi.set_loop_region(enabled, start, end)
        self._clip_lane   .set_loop_region(enabled, start, end)
        self._arrange_view.set_loop_region(enabled, start, end)
        self._piano_roll  .set_loop_region(enabled, start, end)
        # Enable loop automatically when user drags a region
        if not enabled:
            self._transport.loop_btn.setChecked(True)

    # ------------------------------------------------------------------
    # Audio clip slots
    # ------------------------------------------------------------------

    @Slot(str, float, float)
    def _on_clip_added(self, path: str, start_beat: float, duration_secs: float) -> None:
        name = os.path.splitext(os.path.basename(path))[0]
        self._midi.add_audio_track(path, start_beat, name=name,
                                   duration_seconds=duration_secs, color=C["gold"])
        self._clip_lane.set_clips(self._midi.get_clips())
        self._refresh_piano_roll()

    @Slot(int)
    def _on_clip_removed(self, clip_id: int) -> None:
        self._midi.remove_clip(clip_id)
        self._clip_lane.set_clips(self._midi.get_clips())
        self._refresh_piano_roll()

    @Slot(float)
    def _on_clip_lane_scroll(self, view_x: float) -> None:
        self._piano_roll._view_x = view_x
        self._piano_roll.update()

    # ------------------------------------------------------------------
    # Transport slots
    # ------------------------------------------------------------------

    @Slot()
    def _on_play(self) -> None:
        self._midi.play()
        self._st_engine.setText("Engine: ▶ PLAYING")

    @Slot()
    def _on_stop(self) -> None:
        self._midi.stop()
        # Stop all audio file tracks immediately — pygame channels keep playing
        # independently of the MidiLogic playback thread, so they must be halted
        # explicitly here alongside the MIDI stop.
        self._audio_player.stop_all()
        if self._midi._recording:
            self._midi.stop_recording()
            self._transport.record_btn.setChecked(False)
        self._st_engine.setText("Engine: ■ STOPPED")
        self._refresh_piano_roll()

    @Slot(bool)
    def _on_record_toggled(self, armed: bool) -> None:
        if armed:
            # If _active_clip belongs to a different track, ignore it so recording
            # always targets the currently selected instrument.
            if self._active_clip is not None:
                track = self._midi.get_track(self._selected_channel)
                if track is None or self._active_clip not in track.clips:
                    self._active_clip = None

            # No clip selected → create a new one at the playhead on the active track.
            if self._active_clip is None:
                track = self._midi.get_track(self._selected_channel)
                if track:
                    self._active_clip = self._midi.create_clip(self._selected_channel, self._midi._playhead_beat)

            ok = self._midi.start_recording(self._selected_channel, into_clip=self._active_clip)

            if not ok:
                self._transport.record_btn.setChecked(False)
                QMessageBox.warning(self, "Record",
                    "Add a track first, then arm record.")
                return
            self._st_engine.setText("Engine: ⏺ RECORDING")
        else:
            rec_clip = self._midi.last_record_clip   # grab ref before stop finalises
            self._midi.stop_recording()

            if self._active_clip is not None:
                # Piano-roll mode: the active clip received notes — refresh the view.
                self._piano_roll.set_active_clip(self._active_clip)
                if self._active_clip.notes:
                    # Scroll horizontally to make the earliest recorded note visible.
                    first_beat = min(n.start_beat for n in self._active_clip.notes)
                    self._piano_roll.set_view_x(first_beat)
                note_count = len(self._active_clip.notes)
            else:
                # Arrangement mode: open the recorded clip in the piano roll.
                note_count = len(rec_clip.notes) if rec_clip else 0
                if rec_clip and rec_clip.notes:
                    self._active_clip = rec_clip
                    self._piano_roll.set_active_clip(rec_clip)
                    self._show_piano_roll_for_track(self._selected_channel)

            self._refresh_piano_roll()
            self._st_engine.setText(
                f"Engine: ■ STOPPED — {note_count} note{'s' if note_count != 1 else ''} recorded"
            )

    @Slot(int, float, float, int)
    def _on_note_added(self, ch: int, start: float, dur: float, pitch: int) -> None:
        if self._active_clip is not None:
            self._midi.add_note_to_clip(self._active_clip, start, dur, pitch)
        else:
            self._midi.add_note_to_track(ch, start, dur, pitch)
        self._refresh_piano_roll()
        # Audition the note so the user hears the sound on insert.
        # Duration is capped at 500 ms so the preview stays snappy.
        preview_ms = int(min(dur * 60_000.0 / max(1.0, self._midi.bpm), 500))
        self._engine.note_on(ch, pitch)
        QTimer.singleShot(preview_ms, lambda p=pitch, c=ch: self._engine.note_off(c, p))

    @Slot(int, int)
    def _on_note_removed(self, ch: int, note_id: int) -> None:
        if self._active_clip is not None:
            self._midi.remove_note_from_clip(self._active_clip, note_id)
        else:
            self._midi.remove_note_from_track(ch, note_id)
        self._refresh_piano_roll()
        self._st_engine.setText("Engine: Note erased  (right-click to erase)")

    # ── Clip selection / MIDI clip operations ─────────────────────────────────

    @Slot(int, int)
    def _on_clip_selected(self, channel: int, clip_id: int) -> None:
        """Highlight the clip for drag/resize — does NOT open the piano roll."""
        track = self._midi.get_track(channel)
        if not track:
            return
        for clip in track.clips:
            if clip.clip_id == clip_id:
                self._active_clip = clip
                self._piano_roll.set_active_clip(clip)
                self._on_track_selected(channel)   # keep _selected_channel in sync
                break

    @Slot(int, int)
    def _on_clip_edit_requested(self, channel: int, clip_id: int) -> None:
        """Open the piano roll for a clip (single click with no drag in arrange view)."""
        self._on_clip_selected(channel, clip_id)
        self._show_piano_roll_for_track(channel)

    @Slot(int, int, float, int)
    def _on_note_moved(self, ch: int, note_id: int,
                       new_beat: float, new_pitch: int) -> None:
        if self._active_clip is not None:
            self._midi.move_note_in_clip(
                self._active_clip, note_id, new_beat, new_pitch)
        self._refresh_piano_roll()

    @Slot(int, int, float)
    def _on_note_resized(self, ch: int, note_id: int, new_dur: float) -> None:
        if self._active_clip is not None:
            self._midi.resize_note_in_clip(self._active_clip, note_id, new_dur)
        self._refresh_piano_roll()

    @Slot(int, int, float)
    def _on_midi_clip_moved(self, channel: int, clip_id: int,
                            new_start: float) -> None:
        self._midi.move_clip(channel, clip_id, new_start)
        self._refresh_piano_roll()

    @Slot(int, int, float)
    def _on_midi_clip_resized(self, channel: int, clip_id: int,
                              new_dur: float) -> None:
        self._midi.resize_clip(channel, clip_id, new_dur)
        self._refresh_piano_roll()

    @Slot(int, int)
    def _on_midi_clip_deleted(self, channel: int, clip_id: int) -> None:
        if self._active_clip and self._active_clip.clip_id == clip_id:
            self._active_clip = None
            self._piano_roll.set_active_clip(None)
        self._midi.delete_clip(channel, clip_id)
        self._refresh_piano_roll()

    @Slot(int, int)
    def _on_midi_clip_duplicated(self, channel: int, clip_id: int) -> None:
        self._midi.duplicate_clip(channel, clip_id)
        self._refresh_piano_roll()

    @Slot(int, float)
    def _on_clip_create_requested(self, channel: int, beat: float) -> None:
        new_clip = self._midi.create_clip(channel, beat, duration=8.0)
        self._refresh_piano_roll()
        if new_clip:
            # Make the new clip the active recording target without opening
            # the piano roll — the user can click it to open it, or just hit
            # Record and the notes go directly into this new clip.
            self._on_clip_selected(channel, new_clip.clip_id)

    @Slot(int)
    def _on_audio_track_remove(self, track_id: int) -> None:
        # Stop playback and release the pygame channel for this track.
        self._audio_player.unregister_track(track_id)
        self._audio_fx_chains.pop(track_id, None)

        # Remove the AudioMixerStrip from the mixer panel.
        strip = self._audio_mixer_strips.pop(track_id, None)
        if strip is not None:
            self._mixer_layout.removeWidget(strip)
            strip.deleteLater()

        self._midi.remove_audio_track(track_id)
        self._refresh_piano_roll()

    @Slot(int, int, float)
    def _on_audio_clip_moved(self, track_id: int, clip_id: int,
                             new_start_beat: float) -> None:
        self._midi.move_audio_clip(track_id, clip_id, new_start_beat)
        self._refresh_piano_roll()

    @Slot(int, int, float)
    def _on_audio_clip_resized(self, track_id: int, clip_id: int,
                               new_duration_secs: float) -> None:
        self._midi.resize_audio_clip(track_id, clip_id, new_duration_secs)
        self._refresh_piano_roll()

    @Slot(int, int)
    def _on_audio_clip_deleted(self, track_id: int, clip_id: int) -> None:
        self._midi.remove_audio_clip(clip_id)
        self._refresh_piano_roll()

    @Slot(int, int)
    def _on_audio_clip_duplicated(self, track_id: int, clip_id: int) -> None:
        self._midi.duplicate_audio_clip(track_id, clip_id)
        self._refresh_piano_roll()

    @Slot(int, str)
    def _on_track_renamed(self, channel: int, new_name: str) -> None:
        track = self._midi.get_track(channel)
        if track:
            track.name = new_name
        if channel == self._selected_channel:
            self._piano_roll_title.setText(f"PIANO ROLL  —  {new_name}")
        self._refresh_piano_roll()

    @Slot(int)
    def _on_track_selected(self, channel: int) -> None:
        self._selected_channel = channel
        self._controller.set_active_channel(channel)
        self._piano_roll.set_active_channel(channel)
        self._arrange_view.set_active_channel(channel)
        for ch, strip in self._mixer_strips.items():
            strip.set_selected(ch == channel)

        track = self._midi.get_track(channel)
        name  = track.name if track else f"CH {channel + 1}"
        self._piano_roll_title.setText(f"PIANO ROLL  —  {name}")

        chain = self._effect_chains.get(channel)
        if chain:
            self._effects_panel.load_chain(chain, self._engine, name)
            # Bring the MIDI effects tab to the front so the user can see it.
            self._midi_fx_dock.raise_()

        # Keep the humanizer panel in sync with the selected track.
        self._humanizer_panel.load_channel(
            channel,
            name,
            self._humanizer_params.get(channel),
        )

    # ── View-switching helpers ─────────────────────────────────────────────

    def _show_arrange_view(self) -> None:
        self._piano_roll.set_active_clip(None)
        self._view_stack.setCurrentIndex(0)

    def _on_track_header_selected(self, channel: int) -> None:
        """Called when user clicks a track header in the arrange view."""
        # Preserve the active clip only if it belongs to THIS track.
        # Clicking a different track's header clears it so recording starts fresh.
        if self._active_clip is not None:
            t = self._midi.get_track(channel)
            if t is None or self._active_clip not in t.clips:
                self._active_clip = None
        self._piano_roll.set_active_clip(self._active_clip)
        self._show_piano_roll_for_track(channel)

    def _show_piano_roll_for_track(self, channel: int) -> None:
        self._on_track_selected(channel)
        # Show the VST editor button only when the track is a VST instrument
        self._vst_edit_btn.setVisible(
            self._vst_manager.get_track(channel) is not None)
        self._view_stack.setCurrentIndex(1)
        # Force scroll area to position 0 so widget coordinates are unambiguous
        self._piano_scroll.horizontalScrollBar().setValue(0)
        self._piano_scroll.verticalScrollBar().setValue(0)
        # Re-apply view_x=0 for clip mode in case showEvent fires before this
        if self._active_clip is not None:
            self._piano_roll.set_view_x(0.0)

    # ── Instrument-change slot ─────────────────────────────────────────────

    @Slot(int)
    def _on_change_instrument(self, channel: int) -> None:
        track = self._midi.get_track(channel)
        if not track:
            return
        dlg = InstrumentSelectorDialog(self._engine, track.name, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        info = dlg.result_info()
        if not info:
            return

        kind  = info[0]
        strip = self._mixer_strips.get(channel)

        # Helper: stop and unregister SFZ player for this channel (if any).
        def _stop_sfz():
            old = self._sfz_engines.pop(channel, None)
            if old is not None:
                self._engine.unregister_sfz_player(channel)
                try:
                    old.stop()
                except Exception:
                    pass

        # Helper: stop and unregister DS player for this channel (if any).
        def _stop_ds():
            old = self._ds_engines.pop(channel, None)
            if old is not None:
                self._engine.unregister_ds_player(channel)
                try:
                    old.stop()
                except Exception:
                    pass

        if kind == "sf2":
            # When switching back to SF2, the SFZ / DS players must be stopped
            # first — they have routing priority and would otherwise silence the
            # new FluidSynth selection.
            _stop_sfz()
            _stop_ds()
            _, sf2, bank, preset, preset_name = info
            self._instrument_names[channel] = preset_name
            plugin = InstrumentPlugin(
                name=preset_name, sf2_path=sf2,
                bank=bank, preset=preset, channel=channel,
            )
            self._engine.register_instrument(plugin)
            if strip:
                strip.set_instrument_name(preset_name)
            if channel == self._selected_channel:
                self._piano_roll_title.setText(f"PIANO ROLL  —  {track.name}")
            self._st_engine.setText(
                f"Instrument changed → '{preset_name}'  (CH {channel + 1})")

        elif kind == "sfz":
            # Stop any DS player — SFZ and DS would both claim real-time output.
            # The existing SFZ player (if any) is cleaned up inside _start_sfz_player.
            _stop_ds()
            _, sfz_path, name = info
            self._instrument_names[channel] = name
            self._start_sfz_player(channel, sfz_path)
            if strip:
                strip.set_instrument_name(name)
            if channel == self._selected_channel:
                self._piano_roll_title.setText(f"PIANO ROLL  —  {track.name}")
            self._st_engine.setText(
                f"SFZ instrument loaded → '{name}'  (CH {channel + 1})")

        elif kind == "vst3":
            # VST3 takes priority over all other players; stop SFZ and DS.
            _stop_sfz()
            _stop_ds()
            _, plugin_path, plugin_name = info
            self._instrument_names[channel] = plugin_name
            if strip:
                strip.set_instrument_name(plugin_name)
            if channel == self._selected_channel:
                self._piano_roll_title.setText(f"PIANO ROLL  —  {track.name}")
            self._st_engine.setText(
                f"VST3 plugin assigned → '{plugin_name}'  (CH {channel + 1})")

        elif kind == "ds":
            # Stop any SFZ player; DS handles its own SFZ → DS cleanup.
            _stop_sfz()
            _, ds_path, ds_name = info
            self._instrument_names[channel] = ds_name
            self._start_ds_player(channel, ds_path)
            if strip:
                strip.set_instrument_name(ds_name)
            if channel == self._selected_channel:
                self._piano_roll_title.setText(f"PIANO ROLL  —  {track.name}")
            self._st_engine.setText(
                f"DS instrument loaded → '{ds_name}'  (CH {channel + 1})")

    # ── Audio drop from arrangement view ──────────────────────────────────

    @Slot(float)
    def _on_arrange_audio_drop(self, beat: float) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Add Audio Sample", os.path.expanduser("~"),
            "Audio Files (*.wav *.mp3 *.ogg *.flac *.aiff *.aif);;All Files (*)",
        )
        if path:
            name = os.path.splitext(os.path.basename(path))[0]
            self._midi.add_audio_track(path, beat, name=name, color=C["gold"])
            self._refresh_piano_roll()

    # ── Octave changed → navigate piano roll ──────────────────────────────

    @Slot(int)
    def _on_octave_changed(self, value: int) -> None:
        self._controller.set_octave(value)
        self._piano_roll.jump_to_pitch(self._nav_pitch())

    def _nav_pitch(self) -> int:
        """Compute navigation pitch from Root combo + Octave spinbox."""
        rt = self._transport.root_combo.currentText()
        NOTES = ["C", "C#", "D", "D#", "E", "F",
                 "F#", "G", "G#", "A", "A#", "B"]
        base = NOTES.index(rt[:-1]) + (int(rt[-1]) + 1) * 12
        return max(0, min(127, base + self._transport.octave_spin.value() * 12))

    @Slot(int)
    def _on_remove_track(self, channel: int) -> None:
        if QMessageBox.question(
            self, "Remove Track",
            f"Remove channel {channel+1}? All notes will be lost.",
            QMessageBox.Yes | QMessageBox.No,
        ) == QMessageBox.Yes:
            self._remove_track(channel)

    @Slot()
    def _on_add_track(self) -> None:
        dlg = AddTrackDialog(self._engine, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vst = dlg.result_vst_track()
        if vst:
            self._on_add_vst_track(vst)
            return
        sfz_track = dlg.result_sfz_track()
        if sfz_track:
            self._on_add_sfz_track(sfz_track, dlg.result_sfz_path())
            return
        # Check for a Decent Sampler track before falling through to SF2.
        ds_track = dlg.result_ds_track()
        if ds_track:
            self._on_add_ds_track(ds_track, dlg.result_ds_path())
            return
        track, plugin = dlg.result_track(), dlg.result_plugin()
        if track and plugin:
            self.add_track(track, plugin)
            self._st_engine.setText(f"Engine: Track '{track.name}' added")

    # ── Piano roll draw/select mode ───────────────────────────────────────────

    @Slot(bool)
    def _on_draw_mode_toggled(self, checked: bool) -> None:
        if checked:
            self._draw_mode_btn.setText("✏ DRAW")
            self._piano_roll.set_edit_mode("draw")
        else:
            self._draw_mode_btn.setText("⊡ SELECT")
            self._piano_roll.set_edit_mode("select")

    # ── Seek (ruler click) ────────────────────────────────────────────────────

    @Slot(float)
    def _on_seek(self, beat: float) -> None:
        was_playing = self._midi.is_playing
        if was_playing:
            self._midi.stop()
        # Always stop currently-playing audio so it doesn't bleed over the
        # new position.  This covers both the "seek while playing" case and
        # the "previous playback finished, now seek before hitting play" case.
        self._audio_player.stop_all()
        if was_playing:
            self._midi.play(from_beat=beat)
            self._st_engine.setText("Engine: ▶ PLAYING")
        else:
            self._midi._playhead_beat = beat
        # Immediately push the new beat so both views repaint the playhead line
        # without waiting for the next 50 ms refresh tick.
        self._piano_roll.set_playhead_beat(beat)
        self._arrange_view.set_playhead_beat(beat)

    # ── Arrangement horizontal scrollbar ─────────────────────────────────────

    @Slot(float)
    def _on_arrange_scroll_x(self, view_x: float) -> None:
        self._arrange_hscroll.blockSignals(True)
        self._arrange_hscroll.setValue(int(view_x * 4))
        self._arrange_hscroll.blockSignals(False)

    @Slot(int)
    def _on_arrange_hscroll(self, value: int) -> None:
        self._arrange_view.set_view_x(value / 4.0)

    # ── Piano roll vertical scrollbar ─────────────────────────────────────────

    @Slot(int)
    def _on_piano_vscroll(self, value: int) -> None:
        """
        Sync the piano roll view offset when the user drags the scrollbar thumb.

        _view_y is set directly so the rendering stays in step with the thumb
        position without the piano roll emitting view_y_changed back (which
        would cause a redundant setValue call, but no infinite loop).
        """
        self._piano_roll._view_y = value
        self._piano_roll.update()

    # ── Audio clip toolbar button ──────────────────────────────────────────────

    @Slot()
    def _on_add_audio_clip_toolbar(self) -> None:
        """Open a file dialog and add an audio sample as its own track."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Add Audio Sample", os.path.expanduser("~"),
            "Audio Files (*.wav *.mp3 *.ogg *.flac *.aiff *.aif);;All Files (*)",
        )
        if not path:
            return
        beat = max(0.0, self._arrange_view._view_x)
        name = os.path.splitext(os.path.basename(path))[0]
        atrack = self._midi.add_audio_track(path, beat, name=name, color=C["gold"])
        if atrack is not None:
            self._register_audio_track_with_player(atrack.track_id)
        self._refresh_piano_roll()
        self._view_stack.setCurrentIndex(0)

    # ── VST track ─────────────────────────────────────────────────────────────

    def _on_add_sfz_track(self, track: MidiTrack, sfz_path: str) -> None:
        """Create a MIDI track backed by an SFZ instrument engine."""
        dummy_plugin = InstrumentPlugin(
            name=track.name, sf2_path="", bank=0, preset=0, channel=track.channel)
        self.add_track(track, dummy_plugin)
        self._start_sfz_player(track.channel, sfz_path)
        self._active_clip = None
        self._piano_roll.set_active_clip(None)
        self._on_track_selected(track.channel)
        self._refresh_piano_roll()
        self._st_engine.setText(
            f"Engine: SFZ Track '{track.name}' loaded  (CH {track.channel + 1})")

    def _start_sfz_player(self, channel: int, sfz_path: str) -> None:
        """Load an SFZ file, create an SfzRealTimePlayer, and register it."""
        # Stop any existing SFZ player on this channel first.
        old = self._sfz_engines.pop(channel, None)
        if old is not None:
            self._engine.unregister_sfz_player(channel)
            try:
                old.stop()
            except Exception:
                pass
        sfz_engine = get_sfz_engine(SfzRealTimePlayer.SAMPLE_RATE,
                                    SfzRealTimePlayer.BLOCK_SIZE)
        sfz_engine.load_sfz(sfz_path)
        player = SfzRealTimePlayer(sfz_engine)
        if not player.start():
            logger.warning("SFZ player could not start audio stream for CH %d", channel)
        # Wire telemetry push so the analyzer receives rendered audio.
        player._telemetry_push = self._telemetry.push_audio
        self._sfz_engines[channel] = player
        self._engine.register_sfz_player(channel, player)

    # ── Decent Sampler track management ───────────────────────────────────────

    def _on_add_ds_track(self, track: MidiTrack, ds_path: str) -> None:
        """
        Create a MIDI track backed by a Decent Sampler (.dspreset) engine.

        Follows the same pattern as _on_add_sfz_track: a dummy InstrumentPlugin
        reserves the MIDI channel in AudioEngine while the real audio comes from
        the DsRealTimePlayer registered on the same channel.
        """
        # Reserve the channel with a placeholder plugin (no SF2 file).
        dummy_plugin = InstrumentPlugin(
            name=track.name, sf2_path="", bank=0, preset=0, channel=track.channel)
        self.add_track(track, dummy_plugin)

        # Load the DS preset and start the audio stream.
        self._start_ds_player(track.channel, ds_path)

        # Show the DS panel docked on the right and raise it.
        self._ds_panel.raise_()

        self._active_clip = None
        self._piano_roll.set_active_clip(None)
        self._on_track_selected(track.channel)
        self._refresh_piano_roll()
        self._st_engine.setText(
            f"Engine: DS Track '{track.name}' loaded  (CH {track.channel + 1})")

    def _start_ds_player(self, channel: int, ds_path: str) -> None:
        """
        Load a .dspreset file, create a DsRealTimePlayer, and register it
        on the given MIDI channel.  Any previous DS player on the channel is
        stopped and unregistered first.
        """
        # Tear down any existing DS player on this channel.
        old = self._ds_engines.pop(channel, None)
        if old is not None:
            self._engine.unregister_ds_player(channel)
            try:
                old.stop()
            except Exception:
                pass

        # Parse the preset to get zone data, then hand it to the C++ engine.
        ds_engine = get_ds_engine(DsRealTimePlayer.SAMPLE_RATE,
                                   DsRealTimePlayer.BLOCK_SIZE)
        try:
            info = parse_dspreset(ds_path)
            load_preset_into_engine(ds_engine, info)
        except Exception as exc:
            logger.warning(
                "_start_ds_player: preset load failed for '%s': %s", ds_path, exc)

        # Open the sounddevice audio stream.
        player = DsRealTimePlayer(ds_engine)
        if not player.start():
            logger.warning(
                "DS player could not open audio stream for CH %d", channel)

        # Register with AudioEngine so note_on/off are routed here.
        self._ds_engines[channel] = player
        self._engine.register_ds_player(channel, player)

        # Wire the DS panel to this engine so its knobs/sliders update the sound.
        self._ds_panel.set_engine(ds_engine)
        self._ds_panel.load_preset(ds_path)
        self._ds_panel.raise_()

    @Slot(str, float)
    def _on_ds_parameter_changed(self, param: str, value: float) -> None:
        """
        Called by DsPresetPanel when the user moves a knob or slider.
        The panel already forwarded the change to its C++ engine; this slot
        is available for project-level parameter recording if needed in future.
        """
        # Nothing to do here yet — the panel handles engine routing directly.
        # This slot exists as a hook for future automation recording.
        pass

    def _on_add_vst_track(self, vst_track: VstTrack) -> None:
        """
        Load a VST plugin, create a matching MIDI track for note storage,
        and add a mixer strip — mirrors add_track() for SF2 instruments.
        """
        ok = self._vst_manager.add_track(vst_track)
        if not ok:
            QMessageBox.warning(
                self, "VST Load Failed",
                f"Could not load plugin:\n{vst_track.plugin_path}\n\n"
                "Make sure pedalboard is installed:\n"
                "  pip install pedalboard sounddevice",
            )
            return

        # Create a companion MidiTrack so the piano roll can store notes
        midi_track = MidiTrack(
            name=vst_track.name,
            channel=vst_track.channel,
            color=vst_track.color,
        )
        self._midi.add_track(midi_track)
        self._effect_chains[vst_track.channel] = EffectChain(
            channel=vst_track.channel)

        strip = MixerStrip(vst_track.channel, vst_track.name, vst_track.color)
        strip.gain_changed  .connect(self._engine.set_gain)
        strip.pan_changed   .connect(self._engine.set_pan)
        strip.mute_toggled  .connect(self._engine.set_mute)
        strip.solo_toggled  .connect(self._engine.set_solo)
        strip.track_selected.connect(self._on_track_selected)
        strip.remove_clicked.connect(self._on_remove_track)
        self._instrument_names[vst_track.channel] = vst_track.name
        strip.set_instrument_name(vst_track.name)
        self._mixer_strips[vst_track.channel] = strip
        self._mixer_layout.addWidget(strip)

        # Always activate the newly loaded VST track.
        self._active_clip = None
        self._piano_roll.set_active_clip(None)
        self._on_track_selected(vst_track.channel)

        self._refresh_piano_roll()
        self._st_engine.setText(
            f"Engine: VST Track '{vst_track.name}' loaded  (CH {vst_track.channel + 1})")

        # Start real-time audio stream and route note events through the VST.
        ok = self._vst_manager.start_realtime(vst_track.channel)
        if ok:
            vst_player = self._vst_manager._rt_players.get(vst_track.channel)
            if vst_player:
                self._engine.register_vst_player(vst_track.channel, vst_player)
        else:
            QMessageBox.warning(
                self, "VST Audio Unavailable",
                f"Real-time audio for '{vst_track.name}' could not start.\n\n"
                "Make sure these packages are installed:\n"
                "  pip install sounddevice numpy\n\n"
                "The track will be created but will produce no sound until "
                "you restart the app after installing the packages.",
            )

        # Auto-open the parameter panel so the user can tweak the VST immediately
        QTimer.singleShot(100, lambda: self._open_vst_panel(vst_track.channel))

    def _open_vst_panel(self, channel: int) -> None:
        """Open the VST parameter panel for a channel (delayed call safe)."""
        dlg = VstParameterPanel(self._vst_manager, channel, self._controller, self)
        dlg.exec()

    @Slot()
    def _on_open_vst_editor(self) -> None:
        """
        Open the VST parameter panel for the currently selected track.

        If pedalboard is not installed, a helpful install hint is shown.
        If the track is not a VST track, the user is informed.
        """
        if not self._vst_manager.is_available():
            QMessageBox.information(
                self, "VST Not Available",
                "pedalboard is not installed.\n\n"
                "Install it with:\n  pip install pedalboard sounddevice",
            )
            return
        vst_track = self._vst_manager.get_track(self._selected_channel)
        if not vst_track:
            QMessageBox.information(
                self, "Not a VST Track",
                "The selected track is an SF2 instrument, not a VST plugin.")
            return
        dlg = VstParameterPanel(self._vst_manager, self._selected_channel,
                                self._controller, self)
        dlg.exec()

    @Slot()
    def _on_new_project(self) -> None:
        """Clear all tracks and start fresh without restarting the app."""
        if QMessageBox.question(
            self, "✦  New Project",
            "This will erase all tracks and notes. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        self._midi.stop()
        self._engine.all_notes_off()

        for strip in list(self._mixer_strips.values()):
            self._mixer_layout.removeWidget(strip)
            strip.deleteLater()
        self._mixer_strips.clear()

        for ch in list(self._engine._instruments.keys()):
            self._engine.unregister_instrument(ch)

        for ch in list(self._vst_manager._tracks.keys()):
            self._vst_manager.remove_track(ch)

        self._audio_player.stop_all()
        for tid in list(self._audio_fx_chains.keys()):
            self._audio_player.unregister_track(tid)
        self._audio_fx_chains.clear()

        # Stop real-time instrument renderers (MIDI tracks with FX).
        for renderer in list(self._instrument_renderers.values()):
            try:
                renderer.stop()
            except Exception:
                pass
        self._instrument_renderers.clear()
        self._midi_fx_chains.clear()

        # Remove all audio mixer strips from the mixer panel.
        for strip in list(self._audio_mixer_strips.values()):
            self._mixer_layout.removeWidget(strip)
            strip.deleteLater()
        self._audio_mixer_strips.clear()

        self._midi._tracks.clear()
        self._midi.clear_clips()
        self._effect_chains.clear()
        self._pygame_sounds.clear()
        self._selected_channel = 0
        self._active_clip = None
        self._piano_roll.set_active_clip(None)
        self._transport.bpm_spin.setValue(120)
        self._transport.loop_btn.setChecked(False)
        self._clip_lane.set_clips([])
        self._arrange_view.set_audio_tracks([])
        self._view_stack.setCurrentIndex(0)
        self._refresh_piano_roll()
        self._st_engine.setText("Engine: ■ New Project")

    # ── Project save / load ───────────────────────────────────────────────────

    @Slot()
    def _on_save_project(self) -> None:
        """Serialise the full DAW state to a .dawproj JSON file."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project",
            os.path.expanduser("~/Untitled.dawproj"),
            "Crystal DAW Project (*.dawproj);;All Files (*)",
        )
        if not path:
            return
        ok = ProjectManager.save_project(
            path,
            self._midi,
            self._engine,
            self._audio_fx_chains,
            self._midi_fx_chains,
            channel_rack_rows=self._channel_rack.get_rows(),
        )
        if ok:
            self._st_engine.setText(f"Saved: {os.path.basename(path)}")
            QMessageBox.information(self, "Save Project",
                                    f"Project saved:\n{path}")
        else:
            QMessageBox.critical(self, "Save Project",
                                 "Save failed — check the log for details.")

    @Slot()
    def _on_load_project(self) -> None:
        """Load a .dawproj file and rebuild all tracks, clips, and FX chains."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Project",
            os.path.expanduser("~"),
            "Crystal DAW Project (*.dawproj);;All Files (*)",
        )
        if not path:
            return

        doc = ProjectManager.load_project(path)
        if doc is None:
            QMessageBox.critical(self, "Load Project",
                                 "Could not read project file.")
            return

        # Clear the current session (same as New Project, but no confirmation).
        self._midi.stop()
        self._engine.all_notes_off()
        self._audio_player.stop_all()
        for tid in list(self._audio_fx_chains.keys()):
            self._audio_player.unregister_track(tid)
        self._audio_fx_chains.clear()
        for renderer in list(self._instrument_renderers.values()):
            try:
                renderer.stop()
            except Exception:
                pass
        self._instrument_renderers.clear()
        self._midi_fx_chains.clear()
        self._midi.clear_project()
        self._effect_chains.clear()
        self._selected_channel = 0
        self._active_clip = None

        # ── Restore BPM ───────────────────────────────────────────────────────
        bpm = float(doc.get("bpm", 120.0))
        self._midi.bpm = bpm
        self._transport.bpm_spin.setValue(int(bpm))
        if self._timeline_bridge is not None:
            self._timeline_bridge.set_bpm(bpm)

        # ── Restore MIDI tracks + clips + notes ───────────────────────────────
        from .midi_logic import MidiTrack, MidiClip, MidiNote
        for t in doc.get("midi_tracks", []):
            track = MidiTrack(
                name=t["name"], channel=t["channel"], color=t.get("color", "#4A90D9"))
            self._midi.add_track(track)
            for cd in t.get("clips", []):
                clip = MidiClip(
                    start_beat=cd["start_beat"],
                    duration=cd["duration"],
                    name=cd.get("name", ""),
                    color=cd.get("color", ""),
                    clip_id=self._midi._next_id(),
                )
                for nd in cd.get("notes", []):
                    clip.notes.append(MidiNote(
                        start_beat=nd["start_beat"],
                        duration=nd["duration"],
                        pitch=nd["pitch"],
                        velocity=nd["velocity"],
                        channel=nd["channel"],
                        note_id=self._midi._next_id(),
                    ))
                track.clips.append(clip)

        # ── Restore audio tracks + clips ──────────────────────────────────────
        for at in doc.get("audio_tracks", []):
            from .midi_logic import AudioTrack, AudioClip
            atrack = AudioTrack(
                name=at["name"],
                track_id=at["track_id"],
                color=at.get("color", "#FFD700"),
            )
            self._midi._audio_tracks[atrack.track_id] = atrack
            self._midi._audio_track_counter = max(
                self._midi._audio_track_counter, atrack.track_id + 1)
            for cd in at.get("clips", []):
                atrack.clips.append(AudioClip(
                    path=cd["path"],
                    start_beat=cd["start_beat"],
                    name=cd.get("name", ""),
                    duration_seconds=cd.get("duration_seconds", 0.0),
                    color=cd.get("color", "#FFD700"),
                    clip_id=self._midi._next_id(),
                ))

        # ── Restore audio FX chains ───────────────────────────────────────────
        for str_tid, slots in doc.get("audio_fx_chains", {}).items():
            tid   = int(str_tid)
            chain = ProjectManager.rebuild_fx_chain_from_list(slots, tid)
            if chain is not None:
                self._audio_fx_chains[tid] = chain
                self._audio_player.register_track(tid, chain)

        # ── Restore MIDI/instrument FX chains ─────────────────────────────────
        for str_ch, slots in doc.get("midi_fx_chains", {}).items():
            ch    = int(str_ch)
            chain = ProjectManager.rebuild_fx_chain_from_list(slots, ch)
            if chain is not None:
                self._midi_fx_chains[ch] = chain

        # ── Restore automation envelopes ──────────────────────────────────────
        ProjectManager.restore_automation_lanes(
            doc, self._audio_fx_chains, self._midi_fx_chains)

        # ── Restore channel rack rows ──────────────────────────────────────────
        rack_rows = ProjectManager.restore_channel_rack(doc)
        if rack_rows:
            self._channel_rack.set_rows(rack_rows)
            self._midi.set_step_rows(rack_rows)

        # ── Refresh UI ────────────────────────────────────────────────────────
        self._refresh_piano_roll()
        self._arrange_view.set_audio_tracks(self._midi.get_audio_tracks())
        self._st_engine.setText(f"Loaded: {os.path.basename(path)}")
        QMessageBox.information(self, "Load Project",
                                f"Project loaded:\n{path}")

    # ------------------------------------------------------------------
    # Automation lane handlers
    # ------------------------------------------------------------------

    @Slot(int, str)
    def _on_automation_toggled(self, track_id: int, kind: str) -> None:
        """
        Toggle the AutomationLane for a track in the AutomationPanel.
        kind = "midi" (uses MIDI FX chain) or "audio" (uses audio FX chain).
        """
        if self._auto_panel.has_lane(track_id):
            # Lane already open: close it
            self._auto_panel.remove_lane(track_id)
            # Collapse the splitter when no lanes remain
            if not self._auto_panel.isVisible():
                self._arrange_splitter.setSizes([600, 0])
        else:
            # Determine the chain and display name for this track
            if kind == "midi":
                chain = self._midi_fx_chains.get(track_id)
                if chain is None:
                    chain = AudioFxChain(track_id=track_id)
                    self._midi_fx_chains[track_id] = chain
                track = self._midi.get_track(track_id)
                name  = track.name if track else f"CH {track_id + 1:02d}"
                color_idx = track_id % len(C["tracks"])
                color = C["tracks"][color_idx]
            else:
                chain = self._audio_fx_chains.get(track_id)
                if chain is None:
                    chain = AudioFxChain(track_id=track_id)
                    self._audio_fx_chains[track_id] = chain
                atrack = self._midi.get_audio_track(track_id)
                name   = atrack.name if atrack else f"Audio {track_id}"
                color  = atrack.color if atrack else C["gold"]

            self._auto_panel.add_lane(track_id, chain, name, color)
            # Expand splitter to show the panel (about 3 lane heights)
            lane_h = 3 * 72 + 4
            total  = self._arrange_splitter.height()
            self._arrange_splitter.setSizes(
                [max(100, total - lane_h), lane_h])

    # ------------------------------------------------------------------
    # Channel Rack / Step Sequencer handlers
    # ------------------------------------------------------------------

    @Slot()
    def _on_toggle_channel_rack(self) -> None:
        """Show or hide the Channel Rack window.

        On the very first call the window is positioned below the main window
        so it is immediately visible without the user having to hunt for it.
        """
        if self._channel_rack.isVisible():
            self._channel_rack.hide()
        else:
            # Centre the rack below the main window on first show.
            # After that, let the OS remember the user's preferred position.
            if not self._channel_rack.isVisible() and \
                    self._channel_rack.pos().isNull():
                mw_geo   = self.frameGeometry()
                rack_w   = self._channel_rack.width()
                rack_x   = mw_geo.x() + (mw_geo.width() - rack_w) // 2
                rack_y   = mw_geo.y() + mw_geo.height() + 8   # just below main window
                self._channel_rack.move(max(0, rack_x), max(0, rack_y))
            self._channel_rack.show()
            self._channel_rack.raise_()
            self._channel_rack.activateWindow()

    @Slot(int, int, int)
    def _on_rack_note_on(self, row_id: int, note: int, velocity: int) -> None:
        """Fire a note for a channel rack row.

        If the row has an audio sample loaded, the RackSamplerEngine handles
        playback (pitched WAV).  Otherwise fall back to FluidSynth on the row's
        original MIDI channel (typically channel 9 for drums).
        """
        if self._rack_engine.has_sample(row_id):
            self._rack_engine.note_on(row_id, note, velocity)
        else:
            fluid_ch = self._rack_row_fluid_ch.get(row_id, 9)
            self._note_event_callback(fluid_ch, note, velocity, True)

    @Slot(int, int)
    def _on_rack_note_off(self, row_id: int, note: int) -> None:
        """Release a note for a channel rack row."""
        if self._rack_engine.has_sample(row_id):
            self._rack_engine.note_off(row_id, note)
        else:
            fluid_ch = self._rack_row_fluid_ch.get(row_id, 9)
            self._note_event_callback(fluid_ch, note, 0, False)

    @Slot(int, str)
    def _on_rack_sample_loaded(self, row_id: int, path: str) -> None:
        """Load an audio sample for a rack row into the RackSamplerEngine."""
        ok = self._rack_engine.load_sample(row_id, path)
        if ok:
            root_note = self._rack_row_note.get(row_id, 60)
            self._rack_engine.set_root_note(row_id, root_note)
            self._st_engine.setText(f"Rack row {row_id}: sample loaded")
        else:
            self._st_engine.setText(f"Rack row {row_id}: failed to load sample")

    @Slot(list)
    def _on_rack_copy_to_timeline(self, rows: list) -> None:
        """
        Convert the current 1-bar step pattern to a MidiClip placed at the
        current playhead position on each active channel.

        One timeline track is created per rack row (keyed by MIDI channel).
        If the track already exists it is reused.  New tracks get a full
        MixerStrip + EffectChain so they are immediately audible.
        """
        if not rows:
            return

        STEP_DUR  = 0.25   # One 16th note in beats
        NOTE_DUR  = STEP_DUR * 0.8  # Note-off slightly before the next step

        start_beat  = self._midi.playhead_beat
        clips_added = 0

        from .midi_logic import MidiTrack, MidiClip, MidiNote

        for row in rows:
            if not row.enabled:
                continue
            # Collect only active (enabled) steps.
            # Notes use row.row_id as channel so _note_event_callback routes
            # them back through RackSamplerEngine (or FluidSynth fallback).
            active_notes = []
            for i, active in enumerate(row.steps[:16]):
                if active:
                    active_notes.append((i * STEP_DUR, NOTE_DUR,
                                         row.note, row.velocity, row.row_id))
            if not active_notes:
                continue

            # Get or create a fully-wired MidiTrack for this row.
            # Use row.row_id as the timeline track channel so each rack row
            # gets its own separate track even when sharing a MIDI channel.
            track = self._midi.get_track(row.row_id)
            if track is None:
                color_idx = row.row_id % len(C["tracks"])
                track = MidiTrack(
                    name=row.name,
                    channel=row.row_id,    # unique per row
                    color=C["tracks"][color_idx],
                )
                # Register with MIDI logic AND create MixerStrip + EffectChain
                # so the track is audible immediately (same path as add_track).
                self._midi.add_track(track)
                self._effect_chains[row.row_id] = EffectChain(channel=row.row_id)
                self._engine.apply_effect_chain(self._effect_chains[row.row_id])

                strip = MixerStrip(row.row_id, row.name, track.color)
                strip.gain_changed  .connect(self._engine.set_gain)
                strip.pan_changed   .connect(self._engine.set_pan)
                strip.mute_toggled  .connect(self._engine.set_mute)
                strip.solo_toggled  .connect(self._engine.set_solo)
                strip.track_selected.connect(self._on_track_selected)
                strip.remove_clicked.connect(self._on_remove_track)
                strip.name_changed  .connect(self._on_track_renamed)
                self._mixer_strips[row.row_id] = strip
                self._mixer_layout.addWidget(strip)

            # Build the one-bar clip and populate it with notes.
            clip = MidiClip(
                start_beat=start_beat,
                duration=4.0,
                name=f"Rack: {row.name}",
                color=track.color,
                clip_id=self._midi._next_id(),
            )
            for rel_beat, dur, pitch, vel, ch in active_notes:
                clip.notes.append(MidiNote(
                    start_beat=rel_beat,
                    duration=dur,
                    pitch=pitch,
                    velocity=vel,
                    channel=ch,
                    note_id=self._midi._next_id(),
                ))
            track.clips.append(clip)
            row.on_timeline = True
            clips_added += 1

        self._refresh_piano_roll()

        if clips_added:
            self._st_engine.setText(
                f"Rack: {clips_added} clip(s) added to timeline at beat {start_beat:.2f}")
        else:
            self._st_engine.setText(
                "Nothing to copy — activate some steps in the channel rack first")

    @Slot()
    def _on_save_midi(self) -> None:
        # Guard: check mido is available before opening the file dialog so
        # the user gets a clear actionable message rather than a silent failure.
        try:
            import mido  # noqa: F401
        except ImportError:
            QMessageBox.critical(
                self, "Save MIDI",
                "The 'mido' library is not installed.\n\n"
                "Install it with:  pip install mido")
            return

        if not self._midi.get_all_tracks():
            QMessageBox.information(self, "Save MIDI",
                "No tracks to save.\n\nAdd a MIDI track and draw some notes first.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Save MIDI File",
            os.path.expanduser("~/Untitled.mid"),
            "MIDI Files (*.mid);;All Files (*)",
        )
        if not path:
            return

        ok = self._midi.export_to_midi_file(path)
        if ok:
            self._st_engine.setText(f"MIDI saved: {os.path.basename(path)}")
            QMessageBox.information(self, "Save MIDI", f"Saved:\n{path}")
        else:
            QMessageBox.critical(
                self, "Save MIDI",
                "Export failed — check the console for details.")

    @Slot()
    def _on_auto_master(self) -> None:
        """
        Open the AutoMasterDialog.  Pre-fills the input path with the most
        recently exported WAV, if one exists in the project export directory.
        """
        # Try to pre-fill with the last known export path.
        last_wav = getattr(self, "_last_export_wav", "")
        dlg = AutoMasterDialog(self, input_path=last_wav)
        dlg.master_completed.connect(self._on_auto_master_done)
        dlg.exec()

    @Slot(str)
    def _on_auto_master_done(self, output_path: str) -> None:
        if output_path and os.path.isfile(output_path):
            QMessageBox.information(
                self, "Auto-Master Complete",
                f"Mastered file saved to:\n{output_path}\n\n"
                "You can drag it into an audio track to audition.",
            )

    @Slot()
    def _on_ai_mix(self) -> None:
        """
        Show a compact genre-picker dialog then apply the AIMixAssistant
        FX template to all loaded MIDI tracks.  Any previously AI-applied
        plugins are cleared first so the operation is fully idempotent.
        """
        tracks = self._midi.get_all_tracks()
        if not tracks:
            QMessageBox.information(
                self, "AI Mix",
                "No MIDI tracks are loaded. Open a MIDI file first.",
            )
            return

        # ── Genre picker dialog ───────────────────────────────────────────────
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox
        _C = {"abyss": "#060A18", "deep": "#0A0E22", "cyan": "#00E5FF",
              "purple": "#9945FF", "text": "#C8E6FF", "dim": "#3D5A80"}

        dlg = QDialog(self)
        dlg.setWindowTitle("AI Mix — Select Genre")
        dlg.setFixedSize(300, 140)
        dlg.setStyleSheet(
            f"QDialog {{ background:{_C['abyss']}; color:{_C['text']}; }}"
            f"QLabel  {{ color:{_C['text']}; font-size:10px; background:transparent; }}"
            f"QComboBox {{ background:{_C['deep']}; color:{_C['cyan']};"
            f" border:1px solid rgba(153,69,255,0.4); border-radius:4px;"
            f" font-size:10px; padding:3px 8px; }}"
            f"QComboBox QAbstractItemView {{ background:{_C['deep']};"
            f" color:{_C['text']}; selection-background-color:{_C['purple']}; }}"
        )
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(10)

        lbl = QLabel("Choose a genre to apply the AI FX template:")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        combo = QComboBox()
        combo.addItems(["TRAP", "TECHNO", "PHONK", "POP", "HIPHOP", "EDM", "HOUSE", "CINEMATIC"])
        # Pre-select genre matching the telemetry dashboard if one is active.
        telem_genre = getattr(self._telemetry, "_selected_genre", "")
        if telem_genre.upper() in [combo.itemText(i) for i in range(combo.count())]:
            combo.setCurrentText(telem_genre.upper())
        lay.addWidget(combo)

        btn_row = QHBoxLayout()
        btn_apply  = QPushButton("▶  Apply")
        btn_cancel = QPushButton("Cancel")
        for btn, color in ((btn_apply, _C["cyan"]), (btn_cancel, _C["dim"])):
            btn.setFixedHeight(28)
            btn.setStyleSheet(
                f"QPushButton {{ background:{_C['deep']}; color:{color};"
                f" border:1px solid {color}; border-radius:4px; font-size:10px; }}"
                f"QPushButton:hover {{ background:rgba(0,0,0,0.2); }}"
            )
        btn_apply.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_apply)
        btn_row.addWidget(btn_cancel)
        lay.addLayout(btn_row)

        if dlg.exec() != QDialog.Accepted:
            return

        genre = combo.currentText()

        # ── Run AIMixAssistant ────────────────────────────────────────────────
        assistant = AIMixAssistant(
            audio_file_player = self._audio_player,
            tracks            = tracks,
            master_chain      = None,   # master bus not touched via status-bar trigger
        )
        try:
            assistant.apply(genre)
            n = len(tracks)
            self.statusBar().showMessage(
                f"🤖 AI Mix applied — genre: {genre}, {n} track(s) processed.", 4000
            )
        except Exception as exc:
            logger.warning("AIMixAssistant.apply failed: %s", exc)
            QMessageBox.warning(
                self, "AI Mix Error",
                f"Failed to apply AI Mix template:\n{exc}",
            )

    @Slot()
    def _on_randomize_instruments(self) -> None:
        """
        Assign a random role-appropriate SF2 instrument to every loaded MIDI track.

        Uses SoundfontLibrary to scan for categorised soundfont folders first.
        Falls back to GM preset pools within GeneralUser-GS.sf2 (or any flat
        SF2 found in the soundfonts/ directory) when no folder structure exists.
        """
        tracks = self._midi.get_all_tracks()
        if not tracks:
            QMessageBox.information(
                self, "Auto-Assign Instruments",
                "No MIDI tracks are loaded. Open a MIDI file first.",
            )
            return

        lib        = build_library_from_engine()
        randomizer = InstrumentRandomizer(lib)
        randomizer.reset_uniqueness()

        assigned = 0
        errors   = []

        for track in tracks:
            result = randomizer.pick(track.name)
            if result is None:
                errors.append(track.name)
                continue

            sf2_path, bank, preset, display_name = result
            plugin = InstrumentPlugin(
                name=display_name,
                sf2_path=sf2_path,
                bank=bank,
                preset=preset,
                channel=track.channel,
            )

            if self._engine.register_instrument(plugin):
                assigned += 1
                # Update the mixer strip label.
                strip = self._mixer_strips.get(track.channel)
                if strip is not None:
                    strip.set_instrument_name(display_name)
                self._instrument_names[track.channel] = display_name
                logger.info(
                    "Auto-Assign: track='%s' → '%s' (bank=%d preset=%d)",
                    track.name, display_name, bank, preset,
                )
            else:
                errors.append(track.name)

        msg = f"Assigned instruments to {assigned}/{len(tracks)} track(s)."
        if errors:
            msg += f"\nFailed tracks: {', '.join(errors)}"
        QMessageBox.information(self, "Auto-Assign Instruments", msg)

    @Slot()
    def _on_open_midi(self) -> None:
        if self._midi.get_all_tracks():
            if QMessageBox.question(
                self, "Open MIDI",
                "Opening will replace all current tracks. Continue?",
                QMessageBox.Yes | QMessageBox.No,
            ) != QMessageBox.Yes:
                return

        path, _ = QFileDialog.getOpenFileName(
            self, "Open MIDI File", os.path.expanduser("~"),
            "MIDI Files (*.mid *.midi);;All Files (*)",
        )
        if not path:
            return

        self._midi.stop()
        self._engine.all_notes_off()
        for strip in list(self._mixer_strips.values()):
            self._mixer_layout.removeWidget(strip)
            strip.deleteLater()
        self._mixer_strips.clear()
        for ch in list(self._engine._instruments.keys()):
            self._engine.unregister_instrument(ch)
        self._effect_chains.clear()

        ok = self._midi.import_from_midi_file(path)
        if not ok:
            QMessageBox.critical(self, "Open MIDI", "Import failed.")
            return

        self._transport.bpm_spin.setValue(self._midi.bpm)
        sf2_files = AudioEngine.get_available_sf2_files()
        sf2 = sf2_files[0] if sf2_files else ""

        # Parse the MIDI file once to get GM program IDs and SFZ paths for
        # every channel — used for both SF2 preset selection and SFZ loading.
        _overrides = GmDefaultsManager().load()
        _parsed    = _parse_midi_file(path, _overrides)
        # Build channel → (gm_program_id, sfz_path) lookup from parsed payloads.
        _ch_gm: dict = {}   # channel → gm_program_id
        _ch_sfz: dict = {}  # channel → sfz_path
        if _parsed:
            _, _payloads = _parsed
            for _pl in _payloads:
                if not _pl.events:
                    continue
                _ch = _pl.events[0].channel
                if _ch not in _ch_gm:
                    _ch_gm[_ch]  = _pl.gm_program_id
                    _ch_sfz[_ch] = _pl.sfz_path

        for track in self._midi.get_all_tracks():
            track.color = C["tracks"][track.channel % len(C["tracks"])]
            gm_id    = _ch_gm.get(track.channel, 0)
            is_drums = (track.channel == 9) or (gm_id == 128)
            plugin = InstrumentPlugin(
                name=track.name, sf2_path=sf2,
                bank=128 if is_drums else 0,
                preset=0 if is_drums else gm_id,
                channel=track.channel,
            )
            if sf2:
                self._engine.register_instrument(plugin)

            self._effect_chains[track.channel] = EffectChain(
                channel=track.channel)

            strip = MixerStrip(track.channel, track.name, track.color)
            strip.gain_changed  .connect(self._engine.set_gain)
            strip.pan_changed   .connect(self._engine.set_pan)
            strip.mute_toggled  .connect(self._engine.set_mute)
            strip.solo_toggled  .connect(self._engine.set_solo)
            strip.track_selected.connect(self._on_track_selected)
            strip.remove_clicked.connect(self._on_remove_track)
            self._mixer_strips[track.channel] = strip
            self._mixer_layout.addWidget(strip)

        if self._midi.get_all_tracks():
            self._on_track_selected(self._midi.get_all_tracks()[0].channel)

        # Apply all effect chains — zeros reverb/chorus so the imported MIDI
        # doesn't play with FluidSynth's default heavy reverb.
        for ch, chain in self._effect_chains.items():
            self._engine.apply_effect_chain(chain)

        # Apply per-category instrument overrides (SFZ, SF2, or VST3).
        # Only activated when the user has explicitly set an override via GM Defaults.
        _gm_mgr_inst  = GmDefaultsManager()
        _loaded_channels: set = set()
        for _ch, _gm_id in _ch_gm.items():
            if _ch in _loaded_channels:
                continue
            _loaded_channels.add(_ch)
            _entry = _gm_mgr_inst.get_override_entry(_gm_id, _overrides)
            if not _entry:
                continue
            _etype = _entry.get("type", "sfz")
            _epath = _entry.get("path", "")
            if not _epath or not os.path.isfile(_epath):
                continue
            if _etype == "sfz":
                self._start_sfz_player(_ch, _epath)
                _pl = self._sfz_engines.get(_ch)
                if _pl is not None:
                    _pl._telemetry_push = self._telemetry.push_audio
            elif _etype == "sf2":
                _track_name = next(
                    (t.name for t in self._midi.get_all_tracks() if t.channel == _ch),
                    "",
                )
                _is_drums = (_gm_id == 128) or (_ch == 9)
                _ov_plugin = InstrumentPlugin(
                    name=_track_name, sf2_path=_epath,
                    bank=_entry.get("bank", 128 if _is_drums else 0),
                    preset=_entry.get("preset", 0),
                    channel=_ch,
                )
                self._engine.register_instrument(_ov_plugin)

        self._refresh_piano_roll()
        self._st_engine.setText(f"Opened: {os.path.basename(path)}")

    @Slot()
    def _on_gm_defaults(self) -> None:
        """Open the GM instrument defaults settings dialog."""
        dlg = GmDefaultsDialog(parent=self)
        dlg.defaults_changed.connect(
            lambda: self._st_engine.setText("GM instrument defaults saved.")
        )
        dlg.exec()

    @Slot()
    def _on_export(self) -> None:
        """
        Export the full project (MIDI instrument tracks + audio file tracks)
        to a single stereo audio file.

        Rendering pipeline:
            MIDI tracks   → pyfluidsynth offline API  → float32 PCM
            Audio tracks  → pedalboard AudioFile       → float32 PCM
            Both paths    → C++ OfflineExporter mix bus → WAV on disk
            Optional      → ffmpeg                     → MP3 or AAC
        """
        # Stop live playback before rendering — the real-time audio thread and
        # the offline renderer both drive FluidSynth and sounddevice; running
        # them simultaneously causes clicks, pitch drift, and repeated sections.
        self._on_stop()

        midi_tracks  = self._midi.get_all_tracks()
        audio_tracks = self._midi.get_audio_tracks()
        step_rows    = self._midi.get_step_rows()

        if not midi_tracks and not audio_tracks and not step_rows:
            QMessageBox.information(
                self, "Export",
                "Nothing to export.\n\n"
                "Add a MIDI track or import an audio file first.")
            return

        dlg = ExportDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return

        out_path  = dlg.result_path().strip()
        fmt       = dlg.result_format()
        bpm       = self._midi.bpm

        # 32-bit float for WAV archiving; 24-bit broadcast standard otherwise.
        bit_depth = 32 if fmt == "wav" else 24

        # Calculate project end beat so step-sequencer events cover the full
        # duration without extending past it.
        end_beat = 0.0
        for track in midi_tracks:
            for note in track.sorted_notes():
                end_beat = max(end_beat, note.start_beat + note.duration)
        for atrack in audio_tracks:
            for clip in atrack.clips:
                end_beat = max(end_beat, clip.start_beat + clip.duration_seconds * bpm / 60.0)
        end_beat = max(end_beat + 1.0, 8.0)

        step_events = self._midi._build_step_events(0.0, end_beat) if step_rows else []

        # ── Progress dialog ───────────────────────────────────────────────────
        prog = QProgressDialog("Preparing export…", "Cancel", 0, 100, self)
        prog.setWindowTitle("Exporting Project")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setValue(0)

        # ── Launch worker thread ──────────────────────────────────────────────
        worker = ExportWorker(
            out_path=out_path,
            fmt=fmt,
            audio_tracks=audio_tracks,
            midi_tracks=midi_tracks,
            instruments=self._engine.get_instruments_by_channel(),
            audio_fx_chains=self._audio_fx_chains,
            bpm=bpm,
            bit_depth=bit_depth,
            step_events=step_events,
            midi_fx_chains=self._midi_fx_chains,
            parent=self,
        )

        worker.progress.connect(prog.setValue)
        worker.log_msg.connect(prog.setLabelText)
        worker.finished.connect(prog.close)
        prog.canceled.connect(worker.requestInterruption)

        def _on_done(ok: bool) -> None:
            if ok:
                QMessageBox.information(
                    self, "Export Complete", f"Saved:\n{out_path}")
                self._st_engine.setText(
                    f"Exported: {os.path.basename(out_path)}")
            else:
                QMessageBox.critical(
                    self, "Export Failed",
                    "Export encountered an error.\n"
                    "Check the log or the progress dialog for details.")

        worker.finished.connect(_on_done)

        # Keep a reference so the thread is not garbage-collected mid-run.
        self._export_worker = worker
        worker.start()

    @Slot()
    def _on_master_export(self) -> None:
        """
        Snapshot the entire live project into a FullProjectRenderInfo and
        open the multi-format mastering export dialog.

        Nothing from the GUI is passed to the worker thread — only immutable
        Python data that was copied here on the GUI thread.
        """
        # Stop live playback before rendering — the real-time audio thread and
        # the offline renderer both drive FluidSynth and sounddevice; running
        # them simultaneously causes clicks, pitch drift, and repeated sections.
        self._on_stop()

        bpm = self._midi.bpm
        spb = 60.0 / max(1.0, bpm)   # seconds per beat

        instruments = self._engine.get_instruments_by_channel()
        max_beat    = 0.0             # tracks the latest event in beats

        # ── Helper: convert one AudioFxChain's envelopes to AutomationRenderInfo ──
        def _collect_automation(fx_chain) -> list:
            if fx_chain is None:
                return []
            result = []
            for key, env in list(fx_chain.envelopes.items()):
                if not getattr(env, "nodes", None):
                    continue
                pts = [
                    (n.beat_pos * spb, env.evaluate(n.beat_pos))
                    for n in sorted(env.nodes, key=lambda x: x.beat_pos)
                ]
                result.append(AutomationRenderInfo(target_key=key, points=pts))
            return result

        # ── MIDI tracks ───────────────────────────────────────────────────────
        # Pre-scan: warn about any tracks that have no SF2 fallback registered.
        # SFZ / DS / VST3 real-time players cannot be used in the offline render;
        # the import handlers register a FluidSynth fallback for exactly this case.
        _skipped_names: list = []
        for _mt in self._midi.get_all_tracks():
            if not list(_mt.notes):
                continue
            _pl = instruments.get(_mt.channel)
            if _pl is None or not _pl.sf2_path:
                _skipped_names.append(_mt.name or f"CH {_mt.channel + 1}")
        if _skipped_names:
            logger.warning(
                "_on_master_export: %d track(s) have no SF2 fallback and will "
                "be skipped: %s", len(_skipped_names), ", ".join(_skipped_names)
            )

        midi_tracks: list = []
        for mtrack in self._midi.get_all_tracks():
            notes = list(mtrack.notes)
            if not notes:
                continue
            plugin = instruments.get(mtrack.channel)
            if plugin is None or not plugin.sf2_path:
                continue   # no SF2 fallback — already warned above

            # Update project duration estimate.
            for note in notes:
                max_beat = max(max_beat, note.start_beat + note.duration)

            # Live mixer-strip values take priority; fall back to plugin defaults.
            strip  = self._mixer_strips.get(mtrack.channel)
            volume = (strip.vol.value() / 100.0) if strip else plugin.gain
            pan    = (strip.pan.value() / 50.0)  if strip else plugin.pan

            fx_chain = self._midi_fx_chains.get(mtrack.channel)

            midi_tracks.append(MidiTrackRenderInfo(
                name       = mtrack.name or f"Track {mtrack.channel}",
                channel    = mtrack.channel,
                notes      = notes,
                sf2_path   = plugin.sf2_path,
                bank       = plugin.bank,
                preset     = plugin.preset,
                volume     = volume,
                pan        = pan,
                automation = _collect_automation(fx_chain),
                fx_chain   = fx_chain,
            ))

        # ── Audio tracks ──────────────────────────────────────────────────────
        audio_track_infos: list = []
        for atrack in self._midi.get_audio_tracks():
            for clip in atrack.clips:
                max_beat = max(
                    max_beat,
                    clip.start_beat + clip.duration_seconds / spb,
                )

            strip  = self._audio_mixer_strips.get(atrack.track_id)
            volume = (strip.vol.value() / 100.0) if strip else 1.0
            pan    = (strip.pan.value() / 50.0)  if strip else 0.0

            fx_chain = self._audio_fx_chains.get(atrack.track_id)

            audio_track_infos.append(TrackRenderInfo(
                track_id   = atrack.track_id,
                name       = atrack.name,
                clips      = list(atrack.clips),
                fx_chain   = fx_chain,
                volume     = volume,
                pan        = pan,
                automation = _collect_automation(fx_chain),
            ))

        # ── Step sequencer events ─────────────────────────────────────────────
        # Use at least 8 bars so short patterns loop enough times to be audible.
        step_end_beat = max(max_beat + 4.0, 8.0)
        step_events   = self._midi._build_step_events(0.0, step_end_beat)

        # ── Guard: nothing to render ──────────────────────────────────────────
        if not midi_tracks and not audio_track_infos and not step_events:
            QMessageBox.information(
                self, "Master Export",
                "Nothing to export.\n\n"
                "Add MIDI tracks with instruments, audio clips, or step-sequencer "
                "patterns before using the mastering export.")
            return

        # ── Build immutable project snapshot ──────────────────────────────────
        render_info = FullProjectRenderInfo(
            midi_tracks  = midi_tracks,
            audio_tracks = audio_track_infos,
            step_events  = step_events,
            bpm          = bpm,
            sample_rate  = 44100,
        )

        # ── Default output directory / project name ───────────────────────────
        last_export  = getattr(self, "_last_export_path", "")
        default_dir  = (
            os.path.dirname(last_export)
            if last_export and os.path.isdir(os.path.dirname(last_export))
            else os.path.expanduser("~")
        )
        default_name = (
            os.path.splitext(os.path.basename(last_export))[0]
            if last_export else "project"
        )

        dlg = MasterExportDialog(
            render_info  = render_info,
            output_dir   = default_dir,
            project_name = default_name,
            parent       = self,
        )
        dlg.exec()

    @Slot()
    def _on_scale_changed(self) -> None:
        scale = self._transport.scale_combo.currentText()
        rt    = self._transport.root_combo.currentText()
        NOTES = ["C", "C#", "D", "D#", "E", "F",
                 "F#", "G", "G#", "A", "A#", "B"]
        pitch = NOTES.index(rt[:-1]) + (int(rt[-1]) + 1) * 12
        self._controller.set_scale(scale, pitch)
        self._st_scale.setText(f"Scale: {scale.title()} — {rt}")
        self._piano_roll.jump_to_pitch(self._nav_pitch())

    @Slot()
    def _on_connect_gamepad(self) -> None:
        # Stop any previous timer before re-connecting.
        if self._gamepad_timer is not None:
            self._gamepad_timer.stop()
            self._gamepad_timer = None

        ok = self._controller.start_gamepad_polling()
        if ok:
            # Drive poll_once() from the main thread via QTimer so that
            # pygame.event.pump() never runs on a background thread
            # (macOS AppKit requires it on the main thread).
            self._gamepad_timer = QTimer(self)
            self._gamepad_timer.timeout.connect(self._controller.poll_once)
            self._gamepad_timer.start(16)   # ~60 Hz

            name = self._controller.controller_name
            QMessageBox.information(self, "Gamepad",
                f"{name} connected!\n\n"
                "8 buttons → scale degrees 0–7\n"
                "D-pad Up / Down → octave shift")
        else:
            QMessageBox.warning(self, "Gamepad",
                "No gamepad detected.\n\n"
                "Make sure your PS5 or Switch Pro Controller\n"
                "is connected via USB or Bluetooth first.")

    @Slot()
    def _on_show_map(self) -> None:
        NOTES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]

        def note_name(pitch: int) -> str:
            return f"{NOTES[pitch % 12]}{pitch // 12 - 1}"

        dlg = QDialog(self)
        dlg.setWindowTitle("Input Mapping Guide")
        dlg.setMinimumSize(520, 480)
        dlg.setStyleSheet(STYLESHEET)
        lay = QVBoxLayout(dlg)

        # Header
        cname = self._controller.controller_name
        gamepad_info = f"  ·  Gamepad: {cname}" if cname else "  ·  No gamepad connected"
        lay.addWidget(_label(
            f"Scale: {self._controller.scale_name.title()}   "
            f"Root: {note_name(self._controller.root_pitch)}   "
            f"Channel {self._controller.active_channel + 1}"
            f"{gamepad_info}",
            C["cyan"], 11, True))

        tabs = QTabWidget()
        tabs.setStyleSheet("QTabWidget::pane { border: none; }")

        # ── Keyboard tab ──────────────────────────────────────────────────
        kb_w  = QWidget()
        kb_lay = QVBoxLayout(kb_w)
        kb_lay.addWidget(_label(
            "↑ Arrow Up = Octave +1    ↓ Arrow Down = Octave −1",
            C["gold"], 10, False))
        kb_grid_w = QWidget()
        kb_grid   = QGridLayout(kb_grid_w)
        kb_grid.addWidget(_label("Key",  C["text_dim"]), 0, 0)
        kb_grid.addWidget(_label("Note", C["text_dim"]), 0, 1)
        kb_grid.addWidget(_label("MIDI", C["text_dim"]), 0, 2)
        for row, (_, m) in enumerate(
            list(self._controller.get_key_map().items())[:36], 1
        ):
            kb_grid.addWidget(QLabel(m.label.upper()),          row, 0)
            kb_grid.addWidget(QLabel(note_name(m.midi_pitch)), row, 1)
            kb_grid.addWidget(QLabel(str(m.midi_pitch)),        row, 2)
        sa_kb = QScrollArea()
        sa_kb.setWidget(kb_grid_w)
        sa_kb.setWidgetResizable(True)
        kb_lay.addWidget(sa_kb)
        tabs.addTab(kb_w, "⌨ Keyboard")

        # ── Gamepad tab ───────────────────────────────────────────────────
        gp_w  = QWidget()
        gp_lay = QVBoxLayout(gp_w)

        gp_grid_w = QWidget()
        gp_grid   = QGridLayout(gp_grid_w)
        gp_grid.addWidget(_label("Button",  C["text_dim"]), 0, 0)
        gp_grid.addWidget(_label("Action",  C["text_dim"]), 0, 1)
        gp_grid.addWidget(_label("Note",    C["text_dim"]), 0, 2)

        row = 1
        for _, m in self._controller._gamepad_map.items():
            gp_grid.addWidget(QLabel(m.label),               row, 0)
            gp_grid.addWidget(QLabel("Play note"),            row, 1)
            gp_grid.addWidget(QLabel(note_name(m.midi_pitch)), row, 2)
            row += 1
        for _, m in self._controller._axis_map.items():
            gp_grid.addWidget(QLabel(f"{m.label} (trigger)"), row, 0)
            gp_grid.addWidget(QLabel("Play note"),            row, 1)
            gp_grid.addWidget(QLabel(note_name(m.midi_pitch)), row, 2)
            row += 1

        # Fixed entries for D-pad and special actions
        for btn, action in [("D-pad ↑", "Octave +1"), ("D-pad ↓", "Octave −1")]:
            gp_grid.addWidget(QLabel(btn),    row, 0)
            gp_grid.addWidget(QLabel(action), row, 1)
            gp_grid.addWidget(QLabel("—"),    row, 2)
            row += 1

        if row == 1:
            gp_lay.addWidget(_label(
                "No gamepad connected.\nConnect a PS5 or Switch controller\n"
                "and click the GAMEPAD button in the toolbar.",
                C["text_dim"], 10, False))
        else:
            sa_gp = QScrollArea()
            sa_gp.setWidget(gp_grid_w)
            sa_gp.setWidgetResizable(True)
            gp_lay.addWidget(sa_gp)

        tabs.addTab(gp_w, "🎮 Gamepad")
        lay.addWidget(tabs)

        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close, alignment=Qt.AlignRight)
        dlg.exec()

    # ── Piano roll copy / paste ───────────────────────────────────────────────

    def _copy_selected_notes(self) -> None:
        """Copy selected notes to the internal clipboard (relative positions)."""
        if not self._active_clip:
            return
        selected = [n for n in self._active_clip.notes
                    if n.note_id in self._piano_roll._selected_ids]
        if not selected:
            self._st_engine.setText("Copy: no notes selected")
            return
        min_beat = min(n.start_beat for n in selected)
        self._note_clipboard = [
            {"rel_beat": n.start_beat - min_beat,
             "pitch":    n.pitch,
             "duration": n.duration,
             "velocity": n.velocity}
            for n in selected
        ]
        self._st_engine.setText(f"Copied {len(selected)} note(s) — Cmd+V to paste")

    def _paste_notes(self) -> None:
        """Paste clipboard notes at the last clicked beat position in the piano roll."""
        if not self._note_clipboard:
            self._st_engine.setText("Paste: clipboard is empty — select and Cmd+C first")
            return
        if not self._active_clip:
            self._st_engine.setText("Paste: no active clip — create or open a clip first")
            return
        paste_beat = max(0.0, self._piano_roll._last_click_beat)
        new_ids: set = set()
        for entry in self._note_clipboard:
            note = self._midi.add_note_to_clip(
                self._active_clip,
                paste_beat + entry["rel_beat"],
                entry["duration"],
                entry["pitch"],
                entry["velocity"],
                self._selected_channel,
            )
            new_ids.add(note.note_id)
        # Select the pasted notes so the user can immediately move/inspect them
        self._piano_roll._selected_ids = new_ids
        self._refresh_piano_roll()
        self._st_engine.setText(
            f"Pasted {len(self._note_clipboard)} note(s) at beat {paste_beat:.2f}")

    @Slot()
    def _on_refresh_tick(self) -> None:
        # Push the current playhead beat to both views so they can draw the
        # moving playhead line.  This runs at ~20 Hz (50 ms interval) which
        # gives smooth visual motion without taxing the render thread.
        beat = self._midi.playhead_beat
        self._piano_roll.set_playhead_beat(beat)
        self._arrange_view.set_playhead_beat(beat)
        if self._midi.is_playing:
            self._st_beat.setText(f"♩ {beat:.2f}")
            # Apply automation envelopes to all audio FX chains, then
            # immediately propagate the new volume/pan to the active playback
            # engine so the change is audible without waiting for the next
            # clip-start event.
            for tid, chain in self._audio_fx_chains.items():
                if chain.envelopes:
                    chain.apply_automation(beat)
                    # Path 1: pygame fallback — update the mixer channel
                    # volume.  _apply_channel_volume reads chain.volume which
                    # apply_automation just wrote, so no extra state is needed.
                    self._audio_player.set_volume(tid, chain.volume)
                    # Path 2: C++ sounddevice engine — push volume and pan to
                    # the TimelineEngine so the block-level gain is updated
                    # before the next process_block_into() call.
                    if self._timeline_bridge.is_available:
                        self._timeline_bridge.set_audio_track_volume(tid, chain.volume)
                        self._timeline_bridge.set_audio_track_pan(tid, chain.pan)
            # MIDI FX chains: apply automation and push the result to FluidSynth
            # via CC7 (volume) and CC10 (pan) so the change is audible immediately.
            for channel, chain in self._midi_fx_chains.items():
                if chain.envelopes:
                    chain.apply_automation(beat)
                    self._engine.set_gain(channel, min(1.0, chain.volume))
                    self._engine.set_pan(channel, chain.pan)
        # Always update the channel rack scanning light (even when paused).
        self._channel_rack.set_playhead_beat(beat)

        # If any background waveform load finished since the last tick,
        # repaint the arrange view so the newly computed peaks appear.
        if self._waveform_gen.poll():
            self._arrange_view.update()

    def _note_event_callback(self, channel: int, pitch: int, velocity: int, is_on: bool) -> None:
        # ── Path 0: Rack-sampler engine (row_id channels ≥ 32) ──────────────
        # Timeline clips copied from the channel rack use channel = row_id (≥32).
        # Route them to the RackSamplerEngine if a sample is loaded; otherwise
        # fall back to the row's real MIDI channel for FluidSynth.
        if channel >= 32:
            if self._rack_engine.has_sample(channel):
                if is_on:
                    self._rack_engine.note_on(channel, pitch, velocity)
                else:
                    self._rack_engine.note_off(channel, pitch)
            else:
                fluid_ch = self._rack_row_fluid_ch.get(channel, 9)
                self._note_event_callback(fluid_ch, pitch, velocity, is_on)
            return

        # ── Path 1: Velocity humanization ────────────────────────────────────
        # Apply Gaussian + timing-weight humanization when the user has enabled
        # it for this channel.  Only modifies note-on velocity (note-off = 0).
        humanizer = self._midi_humanizers.get(channel)
        if is_on and humanizer is not None and velocity > 0:
            beat = self._midi.playhead_beat
            velocity = humanizer.humanize(velocity, beat)

        # ── Path 2: Instrument plugins in MIDI-track FX chains ──────────────
        # Check whether any plugin in the chain is an active instrument (e.g. a
        # SamplerPlugin with a file loaded).  If so, the plugin handles audio
        # synthesis on its own — FluidSynth must be skipped to prevent the
        # instrument and the soundfont from sounding simultaneously.
        midi_chain = self._midi_fx_chains.get(channel)
        chain_has_instrument = False
        if midi_chain is not None:
            vel_float = velocity / 127.0
            for plugin in list(midi_chain.plugins):
                if plugin is None:
                    continue
                # Detect active instrument plugins via the is_instrument_active()
                # contract defined on FxPluginBase (default False for pure FX).
                if plugin.is_instrument_active():
                    chain_has_instrument = True
                # Skip bypassed plugins entirely — no note events, no audio.
                # This lets FluidSynth take over when the user deactivates the slot.
                if not plugin.enabled:
                    continue
                try:
                    if is_on and hasattr(plugin, "note_on"):
                        plugin.note_on(int(pitch), float(vel_float))
                    elif not is_on and hasattr(plugin, "note_off"):
                        plugin.note_off(int(pitch))
                except Exception:
                    pass  # never crash the playback thread on a plugin error

        # ── Path 2: FluidSynth / VST synthesis ──────────────────────────────
        # Skipped when an instrument plugin above already claimed the channel.
        # AudioEngine.note_on/off handles VST routing via _vst_players and
        # FluidSynth CC-based effects via _effect_chains.
        if not chain_has_instrument:
            if is_on:
                chain = self._effect_chains.get(channel)
                self._engine.note_on_with_effects(channel, pitch, velocity, chain)
            else:
                self._engine.note_off(channel, pitch)

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def keyPressEvent(self, ev: QKeyEvent) -> None:
        if not ev.isAutoRepeat():
            key  = ev.key()
            mods = ev.modifiers()
            ctrl = bool(mods & Qt.ControlModifier)

            if key == Qt.Key_Up:
                self._transport.octave_spin.setValue(
                    self._transport.octave_spin.value() + 1)
                return
            if key == Qt.Key_Down:
                self._transport.octave_spin.setValue(
                    self._transport.octave_spin.value() - 1)
                return
            if ctrl and key == Qt.Key_C:
                self._copy_selected_notes()
                return
            if ctrl and key == Qt.Key_V:
                self._paste_notes()
                return

            self._controller.handle_key_press(key)
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev: QKeyEvent) -> None:
        if not ev.isAutoRepeat():
            key  = ev.key()
            mods = ev.modifiers()
            ctrl = bool(mods & Qt.ControlModifier)
            if key in (Qt.Key_Up, Qt.Key_Down):
                return
            if ctrl and key in (Qt.Key_C, Qt.Key_V):
                return
            self._controller.handle_key_release(key)
        super().keyReleaseEvent(ev)

    def closeEvent(self, ev) -> None:
        self._refresh_timer.stop()
        if self._gamepad_timer is not None:
            self._gamepad_timer.stop()
        self._midi.stop()
        self._controller.stop_gamepad_polling()
        self._engine.stop()
        self._rack_engine.stop()
        if self._timeline_bridge is not None:
            self._timeline_bridge.close_stream()
        ev.accept()