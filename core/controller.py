"""
controller.py — SBS-Synth Master Controller Manager
====================================================
Bridges all input devices (QWERTY keyboard, PS5 DualSense, and Nintendo
Switch Pro Controller) to the AudioEngine and MidiLogic.

Design pattern — Mediator:
    The Controller knows about both AudioEngine and MidiLogic.  It
    translates raw hardware events (key codes, gamepad button indices) into
    musical actions (note_on, note_off, record_note_on …).  Neither
    AudioEngine nor MidiLogic know anything about hardware — they only
    receive clean musical commands.  This makes each subsystem independently
    testable and replaceable.

PS5 DualSense support:
    We use pygame's joystick module, which treats the DualSense as a
    standard HID gamepad on macOS.  Each button is mapped to a MIDI pitch
    in the current musical scale so pressing the controller feels like
    playing an instrument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .audio_engine import AudioEngine
from .midi_logic import MidiLogic

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Musical scale helpers
# ---------------------------------------------------------------------------

# Intervals (in semitones) for common scales, relative to the root note.
SCALES: Dict[str, List[int]] = {
    "major":           [0, 2, 4, 5, 7, 9, 11, 12],
    "minor":           [0, 2, 3, 5, 7, 8, 10, 12],
    "pentatonic_major":[0, 2, 4, 7, 9, 12, 14, 16],
    "pentatonic_minor":[0, 3, 5, 7, 10, 12, 15, 17],
    "blues":           [0, 3, 5, 6, 7, 10, 12, 15],
    "chromatic":       list(range(13)),
}

# QWERTY rows mapped to ascending scale degrees.
# Lower row = lower pitches, upper row = higher pitches.
QWERTY_LOWER_ROW: List[str] = list("zxcvbnm,./")
QWERTY_UPPER_ROW: List[str] = list("asdfghjkl;'")
QWERTY_TOP_ROW:   List[str] = list("qwertyuiop[]")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Controller button constants (pygame ordering on macOS)
# ---------------------------------------------------------------------------

# PS5 DualSense
PS5_CROSS:    int = 0
PS5_CIRCLE:   int = 1
PS5_SQUARE:   int = 2
PS5_TRIANGLE: int = 3
PS5_L1:       int = 4
PS5_R1:       int = 5
PS5_L2:       int = 6
PS5_R2:       int = 7
PS5_SHARE:    int = 8
PS5_OPTIONS:  int = 9
PS5_L3:       int = 10
PS5_R3:       int = 11
PS5_PS:       int = 12
PS5_TOUCH:    int = 13
# D-pad — on macOS the DualSense exposes the D-pad as four discrete buttons
# (not a hat), confirmed by get_numhats() == 0 on macOS with SDL2.
PS5_DPAD_UP:    int = 14
PS5_DPAD_DOWN:  int = 15
PS5_DPAD_LEFT:  int = 16
PS5_DPAD_RIGHT: int = 17   # present only when joystick reports 18+ buttons

PS5_NOTE_BUTTONS: List[int] = [
    PS5_CROSS, PS5_CIRCLE, PS5_SQUARE, PS5_TRIANGLE,
    PS5_L1, PS5_R1, PS5_L2, PS5_R2,
]
PS5_NOTE_LABELS: List[str] = ["X", "O", "□", "△", "L1", "R1", "L2", "R2"]

# Nintendo Switch Pro Controller (button indices are identical to PS5 on macOS)
SW_B:   int = 0
SW_A:   int = 1
SW_Y:   int = 2
SW_X:   int = 3
SW_L:   int = 4
SW_R:   int = 5
SW_ZL:  int = 6
SW_ZR:  int = 7
SW_MINUS: int = 8
SW_PLUS:  int = 9
SW_L3:    int = 10
SW_R3:    int = 11
SW_DPAD_UP:    int = 13
SW_DPAD_DOWN:  int = 14
SW_DPAD_LEFT:  int = 15
SW_DPAD_RIGHT: int = 16

SWITCH_NOTE_BUTTONS: List[int] = [SW_B, SW_A, SW_Y, SW_X, SW_L, SW_R, SW_ZL, SW_ZR]
SWITCH_NOTE_LABELS:  List[str]  = ["B", "A", "Y", "X", "L", "R", "ZL", "ZR"]

# D-pad: on macOS, both controllers expose the D-pad as four buttons (no hats).
# Hat-based polling is kept as a fallback for other OS / driver combinations.


# ---------------------------------------------------------------------------
# Key mapping data class
# ---------------------------------------------------------------------------

@dataclass
class KeyMapping:
    """
    Associates a keyboard key or gamepad button with a MIDI pitch.

    Attributes:
        key_code    : Qt key code (e.g. Qt.Key_Z) or button index.
        midi_pitch  : Absolute MIDI note number to play.
        label       : Short string shown in the UI mapping overlay.
        is_active   : True while the key is held down (sustain tracking).
    """
    key_code: int
    midi_pitch: int
    label: str = ""
    is_active: bool = field(default=False, compare=False)


# ---------------------------------------------------------------------------
# ControllerManager
# ---------------------------------------------------------------------------

class ControllerManager:
    """
    Translates raw input events into musical commands.

    Public API (called by the GUI):
        handle_key_press(qt_key)    → note on
        handle_key_release(qt_key)  → note off
        set_scale(name, root_pitch) → rebuild the pitch mapping
        set_active_channel(ch)      → choose which track receives input
        set_octave(octave)          → transpose the keyboard mapping
        start_gamepad_polling()     → begin DualSense background polling
        stop_gamepad_polling()      → stop gamepad polling
    """

    def __init__(self, audio_engine: AudioEngine, midi_logic: MidiLogic) -> None:
        """
        Args:
            audio_engine: Live audio subsystem for real-time note events.
            midi_logic:   Sequencer for recording input into tracks.
        """
        self._engine = audio_engine
        self._logic = midi_logic

        # Currently selected scale and root note.
        self._scale_name: str = "major"
        self._root_pitch: int = 60   # Middle C
        self._octave: int = 0        # +/- semitone offset in multiples of 12

        # The MIDI channel all keyboard/controller input goes to.
        self._active_channel: int = 0

        # Mapping: Qt key integer → KeyMapping
        self._key_map: Dict[int, KeyMapping] = {}
        self._gamepad_map: Dict[int, KeyMapping] = {}
        self._axis_map: Dict[int, KeyMapping] = {}   # axis index → KeyMapping (analog triggers)

        # Gamepad state — polled from the main thread via QTimer (see poll_once).
        self._joystick: Optional[object] = None   # pygame.joystick.Joystick
        self._controller_name: str = ""
        self._prev_states: Dict = {}              # button index → bool, "hat" → (x,y), "axis_N" → bool

        # Optional callback fired when the octave changes via D-pad.
        # The GUI wires this to update its octave spinner safely on the main thread.
        self.on_octave_changed: Optional[Callable[[int], None]] = None

        # Build initial mappings.
        self._build_keyboard_map()
        self._build_gamepad_map()

        logger.info("ControllerManager initialised (scale=%s, root=%d)", self._scale_name, self._root_pitch)

    # ------------------------------------------------------------------
    # Scale & octave control
    # ------------------------------------------------------------------

    def set_scale(self, name: str, root_pitch: int) -> None:
        """
        Rebuild all pitch mappings for a new musical scale.

        Why rebuild on each scale change?  Precomputed maps are O(1) at
        event time — essential for low-latency response.  The rebuild itself
        is negligible (< 1 ms) and only happens when the user changes scale.

        Args:
            name:       Key in the SCALES dict (e.g. "major", "blues").
            root_pitch: MIDI root note (e.g. 60 = C4, 62 = D4).
        """
        if name not in SCALES:
            logger.warning("Unknown scale '%s', keeping current.", name)
            return
        self._scale_name = name
        self._root_pitch = root_pitch
        self._build_keyboard_map()
        self._build_gamepad_map()
        logger.info("Scale set to %s root=%d", name, root_pitch)

    def set_octave(self, octave: int) -> None:
        """
        Shift the keyboard mapping up or down by octaves.

        Args:
            octave: Signed integer; 0 = no shift, +1 = one octave up.
        """
        self._octave = max(-3, min(3, octave))
        self._build_keyboard_map()
        self._build_gamepad_map()
        if callable(self.on_octave_changed):
            self.on_octave_changed(self._octave)

    @property
    def controller_name(self) -> str:
        """Name of the currently connected gamepad, or empty string."""
        return self._controller_name

    def set_active_channel(self, channel: int) -> None:
        """Route keyboard/gamepad input to a specific MIDI channel."""
        self._active_channel = channel

    # ------------------------------------------------------------------
    # Keyboard input handling (called by the GUI event loop)
    # ------------------------------------------------------------------

    def handle_key_press(self, qt_key: int) -> None:
        """
        Process a Qt key-press event.

        We look up the key in the pre-built map.  If found, we fire a
        note-on immediately (for low latency) AND notify MidiLogic if
        recording is active (so the note gets stored in the sequencer).

        Args:
            qt_key: Qt.Key enum value from QKeyEvent.key().
        """
        mapping = self._key_map.get(qt_key)
        if mapping is None or mapping.is_active:
            return  # Unknown key or auto-repeat — ignore.

        mapping.is_active = True
        pitch = self._transpose(mapping.midi_pitch)

        # Fire audio immediately for real-time feel.
        self._engine.note_on(self._active_channel, pitch)

        # Record into the sequencer if armed.
        self._logic.record_note_on(pitch, AudioEngine.DEFAULT_VELOCITY)

    def handle_key_release(self, qt_key: int) -> None:
        """
        Process a Qt key-release event.

        Args:
            qt_key: Qt.Key enum value from QKeyEvent.key().
        """
        mapping = self._key_map.get(qt_key)
        if mapping is None or not mapping.is_active:
            return

        mapping.is_active = False
        pitch = self._transpose(mapping.midi_pitch)
        self._engine.note_off(self._active_channel, pitch)
        self._logic.record_note_off(pitch)

    def reset_key_states(self) -> None:
        """Send note-off for every active key and clear all is_active flags.

        Call this when keyboard focus switches (e.g. before and after the VST
        native editor opens) so no keys get stuck on or permanently silenced.
        """
        for mapping in self._key_map.values():
            if mapping.is_active:
                mapping.is_active = False
                pitch = self._transpose(mapping.midi_pitch)
                self._engine.note_off(self._active_channel, pitch)

    def get_key_map(self) -> Dict[int, KeyMapping]:
        """Return a copy of the current keyboard→pitch mapping for UI display."""
        return dict(self._key_map)

    # ------------------------------------------------------------------
    # Gamepad input
    # ------------------------------------------------------------------

    def start_gamepad_polling(self) -> bool:
        """
        Initialise pygame and the joystick.

        On macOS, pygame.event.pump() must run on the main thread (AppKit
        restriction).  Therefore we do NOT start a background thread here.
        The caller must drive polling via poll_once() from a QTimer.

        Returns:
            True if a joystick was found and initialised.
        """
        try:
            import pygame
            pygame.init()
            pygame.joystick.init()

            if pygame.joystick.get_count() == 0:
                logger.warning("No gamepad detected.")
                return False

            self._joystick = pygame.joystick.Joystick(0)
            self._joystick.init()
            self._controller_name = self._joystick.get_name()
            self._prev_states.clear()
            logger.info(
                "Gamepad connected: %s  (%d buttons, %d axes, %d hats)",
                self._controller_name,
                self._joystick.get_numbuttons(),
                self._joystick.get_numaxes(),
                self._joystick.get_numhats(),
            )
            self._build_gamepad_map()
            return True
        except ImportError:
            logger.warning("pygame not installed — gamepad support disabled.")
            return False
        except Exception as exc:
            logger.error("Gamepad init error: %s", exc)
            return False

    def stop_gamepad_polling(self) -> None:
        """Release pygame joystick resources."""
        self._prev_states.clear()
        try:
            import pygame
            pygame.joystick.quit()
        except Exception:
            pass

    def poll_once(self) -> None:
        """
        Read one frame of joystick state.  Must be called from the main thread
        (macOS AppKit requires pygame.event.pump() to run there).
        Driven by a QTimer in the GUI at ~60 Hz.
        """
        if self._joystick is None:
            return
        try:
            import pygame
            pygame.event.pump()
        except Exception:
            return

        # --- Digital buttons ---
        for btn_idx, mapping in self._gamepad_map.items():
            try:
                pressed: bool = bool(self._joystick.get_button(btn_idx))
            except Exception:
                continue

            was_pressed: bool = self._prev_states.get(btn_idx, False)

            if pressed and not was_pressed:
                pitch = self._transpose(mapping.midi_pitch)
                mapping.is_active = True
                self._engine.note_on(self._active_channel, pitch)
                self._logic.record_note_on(pitch, AudioEngine.DEFAULT_VELOCITY)
            elif not pressed and was_pressed:
                pitch = self._transpose(mapping.midi_pitch)
                mapping.is_active = False
                self._engine.note_off(self._active_channel, pitch)
                self._logic.record_note_off(pitch)

            self._prev_states[btn_idx] = pressed

        # --- Analog triggers (L2/R2 on PS5) — axis value > 0 means pressed ---
        # DualSense axes rest at -1.0 and travel to +1.0; threshold at 0.0
        for axis_idx, mapping in self._axis_map.items():
            try:
                value: float = self._joystick.get_axis(axis_idx)
            except Exception:
                continue

            pressed = value > 0.0
            state_key = f"axis_{axis_idx}"
            was_pressed = self._prev_states.get(state_key, False)

            if pressed and not was_pressed:
                pitch = self._transpose(mapping.midi_pitch)
                mapping.is_active = True
                self._engine.note_on(self._active_channel, pitch)
                self._logic.record_note_on(pitch, AudioEngine.DEFAULT_VELOCITY)
            elif not pressed and was_pressed:
                pitch = self._transpose(mapping.midi_pitch)
                mapping.is_active = False
                self._engine.note_off(self._active_channel, pitch)
                self._logic.record_note_off(pitch)

            self._prev_states[state_key] = pressed

        # --- D-pad → octave shift (Up = +1, Down = -1) ---
        # Primary: button-based D-pad (macOS DualSense has 0 hats; D-pad = buttons 13-16).
        ctype = self._detect_controller_type()
        dpad_up_btn   = PS5_DPAD_UP   if ctype != "switch" else SW_DPAD_UP
        dpad_down_btn = PS5_DPAD_DOWN if ctype != "switch" else SW_DPAD_DOWN
        n_buttons = 0
        try:
            n_buttons = self._joystick.get_numbuttons()
        except Exception:
            pass

        for btn_idx, direction in [(dpad_up_btn, +1), (dpad_down_btn, -1)]:
            if btn_idx >= n_buttons:
                continue
            try:
                pressed = bool(self._joystick.get_button(btn_idx))
            except Exception:
                continue
            state_key = f"dpad_{btn_idx}"
            was = self._prev_states.get(state_key, False)
            if pressed and not was:
                self.set_octave(self._octave + direction)
            self._prev_states[state_key] = pressed

        # Fallback: hat-based D-pad (Linux / Windows / other drivers).
        try:
            if self._joystick.get_numhats() > 0:
                hat: Tuple[int, int] = self._joystick.get_hat(0)
                prev_hat: Tuple[int, int] = self._prev_states.get("hat", (0, 0))
                if hat != prev_hat:
                    if hat[1] == 1 and prev_hat[1] != 1:
                        self.set_octave(self._octave + 1)
                    elif hat[1] == -1 and prev_hat[1] != -1:
                        self.set_octave(self._octave - 1)
                self._prev_states["hat"] = hat
        except Exception:
            pass

        # --- Debug: log any unrecognised button press to help calibrate ---
        if logger.isEnabledFor(logging.DEBUG):
            try:
                for i in range(self._joystick.get_numbuttons()):
                    if i not in self._gamepad_map and self._joystick.get_button(i):
                        if not self._prev_states.get(f"dbg_{i}", False):
                            logger.debug("Unrecognised button pressed: idx=%d", i)
                    self._prev_states[f"dbg_{i}"] = bool(
                        self._joystick.get_button(i))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Private map builders
    # ------------------------------------------------------------------

    def _build_keyboard_map(self) -> None:
        """
        Assign MIDI pitches to QWERTY keys based on the current scale.

        Layout logic:
            Bottom row (Z…/) → scale degrees 0–9 starting at the root.
            Middle row (A…') → scale degrees 0–9 one octave higher.
            Top row   (Q…]) → scale degrees 0–9 two octaves higher.

        Why pre-build?  Lookup at key-press time is O(1) regardless of
        how complex the scale is — critical for consistent, low latency.
        """
        from PySide6.QtCore import Qt

        intervals = SCALES.get(self._scale_name, SCALES["major"])
        offset = self._octave * 12

        def pitch_for_degree(degree: int) -> int:
            """Wrap degree into the intervals list; add octave as needed."""
            octave_bonus = (degree // len(intervals)) * 12
            return self._root_pitch + offset + intervals[degree % len(intervals)] + octave_bonus

        self._key_map.clear()

        # Map string characters to Qt key codes.
        char_to_qt: Dict[str, int] = {
            'z': Qt.Key_Z, 'x': Qt.Key_X, 'c': Qt.Key_C, 'v': Qt.Key_V,
            'b': Qt.Key_B, 'n': Qt.Key_N, 'm': Qt.Key_M, ',': Qt.Key_Comma,
            '.': Qt.Key_Period, '/': Qt.Key_Slash,
            'a': Qt.Key_A, 's': Qt.Key_S, 'd': Qt.Key_D, 'f': Qt.Key_F,
            'g': Qt.Key_G, 'h': Qt.Key_H, 'j': Qt.Key_J, 'k': Qt.Key_K,
            'l': Qt.Key_L, ';': Qt.Key_Semicolon, "'": Qt.Key_Apostrophe,
            'q': Qt.Key_Q, 'w': Qt.Key_W, 'e': Qt.Key_E, 'r': Qt.Key_R,
            't': Qt.Key_T, 'y': Qt.Key_Y, 'u': Qt.Key_U, 'i': Qt.Key_I,
            'o': Qt.Key_O, 'p': Qt.Key_P, '[': Qt.Key_BracketLeft,
            ']': Qt.Key_BracketRight,
        }

        rows = [
            (QWERTY_LOWER_ROW, 0),
            (QWERTY_UPPER_ROW, len(intervals)),
            (QWERTY_TOP_ROW,   len(intervals) * 2),
        ]

        for row_keys, degree_offset in rows:
            for i, ch in enumerate(row_keys):
                qt_key = char_to_qt.get(ch)
                if qt_key is None:
                    continue
                degree = degree_offset + i
                pitch = pitch_for_degree(degree)
                if 0 <= pitch <= 127:
                    self._key_map[qt_key] = KeyMapping(
                        key_code=qt_key,
                        midi_pitch=pitch,
                        label=ch.upper(),
                    )

    def _detect_controller_type(self) -> str:
        """Return 'ps5', 'switch', or 'generic' based on joystick name."""
        name = self._controller_name.lower()
        if any(k in name for k in ("dualsense", "dualshock", "playstation", "ps5", "ps4")):
            return "ps5"
        if any(k in name for k in ("pro controller", "nintendo", "switch")):
            return "switch"
        return "generic"

    def _build_gamepad_map(self) -> None:
        """Map the 8 note-buttons/triggers to scale degrees 0–7.

        PS5  DualSense:       Cross(0), Circle(1), Square(2), Triangle(3),
                              L1(4), R1(5) as digital buttons;
                              L2(axis 4), R2(axis 5) as analog triggers.
        Switch Pro Controller: B, A, Y, X, L, R, ZL, ZR — all digital buttons.

        D-pad Up/Down shift the octave (polled as a hat, not buttons).
        """
        intervals = SCALES.get(self._scale_name, SCALES["major"])
        offset = self._octave * 12

        ctype = self._detect_controller_type()
        self._gamepad_map.clear()
        self._axis_map.clear()

        if ctype == "switch":
            # All 8 buttons are digital
            for i, btn_idx in enumerate(SWITCH_NOTE_BUTTONS):
                degree = i % len(intervals)
                pitch = self._root_pitch + offset + intervals[degree]
                if 0 <= pitch <= 127:
                    self._gamepad_map[btn_idx] = KeyMapping(
                        key_code=btn_idx, midi_pitch=pitch,
                        label=SWITCH_NOTE_LABELS[i],
                    )
        else:
            # PS5 / generic: face buttons + L1/R1 are digital (buttons 0-5);
            # L2 and R2 are analog triggers exposed as axes 4 and 5.
            digital_buttons = [PS5_CROSS, PS5_CIRCLE, PS5_SQUARE, PS5_TRIANGLE,
                               PS5_L1, PS5_R1]
            digital_labels  = ["X", "O", "□", "△", "L1", "R1"]
            for i, btn_idx in enumerate(digital_buttons):
                degree = i % len(intervals)
                pitch = self._root_pitch + offset + intervals[degree]
                if 0 <= pitch <= 127:
                    self._gamepad_map[btn_idx] = KeyMapping(
                        key_code=btn_idx, midi_pitch=pitch,
                        label=digital_labels[i],
                    )
            # L2 = axis 4, R2 = axis 5 — degrees 6 and 7
            for axis_idx, label, degree in [(4, "L2", 6), (5, "R2", 7)]:
                d = degree % len(intervals)
                pitch = self._root_pitch + offset + intervals[d]
                if 0 <= pitch <= 127:
                    self._axis_map[axis_idx] = KeyMapping(
                        key_code=axis_idx, midi_pitch=pitch, label=label,
                    )

    def _transpose(self, pitch: int) -> int:
        """Clamp a MIDI pitch to the valid 0–127 range after any offset."""
        return max(0, min(127, pitch))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def scale_name(self) -> str:
        """Name of the currently active scale."""
        return self._scale_name

    @property
    def root_pitch(self) -> int:
        """MIDI root note of the current scale."""
        return self._root_pitch

    @property
    def active_channel(self) -> int:
        """MIDI channel currently receiving keyboard/gamepad input."""
        return self._active_channel