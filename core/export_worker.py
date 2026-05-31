"""
export_worker.py  --  Full-Project Background Export Thread
============================================================
ExportWorker renders the entire project — both MIDI instrument tracks and
audio file tracks — into a single stereo audio file.

Rendering pipeline
------------------
1.  MIDI tracks   → pyfluidsynth offline API  → float32 PCM buffers
2.  Audio tracks  → pedalboard AudioFile      → float32 PCM buffers
3.  Both paths    → C++ OfflineExporter       → mix bus (32-bit float)
4.  OfflineExporter.write_wav()               → final WAV on disk
5.  Optional ffmpeg encoding                  → MP3 or AAC (.m4a)

Running in a QThread keeps the GUI responsive.  Progress and log messages
are delivered via Qt signals so a QProgressDialog can display them.

Architecture note
-----------------
All audio mixing runs inside the C++ OfflineExporter (daw_processors module).
Python is only responsible for loading data and calling the C++ API.
FluidSynth handles MIDI synthesis in its own C library — it is also C-level
processing, accessed here via the pyfluidsynth Python bindings.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Dict, List, Optional

import numpy as np
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

# All exported audio uses this sample rate (CD quality).
EXPORT_SAMPLE_RATE = 44100

# Extra tail added after the last event so reverb / delay can fully decay.
TAIL_SECONDS = 2


class ExportWorker(QThread):
    """
    QThread that renders the full project (MIDI + audio) to a single file.

    Signals
    -------
    progress(int)   0–100 percentage complete.
    log_msg(str)    Human-readable status line for the progress dialog.
    finished(bool)  True = export succeeded; False = an error occurred.
    """

    progress = Signal(int)    # 0-100
    log_msg  = Signal(str)    # status text for progress dialog
    finished = Signal(bool)   # True = success

    def __init__(
        self,
        out_path:        str,
        fmt:             str,           # "wav" | "mp3" | "aac"
        audio_tracks:    List,          # list[AudioTrack] from MidiLogic
        midi_tracks:     List,          # list[MidiTrack]  from MidiLogic
        instruments:     Dict,          # {channel: InstrumentPlugin}
        audio_fx_chains: Dict,          # {track_id: AudioFxChain}
        bpm:             float,
        bit_depth:       int = 24,      # 16, 24, or 32
        step_events:     Optional[List] = None,  # [(beat, ch, note, vel, is_on)]
        midi_fx_chains:  Optional[Dict] = None,  # {channel: AudioFxChain} — for automation
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._out_path      = out_path
        self._fmt           = fmt.lower()
        self._audio_tracks  = list(audio_tracks)
        self._midi_tracks   = list(midi_tracks)
        self._instruments   = dict(instruments)
        self._audio_fx      = dict(audio_fx_chains)
        self._bpm           = max(1.0, bpm)
        self._bit_depth     = bit_depth if bit_depth in (16, 24, 32) else 24
        self._step_events   = list(step_events) if step_events else []
        self._midi_fx       = dict(midi_fx_chains) if midi_fx_chains else {}

    # ── QThread entry point ───────────────────────────────────────────────────

    def run(self) -> None:
        try:
            ok = self._do_export()
            self.finished.emit(ok)
        except Exception as exc:
            logger.error("ExportWorker crashed: %s", exc, exc_info=True)
            self.log_msg.emit(f"Fatal error: {exc}")
            self.finished.emit(False)

    # ── Main export logic ─────────────────────────────────────────────────────

    def _do_export(self) -> bool:
        # Try the C++ mix bus first; fall back to the pure-Python implementation
        # when the extension cannot be loaded (e.g. missing MSVC runtime on Windows).
        try:
            import daw_processors as dp
            exporter_class = dp.OfflineExporter
            self.log_msg.emit("Using C++ OfflineExporter.")
        except (ImportError, OSError):
            try:
                from .offline_exporter_python import OfflineExporterPython
                exporter_class = OfflineExporterPython
                self.log_msg.emit(
                    "C++ module unavailable — using Python OfflineExporter fallback.")
            except ImportError as exc:
                self.log_msg.emit(f"Export engine unavailable: {exc}")
                return False

        n_audio = len(self._audio_tracks)
        n_midi  = len(self._midi_tracks)
        if n_audio == 0 and n_midi == 0 and not self._step_events:
            self.log_msg.emit("Nothing to export — add some tracks first.")
            return False

        # Calculate total project duration.
        total_secs   = self._calc_total_seconds()
        total_frames = int(total_secs * EXPORT_SAMPLE_RATE) + \
                       int(TAIL_SECONDS * EXPORT_SAMPLE_RATE)
        total_frames = max(total_frames, EXPORT_SAMPLE_RATE)

        self.log_msg.emit(
            f"Exporting  {n_midi} MIDI track(s) + {n_audio} audio track(s)"
            f"  ({total_secs:.1f} s @ {EXPORT_SAMPLE_RATE} Hz, {self._bit_depth}-bit)…"
        )

        # Prepare the stereo mix bus.
        exporter = exporter_class()
        exporter.prepare(EXPORT_SAMPLE_RATE, total_frames)

        # ── Step 1: render MIDI tracks via FluidSynth offline ─────────────────
        if n_midi > 0 or self._step_events:
            self.log_msg.emit("Rendering MIDI tracks…")
            self._render_midi_tracks(exporter, total_frames)
            if self.isInterruptionRequested():
                return False

        self.progress.emit(50)

        # ── Step 2: mix audio file tracks through C++ OfflineExporter ─────────
        for idx, atrack in enumerate(self._audio_tracks):
            if self.isInterruptionRequested():
                self.log_msg.emit("Export cancelled.")
                return False
            pct = 50 + int(idx / max(1, n_audio) * 38)
            self.progress.emit(pct)
            self.log_msg.emit(f"  Audio track {idx + 1}/{n_audio}: {atrack.name}")
            chain = self._audio_fx.get(atrack.track_id)
            for clip in atrack.clips:
                self._mix_audio_clip(exporter, clip, chain)

        # ── Step 3: write WAV ─────────────────────────────────────────────────
        self.progress.emit(90)
        self.log_msg.emit("Writing WAV…")

        # For encoded formats write a temporary WAV first, then encode.
        need_encode = self._fmt in ("mp3", "aac")
        if need_encode:
            wav_path = os.path.splitext(self._out_path)[0] + "_tmp_export.wav"
        else:
            wav_path = self._out_path

        ok = exporter.write_wav(wav_path, self._bit_depth)
        if not ok:
            self.log_msg.emit(
                f"WAV write failed — check path / disk space: {wav_path}")
            return False

        # Report peak levels so the user can detect clipping.
        pk_l, pk_r = exporter.peak_left(), exporter.peak_right()
        if max(pk_l, pk_r) > 1.0:
            self.log_msg.emit(
                f"  Warning: mix clipped (L={pk_l:.2f}, R={pk_r:.2f}) — "
                "lower track volumes and re-export.")
        else:
            self.log_msg.emit(f"  Peak: L={pk_l:.3f}  R={pk_r:.3f}")

        self.progress.emit(94)

        # ── Step 4: optional encode ───────────────────────────────────────────
        if self._fmt == "mp3":
            ok = self._encode_ffmpeg(
                wav_path, self._out_path,
                ["-b:a", "192k"],
                "MP3",
            )
        elif self._fmt == "aac":
            ok = self._encode_ffmpeg(
                wav_path, self._out_path,
                ["-c:a", "aac", "-b:a", "192k"],
                "AAC",
            )

        if need_encode:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
            if not ok:
                return False

        self.progress.emit(100)
        self.log_msg.emit(f"Done  →  {self._out_path}")
        return True

    # ── MIDI offline rendering ────────────────────────────────────────────────

    def _render_midi_tracks(self, exporter, total_frames: int) -> None:
        """
        Render all MIDI tracks to PCM via pyfluidsynth offline API,
        then mix the result into the C++ OfflineExporter bus.

        FluidSynth renders to int16 interleaved stereo; this method converts
        that to float32 and calls exporter.mix_in() at frame 0.
        """
        try:
            import fluidsynth
        except ImportError:
            self.log_msg.emit(
                "  Warning: pyfluidsynth not installed — "
                "MIDI tracks skipped.  Install it with: pip install pyfluidsynth")
            return

        if not self._instruments and not self._midi_tracks and not self._step_events:
            return

        # Create an offline FluidSynth synth (no audio driver opened).
        fs = fluidsynth.Synth(gain=0.8, samplerate=float(EXPORT_SAMPLE_RATE))

        # Load every required SF2 file and configure channels.
        sfid_map: Dict[str, int] = {}
        for channel, plugin in self._instruments.items():
            sf2 = getattr(plugin, "sf2_path", "")
            if not sf2 or not os.path.isfile(sf2):
                continue
            if sf2 not in sfid_map:
                sfid = fs.sfload(sf2)
                if sfid == -1:
                    logger.warning("MIDI export: could not load SF2 '%s'", sf2)
                    continue
                sfid_map[sf2] = sfid
            sfid = sfid_map[sf2]
            fs.program_select(
                channel, sfid,
                int(getattr(plugin, "bank",   0)),
                int(getattr(plugin, "preset", 0)),
            )
            # Apply per-channel volume and pan so the export mix matches
            # the live session.
            fs.cc(channel, 7,  int(min(1.0, getattr(plugin, "gain", 1.0)) * 127))
            fs.cc(channel, 10, int((getattr(plugin, "pan", 0.0) + 1.0) / 2.0 * 127))

        if not sfid_map:
            fs.delete()
            self.log_msg.emit(
                "  Warning: no SF2 files loaded — MIDI tracks skipped.")
            return

        # Build a sorted list of (frame, is_on, channel, note, velocity).
        events: List[tuple] = []
        for track in self._midi_tracks:
            for note in track.sorted_notes():
                spb   = 60.0 / self._bpm
                on_f  = int(note.start_beat * spb * EXPORT_SAMPLE_RATE)
                off_f = int((note.start_beat + note.duration) * spb * EXPORT_SAMPLE_RATE)
                events.append((on_f,  True,  track.channel, note.pitch, note.velocity))
                events.append((off_f, False, track.channel, note.pitch, 0))

        for beat, ch, note, vel, is_on in self._step_events:
            frame = int(beat * 60.0 / self._bpm * EXPORT_SAMPLE_RATE)
            events.append((frame, is_on, ch, note, vel))

        events.sort(key=lambda e: e[0])

        # Helper: apply MIDI automation CC7/CC10 at each block boundary.
        def _apply_midi_automation(frame_start):
            block_beat = frame_start / EXPORT_SAMPLE_RATE * (self._bpm / 60.0)
            for ch, chain in self._midi_fx.items():
                vol_env = chain.envelopes.get("volume")
                if vol_env and getattr(vol_env, 'nodes', None):
                    vol = max(0.0, min(1.0, float(vol_env.evaluate(block_beat))))
                    try:
                        fs.cc(ch, 7, int(vol * 127))
                    except Exception:
                        pass
                pan_env = chain.envelopes.get("pan")
                if pan_env and getattr(pan_env, 'nodes', None):
                    pan = max(-1.0, min(1.0, float(pan_env.evaluate(block_beat))))
                    pan_cc = int((pan + 1.0) / 2.0 * 127)
                    try:
                        fs.cc(ch, 10, pan_cc)
                    except Exception:
                        pass

        # Render in blocks of 1024 frames and accumulate into output buffers.
        BLOCK      = 1024
        buf_l      = np.zeros(total_frames, dtype=np.float32)
        buf_r      = np.zeros(total_frames, dtype=np.float32)
        ev_idx     = 0
        n_ev       = len(events)

        for frame_start in range(0, total_frames, BLOCK):
            if self.isInterruptionRequested():
                break
            frame_end  = min(frame_start + BLOCK, total_frames)
            block_size = frame_end - frame_start

            # Apply automation envelopes at the start of each block.
            if self._midi_fx:
                _apply_midi_automation(frame_start)

            # Fire all events whose position falls inside this block.
            while ev_idx < n_ev and events[ev_idx][0] < frame_end:
                _, is_on, ch, pitch, vel = events[ev_idx]
                try:
                    if is_on and vel > 0:
                        fs.noteon(ch, pitch, vel)
                    else:
                        fs.noteoff(ch, pitch)
                except Exception:
                    pass
                ev_idx += 1

            # Ask FluidSynth to render this block.
            # get_samples() returns an interleaved int16 array.
            try:
                raw = fs.get_samples(block_size)
                arr = np.array(raw, dtype=np.int16).reshape(-1, 2)
                actual = min(block_size, len(arr))
                flt    = arr[:actual].astype(np.float32) / 32768.0
                buf_l[frame_start : frame_start + actual] = flt[:, 0]
                buf_r[frame_start : frame_start + actual] = flt[:, 1]
            except Exception as exc:
                logger.warning("FluidSynth get_samples error at frame %d: %s",
                               frame_start, exc)

        fs.delete()

        # Mix the MIDI render into the C++ OfflineExporter at frame 0.
        exporter.mix_in(
            np.ascontiguousarray(buf_l),
            np.ascontiguousarray(buf_r),
            0,
            1.0,
        )
        self.log_msg.emit(
            f"  MIDI render complete ({len(self._midi_tracks)} track(s)).")

    # ── Audio clip mixing ─────────────────────────────────────────────────────

    def _mix_audio_clip(self, exporter, clip, chain) -> None:
        """Decode one audio clip, apply its FX chain, and mix into the bus."""
        try:
            from pedalboard.io import AudioFile

            with AudioFile(clip.path).resampled_to(EXPORT_SAMPLE_RATE) as f:
                # pedalboard returns (channels, samples) float32.
                audio = f.read(f.frames).T.astype(np.float32)

            # Ensure shape is (samples, 2).
            if audio.ndim == 1:
                audio = np.column_stack([audio, audio])
            elif audio.shape[1] == 1:
                audio = np.repeat(audio, 2, axis=1)

            # Apply the track's C++ FX chain (same path as live playback).
            if chain is not None:
                audio = chain.process(audio, EXPORT_SAMPLE_RATE)
                n_ch  = audio.shape[1] if audio.ndim == 2 else 1
                audio = chain.apply_gain_pan(audio, n_ch)

            at_frame = int(clip.start_beat * 60.0 / self._bpm * EXPORT_SAMPLE_RATE)

            # Apply time-varying volume automation envelope if present.
            if chain is not None and chain.envelopes:
                vol_env = chain.envelopes.get("volume")
                if vol_env and getattr(vol_env, 'nodes', None):
                    n_samples = len(audio)
                    BLOCK = 1024
                    gain_curve = np.ones(n_samples, dtype=np.float32)
                    for i in range(0, n_samples, BLOCK):
                        frame_beat = (at_frame + i) / EXPORT_SAMPLE_RATE * (self._bpm / 60.0)
                        g = max(0.0, min(4.0, float(vol_env.evaluate(frame_beat))))
                        gain_curve[i:min(i+BLOCK, n_samples)] = g
                    audio[:, 0] *= gain_curve
                    audio[:, 1] *= gain_curve

            left  = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
            right = np.ascontiguousarray(audio[:, 1], dtype=np.float32)
            exporter.mix_in(left, right, at_frame, 1.0)

        except Exception as exc:
            logger.warning(
                "ExportWorker._mix_audio_clip failed  path=%s  err=%s",
                getattr(clip, "path", "?"), exc,
            )

    # ── Duration calculation ──────────────────────────────────────────────────

    def _calc_total_seconds(self) -> float:
        """Return the wall-clock end time of the last event in the project."""
        spb = 60.0 / self._bpm
        end = 0.0

        # MIDI notes.
        for track in self._midi_tracks:
            for note in track.sorted_notes():
                end = max(end, (note.start_beat + note.duration) * spb)

        # Step sequencer events.
        for beat, *_ in self._step_events:
            end = max(end, beat * spb)

        # Audio clips.
        for atrack in self._audio_tracks:
            for clip in atrack.clips:
                clip_end = clip.start_beat * spb + clip.duration_seconds
                end = max(end, clip_end)

        return max(end, 4.0)   # minimum 4 s so the file is always valid

    # ── ffmpeg encoding (MP3 and AAC) ─────────────────────────────────────────

    def _encode_ffmpeg(
        self,
        wav_path:  str,
        out_path:  str,
        extra_args: List[str],
        label:     str,
    ) -> bool:
        """
        Encode wav_path to out_path using ffmpeg with extra_args inserted
        before the output filename.  Used for both MP3 and AAC encoding.
        """
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            for candidate in (
                "/opt/homebrew/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            ):
                if os.path.isfile(candidate):
                    ffmpeg = candidate
                    break

        if ffmpeg is None:
            self.log_msg.emit(
                f"ffmpeg not found — cannot encode {label}.  "
                "Install ffmpeg and make sure it is on PATH.")
            return False

        self.log_msg.emit(f"Encoding {label} via ffmpeg…")
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", wav_path] + extra_args + [out_path],
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as exc:
            err = exc.stderr.decode(errors="replace") if exc.stderr else "(no output)"
            self.log_msg.emit(f"ffmpeg {label} failed: {err[:300]}")
            return False
