"""
project_render_pipeline.py -- Full-project offline render orchestrator.
========================================================================
Converts a FullProjectRenderInfo snapshot into a (2, N) float32 numpy array
representing the complete stereo mix of the project.

Architecture
------------
  Layer 1 — MIDI synthesis  (Python glue, C synthesis)
      One FluidSynth instance renders ALL MIDI channels + step-sequencer
      events in a single block-by-block pass.  Volume / pan automation is
      applied per block as FluidSynth CC7 / CC10 messages, matching the
      live-playback and basic ExportWorker behaviour exactly.

  Layer 2 — Audio clip decode  (Python glue, C decode)
      Each audio track's clips are decoded from disk (pedalboard → soundfile
      fallback), positioned in the timeline, and accumulated into a per-track
      float32 buffer.

  Layer 3 — Per-track FX chains  (Python / pedalboard)
      AudioFxChain.process() applies EQ, reverb, compression etc. per track.

  Layer 4 — C++ mix bus  (C++ FullProjectRenderer)
      All rendered track buffers are mixed into the stereo bus with per-frame
      volume / pan automation applied by the C++ AutomationProcessor.
      Falls back to FullProjectRendererPython when the C++ module is absent.

  Layer 5 — Master FX chain  (Python / pedalboard — optional)
      If the GUI has created a master AudioFxChain it is applied to the
      finished stereo mix before the mastering export pipeline (LUFS,
      limiter, normalisation) takes over.

Caller interface
----------------
  pipeline = ProjectRenderPipeline(render_info)
  mix = pipeline.render(
      status_fn   = lambda msg: ...,          # human-readable status
      progress_fn = lambda done, total: ...,  # track-level progress
      cancelled_fn = lambda: False,           # return True to abort
  )
  # mix is (2, N) float32 or None on failure / cancellation.

  # Stem rendering (one track at a time):
  stem = pipeline.render_single_audio_track(track_info)
  stem = pipeline.render_single_midi_track(midi_track_info, n_frames)
"""

from __future__ import annotations

import logging
import os
from typing import Callable, List, Optional, Tuple

import numpy as np

from .automation_processor_python import get_automation_processor
from .full_project_renderer_python import get_full_project_renderer
from .project_render_info import (
    AutomationRenderInfo,
    FullProjectRenderInfo,
    MidiTrackRenderInfo,
)

logger = logging.getLogger(__name__)

# All render targets use 44.1 kHz — matches EXPORT_SR in mastering_export_worker.py.
RENDER_SR   = 44_100
# Extra silence appended after the last event so reverb tails can fully decay.
TAIL_SECS   = 2.0
# Minimum project length so an all-empty project still produces a valid file.
MIN_SECS    = 4.0
# FluidSynth block size (frames per get_samples() call).
FLUID_BLOCK = 1024


class ProjectRenderPipeline:
    """
    Offline render pipeline for the full project.

    Instantiate with a FullProjectRenderInfo snapshot, then call render().
    """

    def __init__(self, render_info: FullProjectRenderInfo) -> None:
        self._info = render_info
        self._sr   = render_info.sample_rate or RENDER_SR

    # ── Public API ────────────────────────────────────────────────────────────

    def render(
        self,
        status_fn:    Optional[Callable[[str], None]]        = None,
        progress_fn:  Optional[Callable[[int, int], None]]   = None,
        cancelled_fn: Optional[Callable[[], bool]]           = None,
    ) -> Optional[np.ndarray]:
        """
        Render the entire project to a (2, N) float32 stereo array.

        Returns None on failure or cancellation.

        Parameters
        ----------
        status_fn    : Called with a human-readable status string.
        progress_fn  : Called with (tracks_done, tracks_total) counts.
        cancelled_fn : Called before each major step; return True to abort.
        """
        def cancelled() -> bool:
            return cancelled_fn() if cancelled_fn else False

        def status(msg: str) -> None:
            if status_fn:
                status_fn(msg)

        n_frames  = self._compute_n_frames()
        renderer  = get_full_project_renderer()
        renderer.prepare(n_frames, self._sr)

        n_total = (
            (1 if (self._info.midi_tracks or self._info.step_events) else 0)
            + len(self._info.audio_tracks)
        )
        done = 0

        # ── Layer 1: MIDI + step events via FluidSynth ────────────────────────
        has_midi = bool(self._info.midi_tracks or self._info.step_events)
        if has_midi:
            if cancelled():
                return None
            status("Rendering MIDI tracks via FluidSynth…")
            midi_buf = self._render_all_midi(n_frames, status, cancelled_fn)
            if midi_buf is not None:
                # The MIDI buffer already has volume/pan baked in via CC7/CC10,
                # so mix it at unity gain with no additional automation.
                renderer.mix_track(
                    np.ascontiguousarray(midi_buf[0]),
                    np.ascontiguousarray(midi_buf[1]),
                    at_frame = 0,
                    volume   = 1.0,
                    pan      = 0.0,
                )
            done += 1
            if progress_fn:
                progress_fn(done, n_total)

        # ── Layer 2 & 3: Audio tracks (decode + FX) ───────────────────────────
        for audio_track in self._info.audio_tracks:
            if cancelled():
                return None
            status(f"Rendering audio: {audio_track.name}")

            # Decode all clips for this track into a single full-length buffer.
            track_buf = self._render_audio_track_to_buffer(audio_track, n_frames)
            if track_buf is not None:
                # Apply the per-track FX chain (EQ, reverb, compression…).
                track_buf = _apply_fx_chain(track_buf, audio_track.fx_chain, self._sr)

                # Build automation objects for volume and pan.
                vol_auto = _build_automation_processor(audio_track.automation, "volume")
                pan_auto = _build_automation_processor(audio_track.automation, "pan")

                # Mix into the C++ (or Python) bus with per-frame automation.
                renderer.mix_track(
                    np.ascontiguousarray(track_buf[0]),
                    np.ascontiguousarray(track_buf[1]),
                    at_frame = 0,
                    volume   = audio_track.volume,
                    pan      = audio_track.pan,
                    vol_auto = vol_auto,
                    pan_auto = pan_auto,
                )

            done += 1
            if progress_fn:
                progress_fn(done, n_total)

        # ── Retrieve stereo mix from the bus ──────────────────────────────────
        L = np.array(renderer.get_L(), dtype=np.float32)
        R = np.array(renderer.get_R(), dtype=np.float32)
        mix = np.vstack([L, R])

        # ── Layer 5: Optional master FX chain ─────────────────────────────────
        # (No master chain is defined in the current project architecture;
        #  this hook lets a future GUI addition plug one in without changes here.)
        master_fx = getattr(self._info, "master_fx_chain", None)
        if master_fx is not None:
            mix = _apply_fx_chain(mix, master_fx, self._sr)

        return mix

    # ── Stem helpers (called by MasteringExportWorker for stem export) ────────

    def render_single_audio_track(self, track) -> Optional[np.ndarray]:
        """
        Render one audio track to (2, N) float32 with its FX chain applied.

        Used by the stem export step.  The buffer length is sized to cover
        only the track's own clips (not the full project duration).
        """
        n_frames = self._compute_n_frames()
        buf = self._render_audio_track_to_buffer(track, n_frames)
        if buf is None:
            return None
        return _apply_fx_chain(buf, track.fx_chain, self._sr)

    def render_single_midi_track(
        self,
        midi_track: MidiTrackRenderInfo,
        n_frames:   Optional[int] = None,
    ) -> Optional[np.ndarray]:
        """
        Render one MIDI channel in isolation to (2, N) float32.

        Creates its own FluidSynth instance so other channels are silent.
        Used for MIDI stem export.
        """
        if n_frames is None:
            n_frames = self._compute_n_frames()
        return _render_single_midi_channel(midi_track, n_frames, self._info.bpm, self._sr)

    # ── Internal: MIDI rendering ──────────────────────────────────────────────

    def _render_all_midi(
        self,
        n_frames:     int,
        status_fn:    Optional[Callable],
        cancelled_fn: Optional[Callable],
    ) -> Optional[np.ndarray]:
        """
        Render ALL MIDI tracks and step-sequencer events in one FluidSynth pass.

        All channels are rendered simultaneously (matching the live and
        basic-export behaviour) into a single (2, N) float32 buffer.
        Per-channel automation (volume / pan) is baked in via CC7 / CC10
        messages injected at block boundaries.

        Returns None if FluidSynth is not installed or no SF2 files load.
        """
        try:
            import fluidsynth  # type: ignore[import]
        except ImportError:
            logger.warning(
                "pyfluidsynth not installed — MIDI tracks skipped."
                "  Install with: pip install pyfluidsynth"
            )
            return None

        spb = 60.0 / max(1.0, self._info.bpm)   # seconds per beat

        # Create the offline synth (no audio driver = no real-time output).
        fs = fluidsynth.Synth(gain=0.8, samplerate=float(self._sr))

        # Load each unique SF2 file once and configure all channels.
        sfid_map: dict = {}
        for midi_track in self._info.midi_tracks:
            sf2 = midi_track.sf2_path
            if not sf2 or not os.path.isfile(sf2):
                continue
            if sf2 not in sfid_map:
                sfid = fs.sfload(sf2)
                if sfid == -1:
                    logger.warning("MIDI render: sfload failed for '%s'", sf2)
                    continue
                sfid_map[sf2] = sfid
            fs.program_select(
                midi_track.channel,
                sfid_map[sf2],
                int(midi_track.bank),
                int(midi_track.preset),
            )
            # Initial CC7/CC10 from the mixer strip values.
            # Volume: 0-1 range → CC 0-127.
            fs.cc(midi_track.channel, 7,
                  int(max(0.0, min(1.0, midi_track.volume)) * 127))
            fs.cc(midi_track.channel, 10,
                  int((midi_track.pan + 1.0) / 2.0 * 127))

        if not sfid_map:
            fs.delete()
            logger.warning("MIDI render: no SF2 files loaded — MIDI tracks skipped.")
            return None

        # ── Build sorted event list ──────────────────────────────────────────
        # Events are (frame, type, ch, arg1, [arg2]).
        # type: 'on', 'off', 'cc'
        events: List[Tuple] = []

        for midi_track in self._info.midi_tracks:
            for note in midi_track.notes:
                on_f  = int(note.start_beat * spb * self._sr)
                off_f = int((note.start_beat + note.duration) * spb * self._sr)
                events.append((on_f,  "on",  midi_track.channel, note.pitch, note.velocity))
                events.append((off_f, "off", midi_track.channel, note.pitch))

        # Step-sequencer events: (beat, ch, note, vel, is_on)
        for beat, ch, note, vel, is_on in self._info.step_events:
            frame = int(beat * spb * self._sr)
            if is_on:
                events.append((frame, "on",  ch, note, vel))
            else:
                events.append((frame, "off", ch, note))

        events.sort(key=lambda e: e[0])

        # ── Preprocess automation into per-channel CC event lists ─────────────
        # Automation for each MIDI channel is converted to CC7/CC10 events
        # at every automation node, then merged into the main event list.
        for midi_track in self._info.midi_tracks:
            for auto_info in midi_track.automation:
                if auto_info.target_key == "volume":
                    for t_secs, val in auto_info.points:
                        frame  = int(t_secs * self._sr)
                        cc_val = int(max(0.0, min(1.0, val)) * 127)
                        events.append((frame, "cc", midi_track.channel, 7, cc_val))
                elif auto_info.target_key == "pan":
                    for t_secs, val in auto_info.points:
                        frame  = int(t_secs * self._sr)
                        cc_val = int((val + 1.0) / 2.0 * 127)
                        events.append((frame, "cc", midi_track.channel, 10, cc_val))

        events.sort(key=lambda e: e[0])

        # ── Block-by-block render ─────────────────────────────────────────────
        out_L = np.zeros(n_frames, dtype=np.float32)
        out_R = np.zeros(n_frames, dtype=np.float32)
        ev_idx = 0
        n_ev   = len(events)

        for frame_start in range(0, n_frames, FLUID_BLOCK):
            if cancelled_fn and cancelled_fn():
                fs.delete()
                return None

            frame_end  = min(frame_start + FLUID_BLOCK, n_frames)
            block_size = frame_end - frame_start

            # Fire all events whose position falls in this block.
            while ev_idx < n_ev and events[ev_idx][0] < frame_end:
                ev = events[ev_idx]
                try:
                    if ev[1] == "on":
                        _, _, ch, pitch, vel = ev
                        if vel > 0:
                            fs.noteon(ch, pitch, vel)
                        else:
                            fs.noteoff(ch, pitch)
                    elif ev[1] == "off":
                        _, _, ch, pitch = ev
                        fs.noteoff(ch, pitch)
                    elif ev[1] == "cc":
                        _, _, ch, cc_num, cc_val = ev
                        fs.cc(ch, cc_num, cc_val)
                except Exception as exc:
                    logger.debug("FluidSynth event error at frame %d: %s", frame_start, exc)
                ev_idx += 1

            # Render FLUID_BLOCK samples (FluidSynth always renders the full block).
            try:
                raw = fs.get_samples(FLUID_BLOCK)
                arr = np.array(raw, dtype=np.int16).reshape(-1, 2)
                actual = min(block_size, len(arr))
                flt    = arr[:actual].astype(np.float32) / 32768.0
                out_L[frame_start : frame_start + actual] = flt[:, 0]
                out_R[frame_start : frame_start + actual] = flt[:, 1]
            except Exception as exc:
                logger.warning("FluidSynth get_samples failed at frame %d: %s",
                               frame_start, exc)

        fs.delete()

        n_loaded = len(self._info.midi_tracks)
        if status_fn:
            status_fn(f"  MIDI complete ({n_loaded} track(s) + step events).")

        return np.vstack([out_L, out_R])

    # ── Internal: audio clip rendering ───────────────────────────────────────

    def _render_audio_track_to_buffer(
        self,
        track,          # TrackRenderInfo
        n_frames: int,
    ) -> Optional[np.ndarray]:
        """
        Decode all clips for one audio track and position them in a
        (2, n_frames) float32 buffer.  The buffer is pre-zeroed so silent
        regions between clips are handled automatically.

        Returns None if the track has no clips or all clips fail to decode.
        """
        if not track.clips:
            return None

        buf        = np.zeros((2, n_frames), dtype=np.float32)
        placed_any = False
        spb        = 60.0 / max(1.0, self._info.bpm)

        for clip in track.clips:
            audio = _decode_clip(clip, self._sr)
            if audio is None:
                continue

            # Position the clip at its timeline beat offset.
            at_frame  = int(clip.start_beat * spb * self._sr)
            end_frame = at_frame + audio.shape[1]

            if at_frame >= n_frames:
                continue  # clip starts beyond the mix bus — skip
            if end_frame > n_frames:
                audio     = audio[:, : n_frames - at_frame]
                end_frame = n_frames

            buf[:, at_frame:end_frame] += audio
            placed_any = True

        return buf if placed_any else None

    # ── Internal: duration calculation ────────────────────────────────────────

    def _compute_n_frames(self) -> int:
        """Compute the total frame count covering all events + the decay tail."""
        spb = 60.0 / max(1.0, self._info.bpm)
        end = MIN_SECS

        # MIDI notes.
        for midi_track in self._info.midi_tracks:
            for note in midi_track.notes:
                end = max(end, (note.start_beat + note.duration) * spb)

        # Step events (only beat position, no duration available).
        for beat, *_ in self._info.step_events:
            end = max(end, beat * spb + 0.5)  # +0.5 s to let the note ring

        # Audio clips.
        for audio_track in self._info.audio_tracks:
            for clip in audio_track.clips:
                end = max(end, clip.start_beat * spb + clip.duration_seconds)

        return int((end + TAIL_SECS) * self._sr)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decode_clip(clip, target_sr: int) -> Optional[np.ndarray]:
    """
    Decode one audio clip to a (2, n_frames) float32 array at target_sr.

    Tries pedalboard.io.AudioFile first (MP3/OGG/FLAC/WAV), then soundfile.
    Returns None if the file cannot be read.
    """
    if not os.path.isfile(clip.path):
        logger.warning("Clip file not found: %s", clip.path)
        return None

    # Backend 1: pedalboard.io — handles MP3 and most formats.
    try:
        from pedalboard.io import AudioFile  # type: ignore[import]
        with AudioFile(clip.path).resampled_to(target_sr) as f:
            data = f.read(f.frames).astype(np.float32)   # (channels, frames)
        if data.ndim == 1:
            data = np.vstack([data[np.newaxis], data[np.newaxis]])
        elif data.shape[0] == 1:
            data = np.vstack([data, data])
        return data[:2]
    except Exception:
        pass

    # Backend 2: soundfile with linear resample.
    try:
        import soundfile as sf  # type: ignore[import]
        raw, file_sr = sf.read(clip.path, dtype="float32", always_2d=True)
        data = raw.T.astype(np.float32)  # (channels, frames)
        if data.shape[0] == 1:
            data = np.vstack([data, data])
        if file_sr != target_sr:
            ratio   = target_sr / file_sr
            n_out   = int(data.shape[1] * ratio)
            x_old   = np.arange(data.shape[1], dtype=np.float64)
            x_new   = np.linspace(0.0, data.shape[1] - 1, n_out)
            data    = np.vstack([
                np.interp(x_new, x_old, data[0]).astype(np.float32),
                np.interp(x_new, x_old, data[1]).astype(np.float32),
            ])
        return data[:2]
    except Exception as exc:
        logger.warning("_decode_clip: cannot read '%s': %s", clip.path, exc)
        return None


def _apply_fx_chain(
    audio:   np.ndarray,   # (2, n_frames) float32
    chain,                 # AudioFxChain or None
    sr:      int,
) -> np.ndarray:
    """
    Pass (2, n_frames) float32 through an AudioFxChain.

    AudioFxChain.process() expects (n_frames, n_channels) so we transpose
    before and after the call.  Returns the original array on any error.
    """
    if chain is None:
        return audio
    try:
        processed = chain.process(audio.T, sr)        # → (n_frames, 2)
        if processed is not None and processed.shape[0] > 0:
            return processed.T.astype(np.float32)     # → (2, n_frames)
    except Exception as exc:
        logger.debug("_apply_fx_chain error: %s", exc)
    return audio


def _build_automation_processor(
    automation: List[AutomationRenderInfo],
    target_key: str,
):
    """
    Find the AutomationRenderInfo for target_key and build an
    AutomationProcessor loaded with its (time_secs, value) points.

    Returns an AutomationProcessor (C++ or Python) if points exist,
    or None if no automation is defined for this parameter.
    """
    for auto_info in automation:
        if auto_info.target_key == target_key and auto_info.points:
            proc = get_automation_processor()
            for t, v in auto_info.points:
                proc.add_point(float(t), float(v))
            return proc
    return None


def _render_single_midi_channel(
    midi_track: MidiTrackRenderInfo,
    n_frames:   int,
    bpm:        float,
    sr:         int,
) -> Optional[np.ndarray]:
    """
    Render one MIDI channel in isolation to (2, n_frames) float32.

    Uses a dedicated FluidSynth instance so only the target channel is
    audible.  Used for per-MIDI-track stem export.
    """
    try:
        import fluidsynth  # type: ignore[import]
    except ImportError:
        return None

    sf2 = midi_track.sf2_path
    if not sf2 or not os.path.isfile(sf2):
        return None

    spb = 60.0 / max(1.0, bpm)
    fs  = fluidsynth.Synth(gain=0.8, samplerate=float(sr))
    sfid = fs.sfload(sf2)
    if sfid == -1:
        fs.delete()
        return None

    fs.program_select(midi_track.channel, sfid,
                      int(midi_track.bank), int(midi_track.preset))
    fs.cc(midi_track.channel, 7,  int(max(0.0, min(1.0, midi_track.volume)) * 127))
    fs.cc(midi_track.channel, 10, int((midi_track.pan + 1.0) / 2.0 * 127))

    # Build event list (only this channel's notes + automation).
    events: List[Tuple] = []
    for note in midi_track.notes:
        on_f  = int(note.start_beat * spb * sr)
        off_f = int((note.start_beat + note.duration) * spb * sr)
        events.append((on_f,  "on",  midi_track.channel, note.pitch, note.velocity))
        events.append((off_f, "off", midi_track.channel, note.pitch))
    for auto_info in midi_track.automation:
        if auto_info.target_key == "volume":
            for t_secs, val in auto_info.points:
                events.append((int(t_secs * sr), "cc", midi_track.channel, 7,
                                int(max(0.0, min(1.0, val)) * 127)))
        elif auto_info.target_key == "pan":
            for t_secs, val in auto_info.points:
                events.append((int(t_secs * sr), "cc", midi_track.channel, 10,
                                int((val + 1.0) / 2.0 * 127)))
    events.sort(key=lambda e: e[0])

    out_L  = np.zeros(n_frames, dtype=np.float32)
    out_R  = np.zeros(n_frames, dtype=np.float32)
    ev_idx = 0
    n_ev   = len(events)

    for frame_start in range(0, n_frames, FLUID_BLOCK):
        frame_end  = min(frame_start + FLUID_BLOCK, n_frames)
        block_size = frame_end - frame_start
        while ev_idx < n_ev and events[ev_idx][0] < frame_end:
            ev = events[ev_idx]
            try:
                if ev[1] == "on":
                    fs.noteon(ev[2], ev[3], ev[4])
                elif ev[1] == "off":
                    fs.noteoff(ev[2], ev[3])
                elif ev[1] == "cc":
                    fs.cc(ev[2], ev[3], ev[4])
            except Exception:
                pass
            ev_idx += 1
        try:
            raw    = fs.get_samples(FLUID_BLOCK)
            arr    = np.array(raw, dtype=np.int16).reshape(-1, 2)
            actual = min(block_size, len(arr))
            flt    = arr[:actual].astype(np.float32) / 32768.0
            out_L[frame_start : frame_start + actual] = flt[:, 0]
            out_R[frame_start : frame_start + actual] = flt[:, 1]
        except Exception:
            pass

    fs.delete()

    # Apply per-track FX chain if present.
    result = np.vstack([out_L, out_R])
    return _apply_fx_chain(result, midi_track.fx_chain, sr)
