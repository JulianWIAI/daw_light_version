"""
dspreset_realtime_player.py -- Real-time audio output for Decent Sampler instruments.
======================================================================================
Routes MIDI note events to a DecentSamplerEngine and streams the rendered audio
to the default output device via sounddevice (C++ engine) or relies on pygame.mixer
(Python fallback, which handles its own audio thread).

The public API mirrors SfzRealTimePlayer so AudioEngine can treat both uniformly.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class DsRealTimePlayer:
    """
    Wraps a DecentSamplerEngine (C++ or Python fallback) for real-time MIDI
    input and audio output.

    Strategy by engine type
    -----------------------
    C++ DecentSamplerEngine:
        Opens a sounddevice.OutputStream.  The audio callback flushes all
        pending MIDI events into the engine, calls engine.render(frames),
        and writes the resulting float32 stereo buffers to the output device.

    DsEnginePython (fallback):
        pygame.mixer.Sound handles its own audio thread; we just call
        engine.note_on/note_off directly.  No sounddevice stream needed.
    """

    SAMPLE_RATE: int = 44100
    BLOCK_SIZE:  int = 512

    def __init__(self, engine: object) -> None:
        self._engine = engine

        # Detect fallback vs C++ engine by checking for render() returning audio.
        from .dspreset_engine import DsEnginePython
        self._is_python: bool = isinstance(engine, DsEnginePython)

        # Thread-safe queue of (kind, note, velocity) pending MIDI events.
        self._pending: List[Tuple[str, int, int]] = []
        self._lock = threading.Lock()
        self._stream: Optional[object] = None

    # ── Public MIDI API ────────────────────────────────────────────────────────

    def note_on(self, pitch: int, velocity: int = 100) -> None:
        """Trigger a note-on event (MIDI note 0-127, velocity 0-127)."""
        pitch    = max(0, min(127, pitch))
        velocity = max(0, min(127, velocity))
        if self._is_python:
            # Python fallback: call directly; pygame handles its own thread.
            self._engine.note_on(0, pitch, velocity)
        else:
            with self._lock:
                self._pending.append(("on", pitch, velocity))

    def note_off(self, pitch: int) -> None:
        """Release a held note."""
        pitch = max(0, min(127, pitch))
        if self._is_python:
            self._engine.note_off(0, pitch, 0)
        else:
            with self._lock:
                self._pending.append(("off", pitch, 0))

    # ── Stream lifecycle ───────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Open the audio output stream.  Returns True on success.

        For the Python fallback this simply ensures pygame.mixer is running.
        For the C++ engine this opens a sounddevice.OutputStream.
        """
        if self._is_python:
            return self._ensure_pygame()

        try:
            import sounddevice as sd
        except ImportError as exc:
            logger.warning(
                "DsRealTimePlayer: sounddevice not available (%s) — "
                "install with: pip install sounddevice", exc)
            return False

        try:
            self._stream = sd.OutputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=self.BLOCK_SIZE,
                channels=2,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            logger.debug("DsRealTimePlayer: sounddevice stream started "
                         "(%d Hz, %d frames)", self.SAMPLE_RATE, self.BLOCK_SIZE)
            return True
        except Exception as exc:
            logger.warning("DsRealTimePlayer: could not open stream: %s", exc)
            return False

    def stop(self) -> None:
        """Stop the audio stream and silence all held notes."""
        try:
            self._engine.all_notes_off(0)
        except Exception:
            pass

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # ── sounddevice audio callback (C++ engine only) ───────────────────────────

    def _callback(self, outdata, frames: int, time_info, status) -> None:
        """
        Audio thread callback.  Runs at audio-thread priority — no GIL held
        except during brief numpy operations.

        1. Drain the pending MIDI queue.
        2. Forward events to the C++ engine with sample-accurate delay = 0.
        3. Render `frames` stereo samples.
        4. Copy into outdata (shape: (frames, 2), dtype float32).
        """
        # Drain pending MIDI events.
        with self._lock:
            events = list(self._pending)
            self._pending.clear()

        try:
            import numpy as np

            for kind, note, vel in events:
                if kind == "on":
                    self._engine.note_on(0, note, vel)
                else:
                    self._engine.note_off(0, note, 0)

            left, right = self._engine.render(frames)
            left  = np.asarray(left,  dtype=np.float32)
            right = np.asarray(right, dtype=np.float32)
            n = min(len(left), len(right), frames)
            outdata[:n, 0] = left[:n]
            outdata[:n, 1] = right[:n]
            if n < frames:
                outdata[n:] = 0.0

        except Exception as exc:
            logger.debug("DsRealTimePlayer._callback error: %s", exc)
            outdata.fill(0.0)

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _ensure_pygame(sample_rate: int = SAMPLE_RATE) -> bool:
        """Initialise pygame.mixer if it is not already running."""
        try:
            import pygame.mixer as pgm
            if not pgm.get_init():
                pgm.init(frequency=sample_rate, size=-16, channels=2, buffer=512)
            return True
        except Exception as exc:
            logger.warning("DsRealTimePlayer: pygame.mixer unavailable: %s", exc)
            return False
