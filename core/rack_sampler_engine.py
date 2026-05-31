"""
rack_sampler_engine.py -- Per-Row Audio Sample Engine for the Channel Rack
==========================================================================
Each channel rack row can have one audio sample assigned to it.
When a step fires (live or from a timeline clip), this engine plays
the sample at the pitch requested by the MIDI note.

Architecture (per project rules):
    Logic:  PythonSampler (pure-Python fallback — C++ daw_processors.Sampler
            is preferred but the .pyd DLL is currently unavailable).
            Replace PythonSampler with daw_processors.Sampler once the
            extension is rebuilt for the target Python version.
    Output: One shared sounddevice OutputStream that mixes all row voices
            so kick + snare + hi-hat can sound simultaneously.
    GUI:    None — pure audio/logic module.  All Qt code lives in channel_rack.py.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Audio constants — match the rest of the DAW.
SAMPLE_RATE = 44100
BLOCK_SIZE  = 256


class RackSamplerEngine:
    """
    Manages one PythonSampler per channel rack row.

    All active samplers are mixed into a single stereo sounddevice output
    stream.  This class is created once by MainWindow and kept alive for
    the lifetime of the application.

    Public API:
        load_sample(row_id, path)          -- load an audio file for a row
        has_sample(row_id)  -> bool        -- check if a row has audio loaded
        set_root_note(row_id, note)        -- update pitch-shift root for a row
        note_on(row_id, midi_note, vel)    -- trigger playback
        note_off(row_id, midi_note)        -- release (start ADSR release phase)
        stop()                             -- close the audio stream
    """

    def __init__(self) -> None:
        # Maps row_id (int ≥ 32) to a PythonSampler instance.
        self._samplers: Dict[int, object] = {}
        self._lock     = threading.Lock()
        self._stream   = None
        self._start_stream()

    # ── Audio stream ───────────────────────────────────────────────────────────

    def _start_stream(self) -> None:
        """Open the shared sounddevice output stream.  No-op if unavailable."""
        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(
                samplerate = SAMPLE_RATE,
                channels   = 2,
                dtype      = "float32",
                blocksize  = BLOCK_SIZE,
                callback   = self._audio_callback,
            )
            self._stream.start()
            logger.info("RackSamplerEngine: audio stream started at %d Hz.", SAMPLE_RATE)
        except Exception as exc:
            logger.warning("RackSamplerEngine: sounddevice unavailable — %s.", exc)
            self._stream = None

    def _audio_callback(
        self,
        outdata: np.ndarray,
        frames:  int,
        time,
        status,
    ) -> None:
        """Mix all active sampler voices into the output buffer (RT thread)."""
        mixed_l = np.zeros(frames, dtype=np.float32)
        mixed_r = np.zeros(frames, dtype=np.float32)

        with self._lock:
            for sampler in self._samplers.values():
                # process_block() adds voices on top of the provided buffer.
                l, r = sampler.process_block(
                    np.zeros(frames, dtype=np.float32),
                    np.zeros(frames, dtype=np.float32),
                )
                mixed_l += l
                mixed_r += r

        # Soft clip to prevent output distortion when many rows play together.
        np.clip(mixed_l, -1.0, 1.0, out=mixed_l)
        np.clip(mixed_r, -1.0, 1.0, out=mixed_r)
        outdata[:, 0] = mixed_l
        outdata[:, 1] = mixed_r

    # ── Public API ─────────────────────────────────────────────────────────────

    def load_sample(self, row_id: int, path: str) -> bool:
        """
        Load an audio file into the sampler for the given rack row.

        Returns True if the file was loaded successfully, False otherwise.
        Supported formats: WAV, FLAC, OGG, AIFF, MP3 (via soundfile or scipy).
        """
        from .fx_plugins_sampler import _load_audio
        flat, sr, channels = _load_audio(path)
        if flat is None:
            logger.warning(
                "RackSamplerEngine.load_sample: cannot read '%s'.", path)
            return False

        from .sampler_python import PythonSampler
        with self._lock:
            if row_id not in self._samplers:
                self._samplers[row_id] = PythonSampler(float(SAMPLE_RATE))
            self._samplers[row_id].load_sample(flat, float(sr), int(channels))

        logger.info(
            "RackSamplerEngine: row %d loaded '%s' (%d ch, %d Hz).",
            row_id, path, channels, int(sr))
        return True

    def has_sample(self, row_id: int) -> bool:
        """Return True if this row has an audio sample loaded and ready."""
        s = self._samplers.get(row_id)
        return s is not None and s.sample_loaded()

    def set_root_note(self, row_id: int, note: int) -> None:
        """
        Update the root MIDI note for pitch-shifting.

        When note_on is called with a pitch equal to root_note the sample
        plays at its original speed.  Other pitches are sped up or slowed
        down by a factor of 2^((pitch - root_note) / 12).
        """
        s = self._samplers.get(row_id)
        if s is not None:
            s.set_root_note(int(note))

    def note_on(self, row_id: int, midi_note: int, velocity: int) -> None:
        """Trigger sample playback for the given row at the given pitch."""
        s = self._samplers.get(row_id)
        if s is not None and s.sample_loaded():
            s.note_on(int(midi_note), velocity / 127.0)

    def note_off(self, row_id: int, midi_note: int) -> None:
        """Start the release phase of the voice for the given row and pitch."""
        s = self._samplers.get(row_id)
        if s is not None:
            s.note_off(int(midi_note))

    def stop(self) -> None:
        """Stop the audio stream and release the sounddevice resource."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
