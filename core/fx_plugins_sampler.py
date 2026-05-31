"""
fx_plugins_sampler.py  --  Simple Sampler Instrument Plugin
============================================================
Polyphonic sample-playback instrument backed by the C++ Sampler engine.
Drag-and-drop a WAV/FLAC/OGG onto the waveform display or click
"Load Sample" to load it.  The MIDI system calls note_on() / note_off()
and process() renders the audio into the DAW's stream.

Design note — metaclass safety
-------------------------------
FxPluginBase uses ABCMeta; QWidget uses PySide6's Shiboken metaclass.
Mixing them in one class causes a metaclass conflict at import time.
The solution is a strict separation:

    SamplerPlugin(FxPluginBase)   -- pure DSP + param storage, no Qt base
    _SamplerWidget(QWidget)       -- all UI; holds a reference to its plugin

create_parameter_widget() instantiates a fresh _SamplerWidget and stores a
weakref so set_params() can later sync the UI when a project is loaded.

Feature 3 — Instrument Preview
--------------------------------
While the plugin panel is focused, computer keyboard keys trigger note_on /
note_off so you can audition the sample without a MIDI controller.

Standard piano-keyboard layout (starting at C4 = MIDI 60):
    White keys:  A  S  D  F  G  H  J  K  L
                 C4 D4 E4 F4 G4 A4 B4 C5 D5
    Black keys:  W  E     T  Y  U     O
                 C# D#    F# G# A#    C#
"""

from __future__ import annotations

import os
import weakref
from typing import Optional

import numpy as np

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSlider,
    QFileDialog, QSizePolicy, QGroupBox,
)
from PySide6.QtCore import Qt, Signal, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QPolygonF, QFont, QKeyEvent

try:
    import daw_processors
    _CPP_OK = True
except ImportError:
    _CPP_OK = False

from .fx_plugin_base import FxPluginBase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _midi_to_note_name(midi: int) -> str:
    return f"{_NOTE_NAMES[midi % 12]}{(midi // 12) - 1}"


def _load_audio(path: str):
    """
    Load an audio file → (flat float32 numpy array, sample_rate, channels).
    Returns (None, 0, 0) on failure.
    """
    try:
        import soundfile as sf
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        return data.T.flatten(), sr, data.shape[1]
    except Exception:
        pass
    try:
        from scipy.io import wavfile
        sr, data = wavfile.read(path)
        if data.dtype == np.int16:
            data = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            data = data.astype(np.float32) / 2_147_483_648.0
        elif data.dtype == np.uint8:
            data = (data.astype(np.float32) - 128.0) / 128.0
        else:
            data = data.astype(np.float32)
        if data.ndim == 1:
            return data, sr, 1
        return data.T.flatten().astype(np.float32), sr, data.shape[1]
    except Exception:
        pass
    return None, 0, 0


# ---------------------------------------------------------------------------
# Preview keyboard mapping  (Qt.Key → (midi_note, button_label))
# ---------------------------------------------------------------------------
_PREVIEW_KEY_MAP: dict[int, tuple[int, str]] = {
    Qt.Key_A: (60, "A\nC4"),
    Qt.Key_W: (61, "W\nC#"),
    Qt.Key_S: (62, "S\nD4"),
    Qt.Key_E: (63, "E\nD#"),
    Qt.Key_D: (64, "D\nE4"),
    Qt.Key_F: (65, "F\nF4"),
    Qt.Key_T: (66, "T\nF#"),
    Qt.Key_G: (67, "G\nG4"),
    Qt.Key_Y: (68, "Y\nG#"),
    Qt.Key_H: (69, "H\nA4"),
    Qt.Key_U: (70, "U\nA#"),
    Qt.Key_J: (71, "J\nB4"),
    Qt.Key_K: (72, "K\nC5"),
    Qt.Key_O: (73, "O\nC#5"),
    Qt.Key_L: (74, "L\nD5"),
}

# Left-to-right display order for the preview keyboard strip.
_KEY_ORDER = [
    Qt.Key_A, Qt.Key_W, Qt.Key_S, Qt.Key_E, Qt.Key_D,
    Qt.Key_F, Qt.Key_T, Qt.Key_G, Qt.Key_Y, Qt.Key_H,
    Qt.Key_U, Qt.Key_J, Qt.Key_K, Qt.Key_O, Qt.Key_L,
]


# ---------------------------------------------------------------------------
# Waveform display widget
# ---------------------------------------------------------------------------

class WaveformDisplay(QWidget):
    """Peak-envelope waveform view with drag-and-drop audio file support."""

    file_dropped = Signal(str)
    _SUPPORTED_EXTS = {".wav", ".flac", ".ogg", ".aiff", ".aif", ".mp3"}

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._peaks_pos: list[float] = []
        self._peaks_neg: list[float] = []
        self._filename  = ""
        self._loaded    = False
        self._hover     = False

        self._col_bg   = QColor(20, 20, 28)
        self._col_wave = QColor(0, 210, 220)
        self._col_mid  = QColor(60, 60, 80)
        self._col_lbl  = QColor(140, 140, 160)
        self._col_bdr  = QColor(50, 50, 70)
        self._col_hl   = QColor(0, 180, 255, 60)

    def set_waveform(self, mono: np.ndarray, filename: str) -> None:
        self._filename = os.path.basename(filename)
        self._loaded   = True
        self._build_peaks(mono)
        self.update()

    def clear_waveform(self) -> None:
        self._loaded    = False
        self._filename  = ""
        self._peaks_pos = []
        self._peaks_neg = []
        self.update()

    def _build_peaks(self, data: np.ndarray) -> None:
        w = max(self.width(), 1)
        n = len(data)
        pos, neg = [], []
        for col in range(w):
            i0 = int(col * n / w)
            i1 = min(max(int((col + 1) * n / w), i0 + 1), n)
            chunk = data[i0:i1]
            pos.append(float(np.max(chunk)))
            neg.append(float(np.min(chunk)))
        self._peaks_pos = pos
        self._peaks_neg = neg

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()
        mid  = h // 2

        p.fillRect(0, 0, w, h, self._col_bg)
        if self._hover:
            p.fillRect(0, 0, w, h, self._col_hl)
        p.setPen(QPen(self._col_mid, 1))
        p.drawLine(0, mid, w, mid)

        if self._loaded and len(self._peaks_pos) == w:
            poly = QPolygonF()
            for x, y in enumerate(self._peaks_pos):
                poly.append(QPointF(x, mid - int(y * (mid - 2))))
            for x in range(w - 1, -1, -1):
                poly.append(QPointF(x, mid - int(self._peaks_neg[x] * (mid - 2))))
            fill = QColor(self._col_wave)
            fill.setAlpha(180)
            p.setBrush(fill)
            p.setPen(Qt.NoPen)
            p.drawPolygon(poly)
            p.setPen(QPen(self._col_wave, 1))
            p.setBrush(Qt.NoBrush)
            for x in range(w - 1):
                p.drawLine(x, mid - int(self._peaks_pos[x] * (mid-2)),
                           x+1, mid - int(self._peaks_pos[x+1] * (mid-2)))
            p.setFont(QFont("Consolas", 8))
            p.setPen(self._col_lbl)
            p.drawText(6, h - 6, self._filename)
        else:
            p.setFont(QFont("Consolas", 9))
            p.setPen(self._col_lbl)
            p.drawText(0, 0, w, h, Qt.AlignCenter,
                       "Drop an audio file here  —  or click  Load Sample")

        p.setPen(QPen(self._col_bdr, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()

    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            for url in ev.mimeData().urls():
                if os.path.splitext(url.toLocalFile())[1].lower() \
                        in self._SUPPORTED_EXTS:
                    ev.acceptProposedAction()
                    self._hover = True
                    self.update()
                    return
        ev.ignore()

    def dragLeaveEvent(self, ev) -> None:
        self._hover = False
        self.update()
        super().dragLeaveEvent(ev)

    def dropEvent(self, ev) -> None:
        self._hover = False
        self.update()
        for url in ev.mimeData().urls():
            p = url.toLocalFile()
            if os.path.splitext(p)[1].lower() in self._SUPPORTED_EXTS \
                    and os.path.isfile(p):
                self.file_dropped.emit(p)
                ev.acceptProposedAction()
                return
        ev.ignore()


# ---------------------------------------------------------------------------
# _SamplerWidget  --  the QWidget returned by create_parameter_widget()
# ---------------------------------------------------------------------------

class _SamplerWidget(QWidget):
    """
    Parameter panel for SamplerPlugin.

    Owns all Qt controls.  Holds a direct reference to its SamplerPlugin so
    slider callbacks can write back to the plugin's public param attributes
    and drive the C++ Sampler engine.
    """

    # Colour tokens
    _C_BG     = "#14141C"
    _C_LABEL  = "#8888AA"
    _C_VALUE  = "#CCCCEE"
    _C_ACCENT = "#00D2DC"
    _C_BTN    = "#2A2A3C"
    _C_BTN_ON = "#00AACC"
    _C_KEY_WH = "#2A2A3C"
    _C_KEY_BK = "#0A0A14"

    def __init__(self, plugin: "SamplerPlugin",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._plugin = plugin
        self._held_keys: dict[int, int] = {}   # Qt.Key → midi_note currently held
        self._preview_btns: dict[int, QPushButton] = {}

        self.setFocusPolicy(Qt.StrongFocus)
        self._build_ui()
        self.sync_to_plugin()

    # ── Public sync (called after set_params) ─────────────────────────────────

    def sync_to_plugin(self) -> None:
        """Refresh all slider / label values from the plugin's stored params."""
        p = self._plugin
        self._root_slider.setValue(int(p.root_note))
        self._sliders["attack"].setValue(int(p.attack_ms))
        self._sliders["decay"].setValue(int(p.decay_ms))
        self._sliders["sustain"].setValue(int(p.sustain_lvl * 100))
        self._sliders["release"].setValue(int(p.release_ms))
        if p._loaded_path and os.path.isfile(p._loaded_path):
            self._filename_lbl.setText(os.path.basename(p._loaded_path))

    # ── Keyboard preview ──────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return
        key = event.key()
        entry = _PREVIEW_KEY_MAP.get(key)
        if entry is not None:
            midi_note = entry[0]
            self._held_keys[key] = midi_note
            self._plugin.note_on(midi_note, 0.8)
            self._set_key_color(key, pressed=True)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return
        key = event.key()
        midi_note = self._held_keys.pop(key, None)
        if midi_note is not None:
            self._plugin.note_off(midi_note)
            self._set_key_color(key, pressed=False)
        else:
            super().keyReleaseEvent(event)

    def _set_key_color(self, key: int, pressed: bool) -> None:
        btn = self._preview_btns.get(key)
        if btn is None:
            return
        midi_note = _PREVIEW_KEY_MAP[key][0]
        is_black  = _NOTE_NAMES[midi_note % 12].endswith("#")
        if pressed:
            btn.setStyleSheet(
                f"background:{self._C_ACCENT}; color:#000;"
                " border:1px solid #00E5FF; border-radius:3px;")
        else:
            base = self._C_KEY_BK if is_black else self._C_KEY_WH
            btn.setStyleSheet(
                f"background:{base}; color:{self._C_VALUE};"
                " border:1px solid #303050; border-radius:3px; font-size:8px;")

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setStyleSheet(f"""
            QWidget     {{ background:{self._C_BG}; color:{self._C_VALUE};
                           font-family:Consolas; font-size:11px; }}
            QGroupBox   {{ border:1px solid #303050; border-radius:4px;
                           margin-top:8px; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:8px;
                                color:{self._C_LABEL}; }}
            QPushButton {{ background:{self._C_BTN}; border:1px solid #404060;
                           border-radius:3px; padding:3px 8px;
                           color:{self._C_VALUE}; }}
            QPushButton:hover   {{ background:#383858; }}
            QPushButton:pressed {{ background:{self._C_BTN_ON}; color:#000; }}
            QSlider::groove:horizontal {{ height:4px; background:#282838;
                                         border-radius:2px; }}
            QSlider::handle:horizontal {{
                width:12px; height:12px; margin:-4px 0;
                background:{self._C_ACCENT}; border-radius:6px; }}
            QSlider::sub-page:horizontal {{ background:{self._C_BTN_ON};
                                           border-radius:2px; }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # ── Title ──────────────────────────────────────────────────────────────
        row = QHBoxLayout()
        t = QLabel("SAMPLER")
        t.setStyleSheet(f"color:{self._C_ACCENT}; font-size:13px; font-weight:bold;")
        row.addWidget(t)
        row.addStretch()
        lay.addLayout(row)

        # ── File load ──────────────────────────────────────────────────────────
        frow = QHBoxLayout()
        btn  = QPushButton("Load Sample")
        btn.setFixedWidth(110)
        btn.clicked.connect(self._on_load_btn)
        self._filename_lbl = QLabel("(no sample loaded)")
        self._filename_lbl.setStyleSheet(f"color:{self._C_LABEL};")
        self._filename_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        frow.addWidget(btn)
        frow.addWidget(self._filename_lbl)
        lay.addLayout(frow)

        # ── Waveform display ───────────────────────────────────────────────────
        self._waveform = WaveformDisplay()
        self._waveform.file_dropped.connect(self._plugin.load_sample)
        # Patch load_sample so widget updates whenever the plugin loads a file.
        self._plugin._widget_load_hook = self._on_sample_loaded
        lay.addWidget(self._waveform)

        # ── Root note ──────────────────────────────────────────────────────────
        rg  = QGroupBox("Root Note")
        rl  = QHBoxLayout(rg)
        self._root_slider = QSlider(Qt.Horizontal)
        self._root_slider.setRange(0, 127)
        self._root_slider.setValue(self._plugin.root_note)
        self._root_slider.setTickInterval(12)
        self._root_slider.setTickPosition(QSlider.TicksBelow)
        self._root_lbl = QLabel(
            f"{_midi_to_note_name(self._plugin.root_note)} ({self._plugin.root_note})")
        self._root_lbl.setFixedWidth(80)
        self._root_lbl.setAlignment(Qt.AlignCenter)
        self._root_slider.valueChanged.connect(self._on_root)
        rl.addWidget(QLabel("Note:"))
        rl.addWidget(self._root_slider)
        rl.addWidget(self._root_lbl)
        lay.addWidget(rg)

        # ── ADSR ───────────────────────────────────────────────────────────────
        ag  = QGroupBox("ADSR Envelope")
        al  = QVBoxLayout(ag)
        al.setSpacing(4)
        self._sliders: dict[str, QSlider] = {}
        self._val_lbls: dict[str, QLabel] = {}

        adsr_defs = [
            # (key,      display,  lo,    hi,    plugin_attr,          suffix)
            ("attack",  "Attack",   0,  5000, "attack_ms",             " ms"),
            ("decay",   "Decay",    0,  5000, "decay_ms",              " ms"),
            ("sustain", "Sustain",  0,   100, "sustain_lvl_x100",      "%"),
            ("release", "Release",  0, 10000, "release_ms",            " ms"),
        ]
        for key, lbl_txt, lo, hi, _, suffix in adsr_defs:
            init = {
                "attack":  int(self._plugin.attack_ms),
                "decay":   int(self._plugin.decay_ms),
                "sustain": int(self._plugin.sustain_lvl * 100),
                "release": int(self._plugin.release_ms),
            }[key]

            hrow = QHBoxLayout()
            lbl  = QLabel(f"{lbl_txt}:")
            lbl.setFixedWidth(56)
            lbl.setStyleSheet(f"color:{self._C_LABEL};")
            sl   = QSlider(Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(init)
            vl   = QLabel(f"{init}{suffix}")
            vl.setFixedWidth(72)
            vl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

            def _handler(v, k=key, vll=vl, sfx=suffix):
                vll.setText(f"{v}{sfx}")
                self._on_adsr(k, v)

            sl.valueChanged.connect(_handler)
            hrow.addWidget(lbl)
            hrow.addWidget(sl)
            hrow.addWidget(vl)
            al.addLayout(hrow)
            self._sliders[key]   = sl
            self._val_lbls[key]  = vl

        lay.addWidget(ag)

        # ── Voice counter ──────────────────────────────────────────────────────
        self._voice_lbl = QLabel("Voices: 0 / 8")
        self._voice_lbl.setStyleSheet(f"color:{self._C_LABEL}; font-size:10px;")
        self._voice_lbl.setAlignment(Qt.AlignRight)
        lay.addWidget(self._voice_lbl)

        # ── Preview keyboard ───────────────────────────────────────────────────
        pg  = QGroupBox("Preview  (click here, then press keys A–L / W E T Y U O)")
        pl  = QHBoxLayout(pg)
        pl.setSpacing(2)
        pl.setContentsMargins(4, 4, 4, 4)

        for qkey in _KEY_ORDER:
            entry     = _PREVIEW_KEY_MAP[qkey]
            midi_note = entry[0]
            label     = entry[1]
            is_black  = _NOTE_NAMES[midi_note % 12].endswith("#")
            base      = self._C_KEY_BK if is_black else self._C_KEY_WH
            h         = 38 if is_black else 50

            b = QPushButton(label)
            b.setFixedWidth(32)
            b.setFixedHeight(h)
            b.setStyleSheet(
                f"background:{base}; color:{self._C_VALUE};"
                " border:1px solid #303050; border-radius:3px; font-size:8px;")
            b.pressed.connect( lambda mn=midi_note: self._plugin.note_on(mn, 0.8))
            b.released.connect(lambda mn=midi_note: self._plugin.note_off(mn))
            pl.addWidget(b, alignment=Qt.AlignBottom)
            self._preview_btns[qkey] = b

        lay.addWidget(pg)
        lay.addStretch()

        # If a sample is already loaded (e.g. after set_params), show it.
        if self._plugin._loaded_path and os.path.isfile(self._plugin._loaded_path):
            self._filename_lbl.setText(
                os.path.basename(self._plugin._loaded_path))

    # ── Slot handlers ─────────────────────────────────────────────────────────

    def _on_load_btn(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Sample",
            os.path.dirname(self._plugin._loaded_path) or "",
            "Audio Files (*.wav *.flac *.ogg *.aiff *.aif *.mp3);;All Files (*)",
        )
        if path:
            self._plugin.load_sample(path)

    def _on_sample_loaded(self, mono: np.ndarray, path: str) -> None:
        """Called by SamplerPlugin.load_sample() via the hook."""
        self._filename_lbl.setText(os.path.basename(path))
        self._waveform.set_waveform(mono, path)

    def _on_root(self, value: int) -> None:
        self._root_lbl.setText(f"{_midi_to_note_name(value)} ({value})")
        self._plugin.root_note = value
        if self._plugin._proc is not None:
            self._plugin._proc.set_root_note(value)
        self._plugin._notify()

    def _on_adsr(self, key: str, value: int) -> None:
        p = self._plugin
        if key == "attack":
            p.attack_ms = float(value)
            if p._proc is not None:
                p._proc.set_attack_ms(p.attack_ms)
        elif key == "decay":
            p.decay_ms = float(value)
            if p._proc is not None:
                p._proc.set_decay_ms(p.decay_ms)
        elif key == "sustain":
            p.sustain_lvl = value / 100.0
            if p._proc is not None:
                p._proc.set_sustain(p.sustain_lvl)
        elif key == "release":
            p.release_ms = float(value)
            if p._proc is not None:
                p._proc.set_release_ms(p.release_ms)
        p._notify()

    def update_voice_count(self) -> None:
        if self._plugin._proc is not None:
            n = self._plugin._proc.active_voice_count()
            self._voice_lbl.setText(f"Voices: {n} / 8")


# ---------------------------------------------------------------------------
# SamplerPlugin  --  pure FxPluginBase subclass (no Qt base, no metaclass clash)
# ---------------------------------------------------------------------------

class SamplerPlugin(FxPluginBase):
    """
    Polyphonic sample-playback instrument.

    DSP + parameter storage only — no QWidget inheritance.
    create_parameter_widget() returns a _SamplerWidget instance.

    Public serialisable attributes (picked up by FxPluginBase.get_params()):
        root_note   (int)   MIDI note that plays the sample at original pitch.
        attack_ms   (float) ADSR attack  time in ms.
        decay_ms    (float) ADSR decay   time in ms.
        sustain_lvl (float) ADSR sustain level 0..1.
        release_ms  (float) ADSR release time in ms.
    """

    DISPLAY_NAME = "Sampler"

    def __init__(self, sample_rate: float = 44100.0) -> None:
        super().__init__()
        self._sample_rate = float(sample_rate)

        # ── Public serialisable parameters ────────────────────────────────────
        self.root_note:   int   = 60
        self.attack_ms:   float = 5.0
        self.decay_ms:    float = 100.0
        self.sustain_lvl: float = 0.8
        self.release_ms:  float = 300.0

        # ── Private state ─────────────────────────────────────────────────────
        self._loaded_path: str = ""

        # Hook called by load_sample() to update the widget when it is open.
        # Assigned by _SamplerWidget after construction.
        self._widget_load_hook = None

        # Weak-ref to the most recently created _SamplerWidget.
        self._widget_ref = None

        # C++ Sampler engine (or pure-Python fallback when C++ is unavailable).
        if _CPP_OK:
            self._proc = daw_processors.Sampler(self._sample_rate)
        else:
            from .sampler_python import PythonSampler
            self._proc = PythonSampler(self._sample_rate)

    # ── FxPluginBase contract ─────────────────────────────────────────────────

    def create_parameter_widget(self) -> QWidget:
        """
        Instantiate (or re-instantiate) the parameter panel.

        A new _SamplerWidget is created each time so it is always fresh
        when the FX rack opens this slot.  A weakref is stored so
        set_params() can sync the UI if a project is loaded while the
        panel is open.
        """
        widget = _SamplerWidget(self)
        self._widget_ref = weakref.ref(widget)
        return widget

    def process(self, audio: np.ndarray, sample_rate: float) -> np.ndarray:
        """Add sampler output to audio.  audio shape: (N, 2)."""
        if self._proc is None or not self._proc.sample_loaded():
            return audio
        if audio.ndim == 1:
            audio = np.column_stack([audio, audio])
        elif audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
        left  = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
        right = np.ascontiguousarray(audio[:, 1], dtype=np.float32)
        out_l, out_r = self._proc.process_block(left, right)
        return np.column_stack([out_l, out_r])

    # ── Serialisation override ────────────────────────────────────────────────

    def get_params(self) -> dict:
        params = super().get_params()
        params["_loaded_path"] = self._loaded_path
        return params

    def set_params(self, params: dict) -> None:
        super().set_params(params)
        # Reload sample file if saved path is still accessible.
        saved = params.get("_loaded_path", "")
        if saved and os.path.isfile(saved):
            self.load_sample(saved)
        # Sync open widget (if any) to restored values.
        if self._widget_ref is not None:
            w = self._widget_ref()
            if w is not None:
                w.sync_to_plugin()

    # ── Instrument capability query ───────────────────────────────────────────

    def is_instrument_active(self) -> bool:
        """
        Return True when the plugin is enabled, a sample is loaded, and the
        engine is ready to produce sound.

        Bypassed plugins (enabled=False) return False so FluidSynth takes over,
        matching professional DAW behaviour: deactivating an instrument lets the
        default synth play instead of producing silence.
        """
        return self.enabled and self._proc is not None and self._proc.sample_loaded()

    # ── MIDI interface ────────────────────────────────────────────────────────

    def note_on(self, midi_note: int, velocity: float = 1.0) -> None:
        if self._proc is not None:
            self._proc.note_on(int(midi_note), float(velocity))

    def note_off(self, midi_note: int) -> None:
        if self._proc is not None:
            self._proc.note_off(int(midi_note))

    # ── Sample loading ────────────────────────────────────────────────────────

    def load_sample(self, path: str) -> None:
        """Load an audio file into the C++ Sampler engine."""
        flat, sr, channels = _load_audio(path)
        if flat is None:
            return
        if self._proc is not None:
            self._proc.load_sample(flat, float(sr), int(channels))
        self._loaded_path = path
        # Build mono mix and notify the widget if it is open.
        mono = (flat[0::2] + flat[1::2]) * 0.5 if channels == 2 else flat
        if self._widget_load_hook is not None:
            try:
                self._widget_load_hook(mono, path)
            except Exception:
                pass

    # ── Voice count helper (call from QTimer tick) ────────────────────────────

    def update_voice_count(self) -> None:
        if self._widget_ref is not None:
            w = self._widget_ref()
            if w is not None:
                w.update_voice_count()
