"""
timeline_engine_bridge.py  --  C++ TimelineEngine ↔ Python bridge
==================================================================
Owns the sounddevice output stream and connects the C++ TimelineEngine
to FluidSynth for MIDI dispatch.

Architecture
------------
The bridge sits between the Python data layer (MidiLogic, AudioTrack) and
the C++ real-time engine (daw_processors.TimelineEngine).

┌──────────────┐    load events     ┌──────────────────────────┐
│  MidiLogic   │ ─────────────────► │  daw_processors.         │
│  (data only) │                    │  TimelineEngine (C++)     │
└──────────────┘                    │  · atomic frame counter   │
                                    │  · audio clip rendering   │
                                    │  · MIDI scheduling        │
                                    └─────────┬────────────────┘
                                              │ process_block_into()
                                              │ pop_midi_events()
                                    ┌─────────▼────────────────┐
                                    │  sounddevice callback     │
                                    │  (PortAudio C thread)     │
                                    │  · fills stereo output    │
                                    │  · dispatches MIDI →      │
                                    │    FluidSynth             │
                                    └──────────────────────────┘

Thread safety
-------------
process_block_into() releases the GIL during C++ processing; FluidSynth
note events are dispatched with the GIL held, which is safe because
FluidSynth's C library is thread-safe for note-on/note-off.
"""

from __future__ import annotations

import logging
import numpy as np
from typing import Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Frames per audio callback.  512 at 44 100 Hz ≈ 11.6 ms latency.
# Increase to 1024 if the system produces underruns.
_BLOCK_SIZE = 512

# Maximum block size that the C++ engine accepts (matches DspHelpers.h).
_MAX_BLOCK = 4096


class TimelineEngineBridge:
    """
    Thin Python shell around daw_processors.TimelineEngine.

    Responsibilities
    ----------------
    * Own the sounddevice output stream.
    * Call process_block_into() from the audio callback (GIL released inside C++).
    * Drain pop_midi_events() and forward to FluidSynth via the registered callback.
    * Convert beats ↔ sample frames for callers that work in beat-time.
    * Provide optional fallback: when daw_processors is not importable the bridge
      becomes a no-op so the application still runs without the C++ extension.
    """

    def __init__(self, sample_rate: int = 44100, bpm: float = 120.0) -> None:
        self._sample_rate = sample_rate
        self._bpm = bpm
        self._engine = None
        self._stream = None
        self._midi_dispatch: Optional[Callable[[int, int, int, bool], None]] = None

        # Pre-allocated output buffers (MAX_BLOCK long so any block size ≤ 4096 works).
        self._out_l = np.zeros(_MAX_BLOCK, dtype=np.float32)
        self._out_r = np.zeros(_MAX_BLOCK, dtype=np.float32)

        # Maps MidiTrack.channel → C++ track id for INSTRUMENT tracks.
        # Maps "audio_<track_id>" → C++ track id for AUDIO tracks.
        self._cpp_track_ids: Dict[str, int] = {}

        try:
            import daw_processors  # noqa: F401  (import-time check only)
            self._daw_processors = daw_processors
            self._engine = daw_processors.TimelineEngine(sample_rate, bpm)
            logger.info("TimelineEngineBridge: C++ engine initialised (%d Hz, %.1f BPM)",
                        sample_rate, bpm)
        except Exception as exc:
            logger.warning("TimelineEngineBridge: C++ engine unavailable (%s) — "
                           "falling back to Python playback.", exc)
            self._daw_processors = None

    # ── Beat / frame conversion ───────────────────────────────────────────────

    def beats_to_frames(self, beats: float) -> int:
        """Convert a beat position to an absolute sample-frame index."""
        return int(beats * (60.0 / self._bpm) * self._sample_rate)

    def frames_to_beats(self, frames: int) -> float:
        """Convert a sample-frame index to a beat position."""
        spb = 60.0 / self._bpm
        return frames / (spb * self._sample_rate)

    # ── Stream management ─────────────────────────────────────────────────────

    def open_stream(self) -> bool:
        """
        Open and start the sounddevice output stream.

        Call once at application startup.  Returns True on success.
        The stream is a stereo float32 output; the callback fills it from C++.
        """
        if self._engine is None:
            return False
        if self._stream is not None:
            return True  # already open

        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(
                samplerate=self._sample_rate,
                blocksize=_BLOCK_SIZE,
                channels=2,
                dtype="float32",
                callback=self._audio_callback,
                latency="low",
            )
            self._stream.start()
            logger.info("TimelineEngineBridge: audio stream opened "
                        "(%d Hz, block=%d, stereo float32)",
                        self._sample_rate, _BLOCK_SIZE)
            return True
        except Exception as exc:
            logger.error("TimelineEngineBridge: failed to open audio stream: %s", exc)
            self._stream = None
            return False

    def close_stream(self) -> None:
        """Stop and close the sounddevice output stream."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            logger.info("TimelineEngineBridge: audio stream closed.")

    @property
    def is_available(self) -> bool:
        """True when the C++ engine loaded successfully."""
        return self._engine is not None

    # ── MIDI dispatch ─────────────────────────────────────────────────────────

    def set_midi_dispatch(
        self, callback: Callable[[int, int, int, bool], None]
    ) -> None:
        """
        Register the callback that receives fired MIDI events from C++.

        Signature: callback(channel: int, note: int, velocity: int, is_on: bool)

        The callback is invoked from the PortAudio thread with the GIL held,
        so it is safe to call FluidSynth note-on/note-off directly.
        """
        self._midi_dispatch = callback

    # ── Transport ─────────────────────────────────────────────────────────────

    def play(self, from_beat: float = 0.0) -> None:
        """Start C++ transport from *from_beat*."""
        if self._engine is not None:
            self._engine.play(self.beats_to_frames(from_beat))

    def stop(self) -> None:
        """Stop C++ transport.  Playhead position is preserved."""
        if self._engine is not None:
            self._engine.stop()

    def seek(self, beat: float) -> None:
        """Jump to *beat* without changing play/stop state."""
        if self._engine is not None:
            self._engine.seek(self.beats_to_frames(beat))

    def set_loop(self, enabled: bool, start_beat: float, end_beat: float) -> None:
        """Configure the loop region in beat units."""
        if self._engine is not None:
            self._engine.set_loop(
                enabled,
                self.beats_to_frames(start_beat),
                self.beats_to_frames(end_beat),
            )

    def current_beat(self) -> float:
        """Current playhead position in beats (lock-free read from C++)."""
        if self._engine is None:
            return 0.0
        return self._engine.current_beat()

    def current_frame(self) -> int:
        """Current playhead position in sample frames."""
        if self._engine is None:
            return 0
        return self._engine.current_frame()

    def is_playing(self) -> bool:
        """True while the C++ transport is running."""
        if self._engine is None:
            return False
        return self._engine.is_playing()

    # ── Host settings ─────────────────────────────────────────────────────────

    def set_bpm(self, bpm: float) -> None:
        """Update tempo.  Affects both beat↔frame conversion and the C++ engine."""
        self._bpm = bpm
        if self._engine is not None:
            self._engine.set_bpm(bpm)

    def set_sample_rate(self, sr: int) -> None:
        """Update host sample rate (call before opening the stream)."""
        self._sample_rate = sr
        if self._engine is not None:
            self._engine.set_sample_rate(sr)

    # ── Track management ──────────────────────────────────────────────────────

    def get_or_create_instrument_track(self, midi_channel: int) -> int:
        """
        Return the C++ track id mapped to *midi_channel*, creating one if needed.
        """
        key = f"midi_{midi_channel}"
        cpp_id = self._cpp_track_ids.get(key)
        if cpp_id is None and self._engine is not None:
            cpp_id = self._engine.add_instrument_track()
            self._cpp_track_ids[key] = cpp_id
        return cpp_id if cpp_id is not None else -1

    def get_or_create_audio_track(self, python_track_id: int) -> int:
        """
        Return the C++ track id mapped to a Python AudioTrack.track_id,
        creating one if needed.
        """
        key = f"audio_{python_track_id}"
        cpp_id = self._cpp_track_ids.get(key)
        if cpp_id is None and self._engine is not None:
            cpp_id = self._engine.add_audio_track()
            self._cpp_track_ids[key] = cpp_id
        return cpp_id if cpp_id is not None else -1

    def set_track_volume(self, cpp_track_id: int, volume: float) -> None:
        if self._engine is not None and cpp_track_id >= 0:
            self._engine.set_track_volume(cpp_track_id, volume)

    def set_track_pan(self, cpp_track_id: int, pan: float) -> None:
        if self._engine is not None and cpp_track_id >= 0:
            self._engine.set_track_pan(cpp_track_id, pan)

    def set_track_mute(self, cpp_track_id: int, muted: bool) -> None:
        if self._engine is not None and cpp_track_id >= 0:
            self._engine.set_track_mute(cpp_track_id, muted)

    def set_track_solo(self, cpp_track_id: int, soloed: bool) -> None:
        if self._engine is not None and cpp_track_id >= 0:
            self._engine.set_track_solo(cpp_track_id, soloed)

    # ── Automation helpers (accept Python-side track IDs) ─────────────────────

    def set_audio_track_volume(self, python_track_id: int, volume: float) -> None:
        """
        Set volume on an audio track identified by its Python track_id.

        Unlike set_track_volume() this method looks up the C++ track ID from
        the internal mapping and is a no-op when the track has not yet been
        registered with sync_all_tracks().  Safe to call from the GUI thread
        at ~20 Hz without creating spurious C++ tracks.
        """
        cpp_id = self._cpp_track_ids.get(f"audio_{python_track_id}", -1)
        if cpp_id >= 0 and self._engine is not None:
            self._engine.set_track_volume(cpp_id, volume)

    def set_audio_track_pan(self, python_track_id: int, pan: float) -> None:
        """
        Set stereo pan on an audio track identified by its Python track_id.

        Same no-op guarantee as set_audio_track_volume().
        """
        cpp_id = self._cpp_track_ids.get(f"audio_{python_track_id}", -1)
        if cpp_id >= 0 and self._engine is not None:
            self._engine.set_track_pan(cpp_id, pan)

    # ── Audio clip loading ────────────────────────────────────────────────────

    def load_audio_clip_from_path(
        self,
        python_track_id: int,
        path: str,
        start_beat: float,
    ) -> bool:
        """
        Decode an audio file and push it to the C++ engine as an audio clip.

        Requires soundfile (pip install soundfile).  If resampy is installed,
        the audio is resampled to the engine sample rate automatically.

        Returns True on success.
        """
        if self._engine is None:
            return False

        try:
            import soundfile as sf
        except ImportError:
            logger.error("soundfile not installed — cannot decode audio clip '%s'.", path)
            return False

        try:
            data, file_sr = sf.read(path, dtype="float32", always_2d=True)
        except Exception as exc:
            logger.error("Cannot read audio file '%s': %s", path, exc)
            return False

        if data.shape[1] == 1:
            left = right = np.ascontiguousarray(data[:, 0])
        else:
            left  = np.ascontiguousarray(data[:, 0])
            right = np.ascontiguousarray(data[:, 1])

        # Resample to engine sample rate when there is a mismatch.
        if file_sr != self._sample_rate:
            try:
                import resampy
                left  = resampy.resample(left,  file_sr, self._sample_rate)
                right = resampy.resample(right, file_sr, self._sample_rate)
            except ImportError:
                logger.warning(
                    "resampy not installed — audio clip '%s' will play at wrong "
                    "speed (file SR %d ≠ engine SR %d).",
                    path, file_sr, self._sample_rate,
                )

        cpp_id = self.get_or_create_audio_track(python_track_id)
        if cpp_id < 0:
            return False

        start_frame = self.beats_to_frames(start_beat)
        self._engine.load_audio_clip(cpp_id, left, right, start_frame, path)
        logger.debug("Loaded audio clip '%s' → C++ track %d at frame %d",
                     path, cpp_id, start_frame)
        return True

    # ── MIDI event loading ────────────────────────────────────────────────────

    def load_midi_track(
        self,
        midi_channel: int,
        absolute_notes,
    ) -> None:
        """
        Convert a list of MidiNote objects (with absolute start_beat) into C++
        MIDI events and push them to the engine.

        Existing events for this track are replaced.  Call sort_midi_events()
        is called implicitly after the bulk add so the engine never sees
        unsorted events.
        """
        if self._engine is None:
            return

        cpp_id = self.get_or_create_instrument_track(midi_channel)
        if cpp_id < 0:
            return

        self._engine.clear_midi_events(cpp_id)

        for note in absolute_notes:
            on_frame  = self.beats_to_frames(note.start_beat)
            off_frame = self.beats_to_frames(note.start_beat + note.duration)
            ch  = int(note.channel) & 0x0F
            pit = max(0, min(127, int(note.pitch)))
            vel = max(1, min(127, int(note.velocity)))
            self._engine.add_midi_event(cpp_id, on_frame,  0x90, ch, pit, vel)
            self._engine.add_midi_event(cpp_id, off_frame, 0x80, ch, pit, 0)

        self._engine.sort_midi_events(cpp_id)
        logger.debug("Loaded %d note(s) → C++ MIDI track (channel %d, id %d)",
                     len(list(absolute_notes)), midi_channel, cpp_id)

    def load_step_events(
        self,
        midi_channel: int,
        step_events,
    ) -> None:
        """
        Push step sequencer note-on/off tuples to the C++ engine.

        *step_events* is a list of (beat, channel, note, velocity, is_on) tuples
        as produced by MidiLogic._build_step_events().  Events are appended to
        (not replacing) the existing MIDI events for this track, so call
        load_midi_track() first then load_step_events().
        """
        if self._engine is None:
            return

        cpp_id = self.get_or_create_instrument_track(midi_channel)
        if cpp_id < 0:
            return

        for beat, ch, note, velocity, is_on in step_events:
            frame  = self.beats_to_frames(beat)
            ev_type = 0x90 if is_on else 0x80
            vel_val = max(0, min(127, int(velocity))) if is_on else 0
            self._engine.add_midi_event(cpp_id, frame, ev_type,
                                        int(ch) & 0x0F,
                                        max(0, min(127, int(note))),
                                        vel_val)

        self._engine.sort_midi_events(cpp_id)

    # ── Full project sync (called before play()) ──────────────────────────────

    def sync_all_tracks(self, midi_logic) -> None:
        """
        Push the complete project state from *midi_logic* into the C++ engine.

        Called by MidiLogic.play() (when a bridge is attached) before starting
        the transport so the C++ engine has an up-to-date snapshot of every
        MIDI note and audio clip.

        Args:
            midi_logic: A MidiLogic instance (duck-typed; no import needed).
        """
        if self._engine is None:
            return

        self._engine.set_bpm(midi_logic.bpm)

        # ── MIDI tracks ───────────────────────────────────────────────────────
        for track in midi_logic.get_all_tracks():
            self.load_midi_track(track.channel, track.sorted_notes())

        # ── Step sequencer rows ───────────────────────────────────────────────
        step_rows = getattr(midi_logic, "_step_rows", [])
        if step_rows:
            # Determine project length for step event generation.
            all_notes = [n for t in midi_logic.get_all_tracks()
                         for n in t.sorted_notes()]
            end_beat = (max(n.start_beat + n.duration for n in all_notes)
                        if all_notes else 0.0)
            for at in midi_logic.get_audio_tracks():
                for clip in at.clips:
                    clip_end = clip.start_beat + clip.duration_seconds * midi_logic.bpm / 60.0
                    end_beat = max(end_beat, clip_end)
            end_beat = max(end_beat + 1.0, 8.0)

            step_evs = midi_logic._build_step_events(0.0, end_beat)
            if step_evs:
                # Use a dedicated drum channel track (channel 9 = GM percussion).
                # Step events already carry their target channel; we group them under
                # a single C++ track keyed to channel 9 (the most common drum channel).
                drum_cpp_id = self.get_or_create_instrument_track(9)
                if drum_cpp_id >= 0:
                    self._engine.clear_midi_events(drum_cpp_id)
                    for beat, ch, note, velocity, is_on in step_evs:
                        frame   = self.beats_to_frames(beat)
                        ev_type = 0x90 if is_on else 0x80
                        vel_val = max(0, min(127, int(velocity))) if is_on else 0
                        self._engine.add_midi_event(
                            drum_cpp_id, frame, ev_type,
                            int(ch) & 0x0F,
                            max(0, min(127, int(note))),
                            vel_val,
                        )
                    self._engine.sort_midi_events(drum_cpp_id)

        # ── Audio tracks ──────────────────────────────────────────────────────
        for atrack in midi_logic.get_audio_tracks():
            cpp_id = self.get_or_create_audio_track(atrack.track_id)
            if cpp_id < 0:
                continue
            self._engine.clear_audio_clips(cpp_id)
            for clip in atrack.clips:
                try:
                    self.load_audio_clip_from_path(
                        atrack.track_id, clip.path, clip.start_beat
                    )
                except Exception as exc:
                    logger.warning("Could not load audio clip '%s': %s", clip.path, exc)

    # ── Audio callback (PortAudio thread) ─────────────────────────────────────

    def _audio_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info,
        status,
    ) -> None:
        """
        sounddevice OutputStream callback.

        Called from a PortAudio C thread via sounddevice.  The GIL is held by
        sounddevice's callback wrapper, so:
        - process_block_into() releases the GIL during C++ rendering.
        - pop_midi_events() + FluidSynth dispatch happen with the GIL held.
        """
        if self._engine is None:
            outdata[:] = 0
            return

        # Clamp to the pre-allocated scratch buffer size.
        n = min(frames, _MAX_BLOCK)

        # Fill _out_l/_out_r in-place; GIL is released inside C++ binding.
        out_l_view = self._out_l[:n]
        out_r_view = self._out_r[:n]
        self._engine.process_block_into(out_l_view, out_r_view)

        # Interleave L/R into the PortAudio stereo output buffer.
        outdata[:n, 0] = out_l_view
        outdata[:n, 1] = out_r_view
        if n < frames:
            outdata[n:] = 0  # pad if block was shorter than requested

        # Dispatch fired MIDI events to FluidSynth (GIL held).
        events = self._engine.pop_midi_events()
        if events and self._midi_dispatch is not None:
            for ev in events:
                try:
                    self._midi_dispatch(ev.channel, ev.note, ev.velocity, ev.is_on)
                except Exception:
                    pass
