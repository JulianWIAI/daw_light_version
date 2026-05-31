"""
audio_mixer_strip.py -- Mixer-strip widget for audio file tracks.
=================================================================
Mirrors the visual style of MixerStrip (MIDI channels) but routes
control changes to AudioFilePlayer / AudioFxChain instead of the
FluidSynth engine.

Key differences from MixerStrip:
    - Keyed by track_id (AudioTrack.track_id) not MIDI channel.
    - Volume fader covers 0-200 (maps to 0.0-2.0) so audio clips can
      be boosted above unity gain when needed.
    - "FX" button opens the AudioFxPanel side-panel for full EQ /
      reverb / compressor / chorus control.
    - Gold accent border visually separates audio strips from MIDI
      strips in the shared mixer panel.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

if TYPE_CHECKING:
    from .audio_fx_chain import AudioFxChain

# Minimal colour palette — mirrors gui_windows.C to avoid a circular import.
_C: dict[str, str] = {
    "abyss":    "#0a0e14",
    "deep":     "#111827",
    "cyan":     "#00e5ff",
    "pink":     "#ff2d9e",
    "gold":     "#ffd700",
    "orange":   "#ff6b2b",
    "text_dim": "#7a8fa6",
}


class AudioMixerStrip(QFrame):
    """
    Crystal-themed vertical channel strip for an audio file track.

    Signals
    -------
    volume_changed(track_id, float)   0.0 – 2.0  (unity = 1.0)
    pan_changed(track_id, float)      -1.0 – 1.0 (centre = 0.0)
    mute_toggled(track_id, bool)
    solo_toggled(track_id, bool)
    remove_clicked(track_id)
    fx_clicked(track_id)              request to open AudioFxPanel
    """

    volume_changed = Signal(int, float)
    pan_changed    = Signal(int, float)
    mute_toggled   = Signal(int, bool)
    solo_toggled   = Signal(int, bool)
    remove_clicked = Signal(int)
    fx_clicked     = Signal(int)

    def __init__(
        self,
        track_id: int,
        name:     str,
        color:    str,
        parent:   Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.track_id  = track_id
        self._building = True   # suppress outbound signals during construction
        self._color    = color

        self.setFixedWidth(96)
        self.setFrameShape(QFrame.NoFrame)
        self._apply_border(selected=False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 5, 5, 6)
        lay.setSpacing(3)

        # ── Colour bar + remove button ─────────────────────────────────────
        top = QHBoxLayout()
        bar = QFrame()
        bar.setFixedHeight(5)
        bar.setStyleSheet(f"background:{color}; border-radius:2px; border:none;")
        top.addWidget(bar, stretch=1)

        rm = QPushButton("×")
        rm.setFixedSize(16, 16)
        rm.setStyleSheet(
            f"QPushButton {{ background:rgba(255,45,158,0.18); border:none;"
            f" border-radius:8px; color:{_C['pink']}; font-size:11px; }}"
            f"QPushButton:hover {{ background:{_C['pink']}; color:white; }}"
        )
        rm.clicked.connect(lambda: self.remove_clicked.emit(track_id))
        top.addWidget(rm)
        lay.addLayout(top)

        # ── Track name label ───────────────────────────────────────────────
        self._name_lbl = QLabel(name[:10])
        self._name_lbl.setAlignment(Qt.AlignCenter)
        self._name_lbl.setFont(QFont("Arial", 8, QFont.Bold))
        self._name_lbl.setStyleSheet(f"color:{color}; background:transparent;")
        self._name_lbl.setWordWrap(True)
        lay.addWidget(self._name_lbl)

        # ── "AUDIO" type badge — visual separator from MIDI strips ─────────
        badge = QLabel("AUDIO")
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            f"color:{_C['gold']}; font-size:8px; background:transparent;"
            f" border:1px solid rgba(255,215,0,0.3); border-radius:3px;"
        )
        lay.addWidget(badge)

        # ── Volume fader (0-200 → 0.0-2.0, unity at slider value 100) ──────
        lbl_vol = QLabel("VOL")
        lbl_vol.setAlignment(Qt.AlignCenter)
        lbl_vol.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:8px; background:transparent;"
        )
        lay.addWidget(lbl_vol)

        self.vol = QSlider(Qt.Vertical)
        self.vol.setRange(0, 200)
        self.vol.setValue(100)   # 100/100 = 1.0 = unity gain
        self.vol.setFixedHeight(90)
        self.vol.valueChanged.connect(self._on_vol)
        lay.addWidget(self.vol, alignment=Qt.AlignCenter)

        # ── Pan slider (-50 to +50 → -1.0 to +1.0) ────────────────────────
        lbl_pan = QLabel("PAN")
        lbl_pan.setAlignment(Qt.AlignCenter)
        lbl_pan.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:8px; background:transparent;"
        )
        lay.addWidget(lbl_pan)

        self.pan = QSlider(Qt.Horizontal)
        self.pan.setRange(-50, 50)
        self.pan.setValue(0)
        self.pan.valueChanged.connect(self._on_pan)
        lay.addWidget(self.pan)

        # ── Mute / Solo buttons ────────────────────────────────────────────
        ms = QHBoxLayout()
        ms.setSpacing(3)

        self.mute_btn = QPushButton("M")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setFixedSize(30, 22)
        self.mute_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid rgba(255,107,43,0.3); border-radius:3px;"
            f" color:{_C['text_dim']}; font-size:10px; }}"
            f"QPushButton:checked {{ background:rgba(255,107,43,0.4);"
            f" border-color:{_C['orange']}; color:{_C['orange']}; }}"
        )
        self.mute_btn.toggled.connect(lambda v: self.mute_toggled.emit(track_id, v))
        ms.addWidget(self.mute_btn)

        self.solo_btn = QPushButton("S")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setFixedSize(30, 22)
        self.solo_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid rgba(255,215,0,0.3); border-radius:3px;"
            f" color:{_C['text_dim']}; font-size:10px; }}"
            f"QPushButton:checked {{ background:rgba(255,215,0,0.3);"
            f" border-color:{_C['gold']}; color:{_C['gold']}; }}"
        )
        self.solo_btn.toggled.connect(lambda v: self.solo_toggled.emit(track_id, v))
        ms.addWidget(self.solo_btn)
        lay.addLayout(ms)

        # ── FX button — opens the AudioFxPanel for full DSP control ────────
        self.fx_btn = QPushButton("FX")
        self.fx_btn.setFixedHeight(22)
        self.fx_btn.setToolTip("Open EQ / effects panel for this audio track")
        self.fx_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid rgba(0,229,255,0.2); border-radius:3px;"
            f" color:{_C['text_dim']}; font-size:10px; }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.12);"
            f" border-color:{_C['cyan']}; color:{_C['cyan']}; }}"
        )
        self.fx_btn.clicked.connect(lambda: self.fx_clicked.emit(track_id))
        lay.addWidget(self.fx_btn)

        lay.addStretch()
        self._building = False

    # ── Signal emitters ────────────────────────────────────────────────────

    def _on_vol(self, v: int) -> None:
        if not self._building:
            # Integer 0-200 maps to float 0.0-2.0
            self.volume_changed.emit(self.track_id, v / 100.0)

    def _on_pan(self, v: int) -> None:
        if not self._building:
            # Integer -50..+50 maps to float -1.0..+1.0
            self.pan_changed.emit(self.track_id, v / 50.0)

    # ── Public API ─────────────────────────────────────────────────────────

    def set_name(self, name: str) -> None:
        """Update the displayed track name (truncated to 10 chars)."""
        self._name_lbl.setText(name[:10])

    def sync_from_chain(self, chain: "AudioFxChain") -> None:
        """
        Populate all controls from an AudioFxChain without emitting signals.

        Call this after loading or replacing a chain so the strip reflects
        the persisted state.
        """
        self._building = True
        self.vol.setValue(int(chain.volume * 100))
        self.pan.setValue(int(chain.pan * 50))
        self.mute_btn.setChecked(chain.muted)
        self.solo_btn.setChecked(chain.soloed)
        self._building = False

    def _apply_border(self, *, selected: bool) -> None:
        """Swap border colour to indicate focus/selection."""
        border_col = (
            "rgba(255,215,0,0.70)" if selected else "rgba(255,215,0,0.18)"
        )
        self.setStyleSheet(f"""
            AudioMixerStrip {{
                background:{_C['abyss']};
                border:1px solid {border_col};
                border-radius:8px;
                margin:2px;
            }}
        """)
