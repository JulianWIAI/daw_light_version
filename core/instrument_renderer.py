"""
instrument_renderer.py -- Real-Time Audio Renderer for MIDI Instrument Tracks
==============================================================================
Provides InstrumentRenderer, which wraps a sounddevice.OutputStream to render
an AudioFxChain (containing e.g. SamplerPlugin) in real time.

Architecture
------------
    InstrumentRenderer owns one sounddevice output stream per MIDI track that
    has been converted to an instrument track (i.e. the user has loaded a
    SamplerPlugin or similar instrument plugin into its FX rack).

    Audio callback path (runs in sounddevice's RT thread):
        zeros input buffer  →  AudioFxChain.process()  →  apply gain/pan  →  output

    MIDI routing (called from MainWindow._note_event_callback, GUI thread):
        note_on(pitch, velocity)  →  each instrument plugin in chain
        note_off(pitch)           →  each instrument plugin in chain

    The chain snapshot used in the audio callback is taken at the START of each
    block call, not once at construction, so parameter changes made from the GUI
    thread are picked up on the next block boundary (GIL makes list() atomic).

Fallback
--------
    If sounddevice is unavailable, InstrumentRenderer silently does nothing.
    The FX panel and note routing still work; audio simply does not play until
    sounddevice is installed ('pip install sounddevice').

Thread safety
-------------
    set_chain()  -- called from GUI thread, just replaces a reference (atomic).
    note_on/off  -- called from the MidiLogic playback thread; they call into
                    the C++ Sampler which uses its own internal voice state
                    (no Python GIL release needed for pybind11 calls).
    _audio_callback -- called from the sounddevice RT thread; takes a list()
                    snapshot of chain.plugins before processing.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

from .audio_fx_chain import AudioFxChain

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100       # frames per second sent to the audio device
BLOCK_SIZE  = 256         # ~5.8 ms per block — low enough for responsive MIDI


# ---------------------------------------------------------------------------
# InstrumentRenderer
# ---------------------------------------------------------------------------

class InstrumentRenderer:
    """
    Real-time stereo audio renderer for a single MIDI instrument track.

    One instance is created per MIDI track that has instrument plugins loaded
    into its FX rack.  MainWindow owns these instances in a dict keyed by
    MIDI channel number.

    Usage::

        renderer = InstrumentRenderer(sample_rate=44100)
        renderer.set_chain(my_fx_chain)
        renderer.start()

        # from MIDI callback:
        renderer.note_on(60, 0.8)
        renderer.note_off(60)

        # when track is deleted:
        renderer.stop()
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE,
                 block_size: int = BLOCK_SIZE) -> None:
        self._sample_rate: int = sample_rate
        self._block_size:  int = block_size

        # AudioFxChain for this track.  Replaced atomically from the GUI thread.
        self._chain: Optional[AudioFxChain] = None

        # sounddevice output stream (None when unavailable or before start()).
        self._stream = None
        self._running: bool = False

        # Lock used only when replacing the stream itself (rare).
        self._stream_lock = threading.Lock()

    # ── Chain management ──────────────────────────────────────────────────────

    def set_chain(self, chain: Optional[AudioFxChain]) -> None:
        """Replace the FX chain.  Thread-safe (single reference assignment)."""
        self._chain = chain

    def get_chain(self) -> Optional[AudioFxChain]:
        return self._chain

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Open the sounddevice output stream and begin rendering.

        Returns True on success, False if sounddevice is unavailable or the
        stream could not be opened (e.g. no audio device present).
        """
        if self._running:
            return True

        try:
            import sounddevice as sd  # optional dependency

            with self._stream_lock:
                self._stream = sd.OutputStream(
                    samplerate=self._sample_rate,
                    channels=2,
                    dtype="float32",
                    blocksize=self._block_size,
                    callback=self._audio_callback,
                    finished_callback=self._on_stream_finished,
                )
                self._stream.start()
                self._running = True

            logger.info(
                "InstrumentRenderer: stream started (sr=%d, block=%d)",
                self._sample_rate, self._block_size,
            )
            return True

        except ImportError:
            logger.warning(
                "InstrumentRenderer: sounddevice not installed — "
                "instrument audio will be silent.  "
                "Fix with: pip install sounddevice"
            )
            return False
        except Exception as exc:
            logger.warning("InstrumentRenderer: stream open failed — %s", exc)
            return False

    def stop(self) -> None:
        """Stop the output stream and release audio resources."""
        self._running = False
        with self._stream_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
        logger.debug("InstrumentRenderer: stream stopped.")

    @property
    def is_running(self) -> bool:
        return self._running

    # ── MIDI note routing ─────────────────────────────────────────────────────

    def note_on(self, pitch: int, velocity: float) -> None:
        """
        Forward a note-on to every instrument plugin in the current chain.

        An 'instrument plugin' is any FxPluginBase subclass that exposes a
        note_on(midi_note, velocity) method (e.g. SamplerPlugin).
        """
        chain = self._chain
        if chain is None:
            return
        for plugin in list(chain.plugins):
            if plugin is not None and hasattr(plugin, "note_on"):
                try:
                    plugin.note_on(int(pitch), float(velocity))
                except Exception as exc:
                    logger.debug("InstrumentRenderer.note_on: %s", exc)

    def note_off(self, pitch: int) -> None:
        """Forward a note-off to every instrument plugin in the current chain."""
        chain = self._chain
        if chain is None:
            return
        for plugin in list(chain.plugins):
            if plugin is not None and hasattr(plugin, "note_off"):
                try:
                    plugin.note_off(int(pitch))
                except Exception as exc:
                    logger.debug("InstrumentRenderer.note_off: %s", exc)

    # ── sounddevice callback (RT thread) ──────────────────────────────────────

    def _audio_callback(
        self, outdata: np.ndarray, frames: int, time, status
    ) -> None:
        """
        sounddevice output callback — called from the audio RT thread.

        outdata shape: (frames, 2) float32 — we fill it in place.

        The chain snapshot (list()) is taken at the top of each block so GUI
        edits become audible on the very next block (≤ BLOCK_SIZE / sr delay).
        """
        # Start from silence (instrument plugins ADD audio to the buffer).
        outdata[:] = 0.0

        chain = self._chain
        if chain is None:
            return

        # Zero input buffer that instrument plugins will add into.
        audio = np.zeros((frames, 2), dtype=np.float32)

        try:
            # Pass through every active plugin in the chain.
            audio = chain.process(audio, self._sample_rate)

            # Apply volume and pan from the chain's routing parameters.
            n_ch = audio.shape[1] if audio.ndim == 2 else 1
            audio = chain.apply_gain_pan(audio, n_ch)

        except Exception as exc:
            # Never raise from the RT callback — just log and continue.
            logger.debug("InstrumentRenderer callback error: %s", exc)
            return

        # Copy result to the output buffer, guarding against shape mismatches.
        n_out = min(frames, audio.shape[0])
        if audio.ndim == 2 and audio.shape[1] >= 2:
            outdata[:n_out, 0] = audio[:n_out, 0]
            outdata[:n_out, 1] = audio[:n_out, 1]
        elif audio.ndim == 2:
            outdata[:n_out, 0] = audio[:n_out, 0]
            outdata[:n_out, 1] = audio[:n_out, 0]
        else:
            outdata[:n_out, 0] = audio[:n_out]
            outdata[:n_out, 1] = audio[:n_out]

    def _on_stream_finished(self) -> None:
        """Called by sounddevice when the stream ends unexpectedly."""
        self._running = False
        logger.debug("InstrumentRenderer: stream finished callback.")
