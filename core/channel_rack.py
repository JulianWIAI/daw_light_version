"""
channel_rack.py -- Channel Rack / Step Sequencer Window for Crystal DAW.
========================================================================
Classic 16-step pattern sequencer with per-row Mute, Volume/Pan knobs,
a BPM-synced scanning light, and a "Copy to Timeline" export.

Classes:
    ChannelStepData    -- Pure Python dataclass: 16 steps + per-row settings.
    KnobWidget         -- Circular drag knob (volume / pan).
    StepButton         -- Toggle button for one 16th-note step cell.
    ChannelRackRow     -- Full instrument row: name, mute, knobs, 16 steps.
    ChannelRackWindow  -- Top-level dialog hosting all rows + toolbar.

Playback integration:
    MainWindow calls set_playhead_beat(beat) from its existing _on_refresh_tick
    (~20 Hz).  The window fires note_on_requested / note_off_requested signals
    which MainWindow connects to _note_event_callback -- the same path used by
    the main sequencer, so the Sampler engine receives the events identically.

Step sequencer timing:
    Each step = 0.25 beats (one 16th note in 4/4).
    One full pattern = 16 steps = 4 beats (one bar).
    The pattern loops continuously while the host transport is running.
    MidiLogic.set_step_rows() also receives the same ChannelStepData list so
    note events are baked into _build_flat_events() for sample-accurate timing.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import List, Optional

from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QScrollArea, QSizePolicy, QFileDialog,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import (
    QPainter, QColor, QPen, QFont,
)


# ---------------------------------------------------------------------------
# Data model -- pure Python, no Qt, safe to share with MidiLogic / ProjectManager
# ---------------------------------------------------------------------------

# Counter for unique per-row IDs.  Starts at 32 to avoid clashing with the
# 16 standard MIDI channels (0–15) and leave headroom for future use.
_RACK_ROW_ID_COUNTER = itertools.count(32)


@dataclass
class ChannelStepData:
    """
    Serialisable state for one channel rack row.

    steps[i] = True means a note fires on that 16th-note step.
    """
    name:     str        = "Track"  # Display label in the rack
    channel:  int        = 0        # MIDI channel that receives note events
    note:     int        = 60       # MIDI note number (default: middle C = C4)
    velocity: int        = 100      # Strike velocity 1–127
    enabled:  bool       = True     # Row-level mute (False = all steps silent)
    steps: List[bool] = field(default_factory=lambda: [False] * 16)
    row_id:      int  = field(default_factory=lambda: next(_RACK_ROW_ID_COUNTER))
    on_timeline: bool = False
    # File-system path to the audio sample loaded for this row.  Empty = none.
    sample_path: str  = ""

    def to_dict(self) -> dict:
        return {
            "name":        self.name,
            "channel":     self.channel,
            "note":        self.note,
            "velocity":    self.velocity,
            "enabled":     self.enabled,
            "steps":       list(self.steps),
            "row_id":      self.row_id,
            "on_timeline": self.on_timeline,
            "sample_path": self.sample_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelStepData":
        row = cls(
            name=d.get("name", "Track"),
            channel=d.get("channel", 0),
            note=d.get("note", 60),
            velocity=d.get("velocity", 100),
            enabled=d.get("enabled", True),
        )
        saved = d.get("steps", [])
        for i in range(min(16, len(saved))):
            row.steps[i] = bool(saved[i])
        row.row_id      = int(d.get("row_id",      row.row_id))
        row.on_timeline = bool(d.get("on_timeline", False))
        row.sample_path = str(d.get("sample_path",  ""))
        return row


# ---------------------------------------------------------------------------
# Note name helper
# ---------------------------------------------------------------------------

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _midi_to_name(n: int) -> str:
    return f"{_NOTE_NAMES[n % 12]}{n // 12 - 1}"


# ---------------------------------------------------------------------------
# KnobWidget -- minimal circular drag knob
# ---------------------------------------------------------------------------

class KnobWidget(QWidget):
    """
    Painted circular knob. Click and drag vertically to change value.
    Emits value_changed(float) on every position change.
    """

    value_changed = Signal(float)

    # Arc geometry: 225° at bottom-left, sweeps 270° clockwise
    ARC_START = 225
    ARC_SPAN  = 270

    def __init__(self, min_val: float = 0.0, max_val: float = 1.0,
                 default: float = 0.5, label: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._min   = min_val
        self._max   = max_val
        self._val   = max(min_val, min(max_val, default))
        self._label = label
        self._drag_y:         Optional[float] = None
        self._drag_start_val: float           = self._val

        self.setFixedSize(38, 44)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip(f"{label}: {self._val:.2f}")

    def value(self) -> float:
        return self._val

    def set_value(self, v: float) -> None:
        self._val = max(self._min, min(self._max, v))
        self.update()

    # ── Mouse interaction ─────────────────────────────────────────────────────

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self._drag_y = ev.position().y()
            self._drag_start_val = self._val

    def mouseMoveEvent(self, ev) -> None:
        if self._drag_y is not None:
            # Dragging 100 px maps to the full parameter range
            delta = (self._drag_y - ev.position().y()) / 100.0
            rng   = self._max - self._min
            self._val = max(self._min, min(self._max,
                            self._drag_start_val + delta * rng))
            self.value_changed.emit(self._val)
            self.setToolTip(f"{self._label}: {self._val:.2f}")
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        self._drag_y = None

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        cx, cy = W // 2, H // 2 - 4  # Centre; leave space for label below
        r = 14

        # Dark background circle
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0A0E22"))
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # Full track arc (dim)
        p.setPen(QPen(QColor(61, 90, 128), 2, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(cx - r + 3, cy - r + 3, (r - 3) * 2, (r - 3) * 2,
                  int(self.ARC_START * 16), int(-self.ARC_SPAN * 16))

        # Value arc (cyan)
        norm  = (self._val - self._min) / max(1e-9, self._max - self._min)
        sweep = int(norm * self.ARC_SPAN * 16)
        p.setPen(QPen(QColor("#00E5FF"), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(cx - r + 3, cy - r + 3, (r - 3) * 2, (r - 3) * 2,
                  int(self.ARC_START * 16), -sweep)

        # Small indicator dot at the current angle
        angle_rad = math.radians(self.ARC_START - norm * self.ARC_SPAN)
        dot_x = cx + math.cos(angle_rad) * (r - 5)
        dot_y = cy - math.sin(angle_rad) * (r - 5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#00E5FF"))
        p.drawEllipse(int(dot_x) - 2, int(dot_y) - 2, 4, 4)

        # Label text below the circle
        if self._label:
            p.setPen(QColor(61, 90, 128))
            p.setFont(QFont("Arial", 7))
            p.drawText(0, H - 6, W, 12, Qt.AlignCenter, self._label)

        p.end()


# ---------------------------------------------------------------------------
# StepButton -- single 16th-note step toggle cell
# ---------------------------------------------------------------------------

class StepButton(QPushButton):
    """
    Small square button representing one step.
    Active = lit neon cyan; scanning (playhead here) = solid white;
    inactive = dark.  Groups of 4 steps are visually separated.
    """

    def __init__(self, step_idx: int,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._step_idx  = step_idx
        self._active    = False
        self._scanning  = False  # True when the playhead is on this step

        self.setFixedSize(26, 26)
        self.setCheckable(False)
        self._apply_style()

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self._apply_style()

    def set_scanning(self, scanning: bool) -> None:
        if self._scanning != scanning:
            self._scanning = scanning
            self._apply_style()

    def is_active(self) -> bool:
        return self._active

    def mousePressEvent(self, ev) -> None:
        # Toggle on left-click; the parent row reads is_active() after this.
        if ev.button() == Qt.LeftButton:
            self._active = not self._active
            self._apply_style()
        super().mousePressEvent(ev)

    def _apply_style(self) -> None:
        # Beat-group left border makes 4/4 structure visible
        grp_left = ("2px solid rgba(0,229,255,0.35)"
                    if self._step_idx % 4 == 0
                    else "1px solid rgba(0,229,255,0.25)")

        if self._scanning:
            bg, border, color = "#00E5FF", "#00E5FF", "#030308"
        elif self._active:
            bg     = "rgba(0,229,255,0.30)"
            border = "#00E5FF"
            color  = "#00E5FF"
        else:
            bg     = "#0A0E22"
            border = "rgba(0,229,255,0.20)"
            color  = "#3D5A80"

        self.setStyleSheet(f"""
            QPushButton {{
                background: {bg};
                border: 1px solid {border};
                border-left: {grp_left};
                border-radius: 3px;
                color: {color};
            }}
            QPushButton:hover {{
                border-color: #00E5FF;
                background: rgba(0,229,255,0.18);
            }}
        """)


# ---------------------------------------------------------------------------
# ChannelRackRow -- one instrument row
# ---------------------------------------------------------------------------

class ChannelRackRow(QWidget):
    """
    One horizontal row in the Channel Rack.
    Layout: [Track name | M | Vol | Pan | Note | Sample | 16 step buttons]
    """

    step_toggled          = Signal(int, int, bool)  # row_idx, step_idx, is_active
    volume_changed        = Signal(int, float)       # row_idx, 0.0–2.0
    pan_changed           = Signal(int, float)       # row_idx, -1.0–+1.0
    mute_changed          = Signal(int, bool)        # row_idx, muted
    note_changed          = Signal(int, int)         # row_idx, midi_note
    # Emitted when the user loads a sample for this row.
    sample_load_requested = Signal(int, str)         # row_idx, file_path

    def __init__(self, row_idx: int, data: ChannelStepData,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._row_idx = row_idx
        self._data    = data
        self._step_btns: List[StepButton] = []

        self.setFixedHeight(36)
        self.setStyleSheet("background:#060A18;")
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(3)

        # Track name label (truncated to 12 chars)
        name_lbl = QLabel(self._data.name[:12])
        name_lbl.setFixedWidth(84)
        name_lbl.setStyleSheet(
            "color:#C8E6FF; font-size:10px; background:transparent;")
        lay.addWidget(name_lbl)

        # Mute toggle button
        mute = QPushButton("M")
        mute.setFixedSize(20, 20)
        mute.setCheckable(True)
        mute.setChecked(not self._data.enabled)
        mute.setStyleSheet(self._mute_style(not self._data.enabled))
        mute.clicked.connect(lambda checked, b=mute: self._on_mute(checked, b))
        lay.addWidget(mute)

        # Volume knob (0 – 2, unity at 1)
        vol = KnobWidget(0.0, 2.0, 1.0, "Vol")
        vol.value_changed.connect(lambda v: self.volume_changed.emit(self._row_idx, v))
        lay.addWidget(vol)

        # Pan knob (-1 full left, +1 full right)
        pan = KnobWidget(-1.0, 1.0, 0.0, "Pan")
        pan.value_changed.connect(lambda v: self.pan_changed.emit(self._row_idx, v))
        lay.addWidget(pan)

        # Root-note label; mouse-wheel changes the note
        self._note_lbl = QLabel(_midi_to_name(self._data.note))
        self._note_lbl.setFixedWidth(26)
        self._note_lbl.setAlignment(Qt.AlignCenter)
        self._note_lbl.setStyleSheet(
            "color:#9945FF; font-size:8px; background:transparent;")
        self._note_lbl.setToolTip("Scroll to change root note")
        lay.addWidget(self._note_lbl)

        # Sample button — click to open a file dialog and load an audio sample.
        # Shows the file basename when a sample is already loaded.
        self._sample_btn = QPushButton(self._sample_label())
        self._sample_btn.setFixedSize(64, 22)
        self._sample_btn.setToolTip(
            "Click to load an audio sample for this row.\n"
            "The sample plays whenever this step fires.")
        self._sample_btn.setStyleSheet(
            "QPushButton { background:#0A0E22; color:#9945FF;"
            " border:1px solid rgba(153,69,255,0.4); border-radius:3px;"
            " font-size:8px; padding:0 2px; }"
            "QPushButton:hover { background:rgba(153,69,255,0.18); }")
        self._sample_btn.clicked.connect(self._on_load_sample)
        lay.addWidget(self._sample_btn)

        # 16 step toggle buttons
        for i in range(16):
            btn = StepButton(i)
            btn.set_active(self._data.steps[i])
            btn.clicked.connect(self._make_step_handler(i, btn))
            self._step_btns.append(btn)
            lay.addWidget(btn)

        lay.addStretch()

    def _sample_label(self) -> str:
        """Short label shown on the sample button."""
        import os as _os
        if self._data.sample_path and _os.path.isfile(self._data.sample_path):
            name = _os.path.basename(self._data.sample_path)
            # Truncate to ~10 chars so it fits in 64 px
            return name[:10] + "…" if len(name) > 10 else name
        return "▶ SAMPLE"

    def _on_load_sample(self) -> None:
        """Open a file dialog and emit the chosen path."""
        import os as _os
        start_dir = (_os.path.dirname(self._data.sample_path)
                     if self._data.sample_path else "")
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Load Sample — {self._data.name}",
            start_dir,
            "Audio Files (*.wav *.flac *.ogg *.aiff *.aif *.mp3);;All Files (*)",
        )
        if path:
            self._data.sample_path = path
            self._sample_btn.setText(self._sample_label())
            self.sample_load_requested.emit(self._row_idx, path)

    def _make_step_handler(self, step_idx: int,
                           btn: StepButton):
        """Return a closure that correctly captures step_idx and btn."""
        def handler():
            # StepButton.mousePressEvent already toggled _active; sync data.
            self._data.steps[step_idx] = btn.is_active()
            self.step_toggled.emit(self._row_idx, step_idx, btn.is_active())
        return handler

    def set_scan_step(self, step_idx: int) -> None:
        """Highlight the currently playing column, dim all others."""
        for i, btn in enumerate(self._step_btns):
            btn.set_scanning(i == step_idx)

    def _on_mute(self, checked: bool, btn: QPushButton) -> None:
        self._data.enabled = not checked
        btn.setStyleSheet(self._mute_style(checked))
        self.mute_changed.emit(self._row_idx, checked)

    @staticmethod
    def _mute_style(muted: bool) -> str:
        if muted:
            return ("QPushButton { background:#FF2D9E; color:#030308;"
                    " border:1px solid #FF2D9E; border-radius:3px; font-size:9px; }")
        return ("QPushButton { background:#0A0E22; color:#3D5A80;"
                " border:1px solid rgba(0,229,255,0.22); border-radius:3px;"
                " font-size:9px; }"
                "QPushButton:hover { border-color:#00E5FF; color:#00E5FF; }")

    def wheelEvent(self, ev) -> None:
        """Scroll over the note label to shift the root note up or down."""
        note_rect = self._note_lbl.geometry()
        if note_rect.contains(ev.position().toPoint()):
            delta = 1 if ev.angleDelta().y() > 0 else -1
            self._data.note = max(0, min(127, self._data.note + delta))
            self._note_lbl.setText(_midi_to_name(self._data.note))
            self.note_changed.emit(self._row_idx, self._data.note)


# ---------------------------------------------------------------------------
# ChannelRackWindow -- main dialog
# ---------------------------------------------------------------------------

_DIALOG_STYLE = """
    QDialog {
        background: #030308;
        border: 1px solid rgba(0,229,255,0.28);
    }
    QScrollArea { background: #060A18; border: none; }
"""


class ChannelRackWindow(QDialog):
    """
    Floating Channel Rack window with an INDEPENDENT play button.

    The rack has its own BPM-synced QTimer.  Pressing PLAY in the rack
    plays only the step pattern; pressing PLAY on the main transport plays
    only the timeline clips.  The two transports do NOT interfere.

    Signals:
        note_on_requested(row_id, note, velocity)  -- step fired a note
        note_off_requested(row_id, note)            -- step note released
        copy_requested(list[ChannelStepData])       -- user clicked Copy to Timeline
        sample_load_requested(row_id, path)         -- user loaded a sample for a row
    """

    # Signals use row_id (≥ 32) so MainWindow can route to RackSamplerEngine.
    note_on_requested     = Signal(int, int, int)  # row_id, note, velocity
    note_off_requested    = Signal(int, int)        # row_id, note
    copy_requested        = Signal(list)            # list[ChannelStepData]
    sample_load_requested = Signal(int, str)        # row_id, path

    STEP_BEATS = 0.25   # One 16th note in beats

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.Window | Qt.WindowCloseButtonHint)
        self.setWindowTitle("Crystal DAW  ·  Channel Rack")
        self.resize(1000, 380)
        self.setStyleSheet(_DIALOG_STYLE)

        self._rows:        List[ChannelStepData]  = []
        self._row_widgets: List[ChannelRackRow]   = []

        # ── Rack-independent transport state ──────────────────────────────────
        self._rack_playing: bool  = False   # True while rack's own timer runs
        self._rack_beat:    float = 0.0     # current beat in the rack pattern
        self._rack_bpm:     float = 120.0   # mirrors main BPM
        self._last_step:    int   = -1      # last fired step (for edge detection)

        # QTimer drives the rack's own playback loop at ~20 Hz.
        self._rack_timer = QTimer(self)
        self._rack_timer.setInterval(50)   # 50 ms ≈ 20 Hz
        self._rack_timer.timeout.connect(self._on_rack_tick)

        self._build_ui()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_rows(self, rows: List[ChannelStepData]) -> None:
        """Replace all rows (e.g. after a project load)."""
        self._rows = rows
        self._rebuild_row_widgets()

    def get_rows(self) -> List[ChannelStepData]:
        return list(self._rows)

    def add_row(self, name: str = "Track", channel: int = 0) -> None:
        """Append a new empty row to the rack."""
        row = ChannelStepData(name=name, channel=channel)
        self._rows.append(row)
        self._add_row_widget(len(self._rows) - 1, row)

    def set_bpm(self, bpm: float) -> None:
        """Sync the rack's internal BPM to the main transport BPM."""
        self._rack_bpm = max(20.0, float(bpm))

    def set_playhead_beat(self, beat: float) -> None:
        """
        Update the scanning light to the given beat position.

        Called from MainWindow._on_refresh_tick so the light follows the
        main transport even when the rack is not independently playing.
        NOTE: this method never fires notes — that is the rack timer's job.
        """
        pattern_beats = 16 * self.STEP_BEATS
        step_idx      = int((beat % pattern_beats) / self.STEP_BEATS) % 16
        for widget in self._row_widgets:
            widget.set_scan_step(step_idx)

    # ── Rack-own transport (independent from main timeline) ───────────────────

    def _on_rack_tick(self) -> None:
        """
        Called every 50 ms by the rack's own QTimer.

        Advances _rack_beat and fires note events for any steps that
        transition on this tick.  Completely independent of the main transport.
        """
        # Advance by the beat equivalent of one 50 ms frame.
        dt_beats = (self._rack_timer.interval() / 1000.0) * self._rack_bpm / 60.0
        self._rack_beat = (self._rack_beat + dt_beats) % (16 * self.STEP_BEATS)

        pattern_beats = 16 * self.STEP_BEATS
        step_idx      = int((self._rack_beat % pattern_beats) / self.STEP_BEATS) % 16

        # Update scanning light from the rack's own beat
        for widget in self._row_widgets:
            widget.set_scan_step(step_idx)

        # Fire note events only when the step index advances
        if step_idx == self._last_step:
            return

        # Release notes from the previous step
        if self._last_step >= 0:
            for row in self._rows:
                if row.enabled and not row.on_timeline \
                        and self._last_step < 16 \
                        and row.steps[self._last_step]:
                    # Emit row_id so MainWindow routes to RackSamplerEngine
                    self.note_off_requested.emit(row.row_id, row.note)

        # Trigger notes for the new step
        for row in self._rows:
            if row.enabled and not row.on_timeline and row.steps[step_idx]:
                self.note_on_requested.emit(row.row_id, row.note, row.velocity)

        self._last_step = step_idx

    def _on_rack_play_stop(self) -> None:
        """Toggle the rack's independent playback."""
        if self._rack_playing:
            # Stop
            self._rack_playing = False
            self._rack_timer.stop()
            self._release_all_notes()
            self._last_step  = -1
            self._rack_beat  = 0.0
            for widget in self._row_widgets:
                widget.set_scan_step(-1)
            self._play_btn.setText("▶ PLAY")
            self._play_btn.setStyleSheet(self._play_btn_style(False))
        else:
            # Start
            self._rack_playing = True
            self._rack_beat    = 0.0
            self._last_step    = -1
            self._rack_timer.start()
            self._play_btn.setText("■ STOP")
            self._play_btn.setStyleSheet(self._play_btn_style(True))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Title bar
        title = QLabel("  ⟡  CHANNEL RACK  /  STEP SEQUENCER")
        title.setFixedHeight(28)
        title.setStyleSheet(
            "background:#060A18; color:#00E5FF; font-size:12px;"
            " font-weight:bold; padding-left:4px;"
            " border-bottom:1px solid rgba(0,229,255,0.25);")
        outer.addWidget(title)

        # Column header labels aligned with row layout
        outer.addWidget(self._build_column_header())

        # Scrollable rows area
        self._content     = QWidget()
        self._content_lay = QVBoxLayout(self._content)
        self._content_lay.setContentsMargins(0, 0, 0, 0)
        self._content_lay.setSpacing(1)
        self._content_lay.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(self._content)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("background:#060A18; border:none;")
        outer.addWidget(scroll, stretch=1)

        # Bottom toolbar
        outer.addWidget(self._build_toolbar())

    def _build_column_header(self) -> QWidget:
        """Thin header bar with column labels aligned to ChannelRackRow layout."""
        hdr = QWidget()
        hdr.setFixedHeight(18)
        hdr.setStyleSheet(
            "background:#0A0E22; border-bottom:1px solid rgba(0,229,255,0.15);")
        lay = QHBoxLayout(hdr)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(3)

        def _lbl(text: str, w: int) -> QLabel:
            l = QLabel(text)
            l.setFixedWidth(w)
            l.setStyleSheet("color:#3D5A80; font-size:8px; background:transparent;")
            l.setAlignment(Qt.AlignCenter)
            return l

        # Mirror the row widget layout widths (must match ChannelRackRow._build_ui)
        lay.addWidget(_lbl("TRACK",  84))
        lay.addWidget(_lbl("M",      20))
        lay.addWidget(_lbl("VOL",    38))
        lay.addWidget(_lbl("PAN",    38))
        lay.addWidget(_lbl("NOTE",   26))
        lay.addWidget(_lbl("SAMPLE", 64))

        # Step number labels 1–16; beat-group markers in cyan
        for i in range(16):
            l = QLabel(str(i + 1))
            l.setFixedSize(26, 18)
            l.setAlignment(Qt.AlignCenter)
            color = "#00E5FF" if i % 4 == 0 else "#3D5A80"
            l.setStyleSheet(
                f"color:{color}; font-size:7px; background:transparent;")
            lay.addWidget(l)

        lay.addStretch()
        return hdr

    @staticmethod
    def _play_btn_style(playing: bool) -> str:
        """Return the stylesheet for the PLAY/STOP button."""
        if playing:
            return ("QPushButton { background:#FF2D9E; color:#030308;"
                    " border:1px solid #FF2D9E; border-radius:4px;"
                    " padding:0 14px; font-size:10px; font-weight:bold; }"
                    "QPushButton:hover { background:#FF5AB8; }")
        return ("QPushButton { background:rgba(0,229,255,0.12); color:#00E5FF;"
                " border:1px solid rgba(0,229,255,0.5); border-radius:4px;"
                " padding:0 14px; font-size:10px; font-weight:bold; }"
                "QPushButton:hover { background:rgba(0,229,255,0.25); }")

    def _build_toolbar(self) -> QWidget:
        """
        Bottom action bar:
            [▶ PLAY] [+ ADD TRACK] [CLEAR ALL]  ··· [COPY TO TIMELINE]

        The PLAY button belongs to the rack's own transport — it is
        completely independent of the main timeline PLAY button.
        """
        bar = QWidget()
        bar.setFixedHeight(36)
        bar.setStyleSheet(
            "background:#060A18; border-top:1px solid rgba(0,229,255,0.18);")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)

        def _btn(label: str, tip: str, cb, color: str = "#00E5FF") -> QPushButton:
            r, g, b = self._hex_to_rgb(color)
            b_ = QPushButton(label)
            b_.setFixedHeight(24)
            b_.setToolTip(tip)
            b_.setStyleSheet(
                f"QPushButton {{ background:rgba(0,0,0,0); color:{color};"
                f" border:1px solid rgba({r},{g},{b},0.4); border-radius:4px;"
                f" padding:0 10px; font-size:10px; }}"
                f"QPushButton:hover {{ background:rgba({r},{g},{b},0.15); }}")
            b_.clicked.connect(cb)
            return b_

        # Independent rack PLAY / STOP button
        self._play_btn = QPushButton("▶ PLAY")
        self._play_btn.setFixedHeight(24)
        self._play_btn.setToolTip(
            "Play / stop the channel rack independently from the main timeline.")
        self._play_btn.setStyleSheet(self._play_btn_style(False))
        self._play_btn.clicked.connect(self._on_rack_play_stop)
        lay.addWidget(self._play_btn)

        lay.addWidget(_btn(
            "+ ADD TRACK", "Add a new sequencer row",
            lambda: self.add_row(f"Track {len(self._rows) + 1}",
                                 len(self._rows) % 16)))
        lay.addWidget(_btn(
            "CLEAR ALL", "Clear all steps in every row",
            self._on_clear_all, "#FF2D9E"))
        lay.addStretch()
        lay.addWidget(_btn(
            "COPY TO TIMELINE",
            "Export current 1-bar pattern as MIDI clips on the main timeline",
            self._on_copy_to_timeline, "#9945FF"))
        return bar

    # ── Row widget management ─────────────────────────────────────────────────

    def _rebuild_row_widgets(self) -> None:
        """Destroy and recreate all row widgets from self._rows."""
        for w in self._row_widgets:
            self._content_lay.removeWidget(w)
            w.deleteLater()
        self._row_widgets.clear()
        for idx, row in enumerate(self._rows):
            self._add_row_widget(idx, row)

    def _add_row_widget(self, idx: int, data: ChannelStepData) -> None:
        widget = ChannelRackRow(idx, data)
        widget.step_toggled.connect(self._on_step_toggled)
        # Forward the sample-load signal, converting row_idx → row_id
        widget.sample_load_requested.connect(self._on_row_sample_loaded)
        self._row_widgets.append(widget)
        # Insert before the stretch item at the end of the layout
        self._content_lay.insertWidget(self._content_lay.count() - 1, widget)

    def _on_row_sample_loaded(self, row_idx: int, path: str) -> None:
        """Convert row_idx to row_id and re-emit at the window level."""
        if 0 <= row_idx < len(self._rows):
            row = self._rows[row_idx]
            row.sample_path = path
            # Emit row_id (not idx) so MainWindow can key into RackSamplerEngine
            self.sample_load_requested.emit(row.row_id, path)

    # ── Toolbar button handlers ───────────────────────────────────────────────

    def _on_step_toggled(self, row_idx: int, step_idx: int,
                         active: bool) -> None:
        """Step button toggled -- data already updated inside StepButton."""
        pass   # ChannelStepData.steps[step_idx] is already live

    def _on_clear_all(self) -> None:
        """Reset all steps in all rows to inactive."""
        for row in self._rows:
            row.steps = [False] * 16
            row.on_timeline = False
        for widget in self._row_widgets:
            widget.set_scan_step(-1)
            for btn in widget._step_btns:
                btn.set_active(False)

    def _on_copy_to_timeline(self) -> None:
        """Emit the current row list; MainWindow will create a MidiClip."""
        self.copy_requested.emit(list(self._rows))

    def _release_all_notes(self) -> None:
        """Send note-off for all rows that could have a held note."""
        for row in self._rows:
            if row.enabled:
                # Use row_id so MainWindow routes to the correct sampler engine
                self.note_off_requested.emit(row.row_id, row.note)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _hex_to_rgb(hex_col: str) -> tuple:
        h = hex_col.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
