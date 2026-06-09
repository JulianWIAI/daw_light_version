"""
sfz_realtime_player.py -- Real-time audio output for SFZ instruments.

Routes MIDI note events to a SfizzEngine and streams the rendered audio to
the default output device via sounddevice (C++ engine) or pygame.mixer
(Python fallback).  The public API mirrors VstRealTimePlayer so AudioEngine
can treat both uniformly.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def _ensure_pygame_mixer(sample_rate: int = 44100) -> bool:
    """Make sure pygame.mixer is initialised before the first Sound.play()."""
    try:
        import pygame.mixer as pgm
        if not pgm.get_init():
            pgm.init(frequency=sample_rate, size=-16, channels=2, buffer=512)
        return True
    except Exception as exc:
        logger.warning("SfzRealTimePlayer: pygame.mixer unavailable: %s", exc)
        return False


class SfzRealTimePlayer:
    """
    Wraps a SfizzEngine (C++ or Python) for real-time note-on/off and audio
    output.

    Strategy by engine type
    -----------------------
    C++ SfizzEngine:
        Opens a sounddevice OutputStream.  The callback flushes pending MIDI
        events into the engine, calls engine.render(frames), and writes the
        resulting float32 stereo buffer to the output device.

    SfizzEnginePython (fallback):
        pygame.mixer.Sound handles its own audio thread; we just call
        engine.note_on/note_off directly.  No sounddevice stream is needed.
    """

    SAMPLE_RATE: int = 44100
    BLOCK_SIZE:  int = 512

    def __init__(self, engine: object) -> None:
        self._engine = engine
        from .sfz_engine_python import SfizzEnginePython
        self._is_python: bool = isinstance(engine, SfizzEnginePython)
        self._pending: List[Tuple[str, int, int]] = []
        self._lock    = threading.Lock()
        self._stream: Optional[object] = None

    # ── Public API ─────────────────────────────────────────────────────────

    def note_on(self, pitch: int, velocity: int = 100) -> None:
        pitch    = max(0, min(127, pitch))
        velocity = max(0, min(127, velocity))
        if self._is_python:
            self._engine.note_on(0, pitch, velocity)
        else:
            with self._lock:
                self._pending.append(("on", pitch, velocity))

    def note_off(self, pitch: int) -> None:
        pitch = max(0, min(127, pitch))
        if self._is_python:
            self._engine.note_off(0, pitch, 0)
        else:
            with self._lock:
                self._pending.append(("off", pitch, 0))

    def start(self) -> bool:
        """Open audio output.  Returns True on success."""
        if self._is_python:
            return _ensure_pygame_mixer(self.SAMPLE_RATE)
        try:
            import sounddevice as sd
        except ImportError as exc:
            logger.warning(
                "SfzRealTimePlayer: sounddevice not available (%s) — "
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
            return True
        except Exception as exc:
            logger.warning("SfzRealTimePlayer: could not open stream: %s", exc)
            return False

    def stop(self) -> None:
        """Stop audio output and silence held notes."""
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

    # ── sounddevice callback (C++ engine only) ─────────────────────────────

    def _callback(self, outdata, frames: int, time_info, status) -> None:
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
            logger.debug("SfzRealTimePlayer._callback error: %s", exc)
            outdata.fill(0.0)
