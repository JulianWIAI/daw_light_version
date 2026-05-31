"""
instrument_preview.py  --  Live Instrument Audition Widget
===========================================================
Provides a clickable two-octave piano keyboard so the user can hear any
SF2 preset BEFORE committing to it in the Add Track / Change Instrument
dialogs.

Architecture
------------
InstrumentPreviewWidget (Python / PySide6 GUI):
    - Renders white and black keys with active-note highlighting.
    - Mouse press/release fires note_on / note_off callbacks.
    - Computer keyboard shortcuts (A–K for C4–C5, same layout as FL Studio)
      also fire notes; the parent dialog forwards its keyPress/Release events
      here so the shortcuts work even when a list widget has focus.

The note callbacks are plain Python callables (no Qt signals) so the widget
stays fully decoupled from AudioEngine.  Parent dialogs wire them like:

    preview.set_note_callbacks(
        on_note_on  = engine.preview_note_on,
        on_note_off = engine.preview_note_off,
    )

All audio processing happens inside AudioEngine (FluidSynth / C level) —
this module only handles geometry and input events.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

from PySide6.QtCore  import Qt, QRect
from PySide6.QtGui   import QColor, QPainter, QPen, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel


# ── Piano layout constants ─────────────────────────────────────────────────

# Lowest MIDI note shown (C4 = middle C).
_BASE_NOTE = 60

# Number of octaves in the keyboard.
_OCTAVES = 2

# 7 white keys per octave (C D E F G A B).
_WHITE_PER_OCT = 7
_TOTAL_WHITE   = _WHITE_PER_OCT * _OCTAVES   # 14

# Semitone offset within an octave for each white-key index 0-6.
_WHITE_SEMI = [0, 2, 4, 5, 7, 9, 11]        # C D E F G A B

# For each black key: (semitone offset from root, white-key index to its LEFT).
# Pattern per octave:  C# between C(0)&D(1), D# between D(1)&E(2),
#                      F# between F(3)&G(4), G# between G(4)&A(5),
#                      A# between A(5)&B(6).
_BLACK_KEYS: List[Tuple[int, int]] = [
    (1,  0),   # C#
    (3,  1),   # D#
    (6,  3),   # F#
    (8,  4),   # G#
    (10, 5),   # A#
]

# Computer-keyboard → semitone offset from _BASE_NOTE.
# Same layout as FL Studio / most DAW on-screen keyboards.
_KEY_MAP: Dict[int, int] = {
    Qt.Key_A:  0,    # C4
    Qt.Key_W:  1,    # C#4
    Qt.Key_S:  2,    # D4
    Qt.Key_E:  3,    # D#4
    Qt.Key_D:  4,    # E4
    Qt.Key_F:  5,    # F4
    Qt.Key_T:  6,    # F#4
    Qt.Key_G:  7,    # G4
    Qt.Key_Y:  8,    # G#4
    Qt.Key_H:  9,    # A4
    Qt.Key_U: 10,    # A#4
    Qt.Key_J: 11,    # B4
    Qt.Key_K: 12,    # C5
    Qt.Key_O: 13,    # C#5
    Qt.Key_L: 14,    # D5
    Qt.Key_P: 15,    # D#5
}

# Visual colours matching the DAW's crystal theme.
_COL_WHITE_IDLE   = QColor(220, 220, 220)
_COL_WHITE_ACTIVE = QColor(0, 229, 255)        # cyan
_COL_BLACK_IDLE   = QColor(28, 28, 28)
_COL_BLACK_ACTIVE = QColor(0, 160, 185)        # darker cyan
_COL_BORDER       = QColor(60, 60, 60)
_COL_BG           = QColor(18, 18, 28)


class InstrumentPreviewWidget(QWidget):
    """
    A clickable two-octave mini piano keyboard for live instrument auditioning.

    Typical usage inside a dialog::

        self._preview = InstrumentPreviewWidget(self)
        self._preview.set_note_callbacks(
            on_note_on  = self._engine.preview_note_on,
            on_note_off = self._engine.preview_note_off,
        )
        layout.addWidget(self._preview)

    To support computer-keyboard shortcuts, forward the dialog's key events::

        def keyPressEvent(self, ev):
            if not isinstance(self.focusWidget(), QLineEdit):
                self._preview.keyPressEvent(ev)
            else:
                super().keyPressEvent(ev)

    Call silence_all() in the dialog's done() / closeEvent handler.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._on_note_on:  Optional[Callable[[int, int], None]] = None
        self._on_note_off: Optional[Callable[[int], None]]      = None

        # Currently sounding MIDI pitches (held by mouse or keyboard).
        self._active: Set[int] = set()

        # The pitch held by the mouse button (for legato slide support).
        self._mouse_pitch: Optional[int] = None

        # Accept keyboard focus when the user clicks the widget.
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMinimumSize(300, 72)
        self.setFixedHeight(72)
        self.setToolTip(
            "Click keys to audition  ·  "
            "A-K = C4-C5, W/E/T/Y/U/O/P = sharps"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def set_note_callbacks(
        self,
        on_note_on:  Callable[[int, int], None],
        on_note_off: Callable[[int], None],
    ) -> None:
        """
        Wire the audio-engine callbacks.

        on_note_on(pitch, velocity) is called on key press.
        on_note_off(pitch)         is called on key release.
        """
        self._on_note_on  = on_note_on
        self._on_note_off = on_note_off

    def silence_all(self) -> None:
        """
        Immediately stop all active notes.

        Must be called when the parent dialog closes so no phantom notes
        keep sounding through the FluidSynth preview channel.
        """
        for pitch in list(self._active):
            self._fire_off(pitch)
        self._active.clear()
        self._mouse_pitch = None

    # ── Internal note firing ──────────────────────────────────────────────────

    def _fire_on(self, pitch: int, velocity: int = 100) -> None:
        """Send note-on; ignored if the note is already active."""
        if pitch in self._active:
            return
        self._active.add(pitch)
        if self._on_note_on:
            self._on_note_on(pitch, velocity)
        self.update()

    def _fire_off(self, pitch: int) -> None:
        """Send note-off; ignored if the note was not active."""
        if pitch not in self._active:
            return
        self._active.discard(pitch)
        if self._on_note_off:
            self._on_note_off(pitch)
        self.update()

    # ── Geometry helpers ──────────────────────────────────────────────────────

    def _white_key_rects(self) -> List[Tuple[QRect, int]]:
        """Return [(QRect, midi_note), ...] for every white key."""
        w = self.width()
        h = self.height()
        kw = w / _TOTAL_WHITE
        result = []
        for i in range(_TOTAL_WHITE):
            x   = int(round(i * kw))
            w_i = int(round((i + 1) * kw)) - x
            oct_  = i // _WHITE_PER_OCT
            step  = i %  _WHITE_PER_OCT
            note  = _BASE_NOTE + oct_ * 12 + _WHITE_SEMI[step]
            result.append((QRect(x, 0, w_i, h), note))
        return result

    def _black_key_rects(self) -> List[Tuple[QRect, int]]:
        """Return [(QRect, midi_note), ...] for every black key."""
        w  = self.width()
        h  = self.height()
        kw = w / _TOTAL_WHITE
        bw = max(6, int(kw * 0.55))
        bh = int(h * 0.60)
        result = []
        for oct_ in range(_OCTAVES):
            white_base = oct_ * _WHITE_PER_OCT
            note_base  = _BASE_NOTE + oct_ * 12
            for semi, left_white in _BLACK_KEYS:
                # Centre the black key over the boundary between two white keys.
                cx = int(round((white_base + left_white + 1) * kw))
                x  = cx - bw // 2
                result.append((QRect(x, 0, bw, bh), note_base + semi))
        return result

    def _pitch_at(self, x: int, y: int) -> Optional[int]:
        """
        Hit-test pixel (x, y) and return the MIDI note, or None.

        Black keys are tested first because they visually overlap white keys.
        """
        for rect, note in self._black_key_rects():
            if rect.contains(x, y):
                return note
        for rect, note in self._white_key_rects():
            if rect.contains(x, y):
                return note
        return None

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, False)
            p.fillRect(self.rect(), _COL_BG)

            # Draw white keys first.
            for rect, note in self._white_key_rects():
                color = _COL_WHITE_ACTIVE if note in self._active else _COL_WHITE_IDLE
                p.fillRect(rect.adjusted(1, 1, -1, -1), color)
                p.setPen(QPen(_COL_BORDER, 1))
                p.drawRect(rect.adjusted(0, 0, -1, -1))

            # Draw black keys on top.
            for rect, note in self._black_key_rects():
                color = _COL_BLACK_ACTIVE if note in self._active else _COL_BLACK_IDLE
                p.fillRect(rect, color)
                p.setPen(QPen(_COL_BORDER, 1))
                p.drawRect(rect)
        finally:
            p.end()

    # ── Mouse input ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            pitch = self._pitch_at(event.pos().x(), event.pos().y())
            if pitch is not None:
                self._mouse_pitch = pitch
                self._fire_on(pitch)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Slide to adjacent notes for a legato audition feel."""
        if event.buttons() & Qt.LeftButton:
            pitch = self._pitch_at(event.pos().x(), event.pos().y())
            if pitch is not None and pitch != self._mouse_pitch:
                if self._mouse_pitch is not None:
                    self._fire_off(self._mouse_pitch)
                self._mouse_pitch = pitch
                self._fire_on(pitch)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._mouse_pitch is not None:
            self._fire_off(self._mouse_pitch)
            self._mouse_pitch = None
        super().mouseReleaseEvent(event)

    # ── Keyboard input ────────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """
        A-K / W-P trigger notes (FL Studio keyboard layout).

        Auto-repeat events are ignored so holding a key does not spam
        note-on messages.
        """
        if event.isAutoRepeat():
            return
        semi = _KEY_MAP.get(event.key())
        if semi is not None:
            self._fire_on(_BASE_NOTE + semi)
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return
        semi = _KEY_MAP.get(event.key())
        if semi is not None:
            self._fire_off(_BASE_NOTE + semi)
        else:
            super().keyReleaseEvent(event)
