"""
import_manager.py -- Multi-File MIDI and Audio Import
======================================================
Centralises all file-import logic so the GUI stays thin.

Responsibilities:
    - Detect whether a dropped or selected file is MIDI or audio.
    - Import one or many MIDI files into a MidiLogic instance, either
      replacing the current project (replace mode) or adding tracks to it
      (append mode).
    - Import one or many audio files into a MidiLogic instance, creating
      one AudioTrack per file.

MIDI append mode:
    Each MIDI file's tracks are added to the existing project without
    touching existing tracks. Channel collisions are resolved by assigning
    the first free channel (0-15). If all 16 channels are occupied the
    extra tracks are silently skipped (logged as warnings).

Design note:
    ImportManager never touches the AudioEngine or GUI -- those are the
    caller's responsibility. It returns the newly created MidiTrack and
    AudioTrack objects so the caller can wire them up.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .midi_logic import MidiLogic, MidiTrack, AudioTrack

logger = logging.getLogger(__name__)

# File-extension sets used for type detection.
MIDI_EXTENSIONS  = {".mid", ".midi"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".aiff", ".aif", ".m4a"}


def detect_file_type(path: str) -> Optional[str]:
    """
    Return 'midi', 'audio', or None based on the file extension.

    Args:
        path: Absolute or relative path to the file.

    Returns:
        'midi'  if the file is a Standard MIDI File.
        'audio' if the file is a supported audio format.
        None    if the extension is not recognised.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in MIDI_EXTENSIONS:
        return "midi"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return None


class ImportManager:
    """
    Stateless helper that imports files into a MidiLogic project.

    All methods return the newly created objects so callers can update
    their UIs, register instruments, or wire callbacks accordingly.
    """

    # -------------------------------------------------------------------------
    # Mixed-type import (called by drag-drop handler)
    # -------------------------------------------------------------------------

    def import_files(
        self,
        paths:      List[str],
        midi_logic,
        start_beat: float = 0.0,
        append:     bool  = True,
    ) -> Tuple[List, List]:
        """
        Import a mixed list of MIDI and audio files.

        Each file is routed to import_midi_files() or import_audio_files()
        based on its extension. Unrecognised extensions are skipped.

        Args:
            paths      : List of absolute file paths (may be mixed types).
            midi_logic : The project's MidiLogic instance.
            start_beat : Beat position for audio clips (MIDI clips always
                         start at beat 0 inside their imported clip).
            append     : When True, MIDI tracks are added without clearing the
                         project. When False, the project is cleared first.

        Returns:
            (midi_tracks, audio_tracks) -- lists of newly created objects.
        """
        midi_paths  = [p for p in paths if detect_file_type(p) == "midi"]
        audio_paths = [p for p in paths if detect_file_type(p) == "audio"]
        skipped     = [p for p in paths if detect_file_type(p) is None]

        if skipped:
            logger.warning(
                "ImportManager: skipped %d unrecognised file(s): %s",
                len(skipped), skipped,
            )

        midi_tracks  = self.import_midi_files(midi_paths, midi_logic, append=append)
        audio_tracks = self.import_audio_files(audio_paths, midi_logic,
                                               start_beat=start_beat)

        return midi_tracks, audio_tracks

    # -------------------------------------------------------------------------
    # MIDI import
    # -------------------------------------------------------------------------

    def import_midi_files(
        self,
        paths:      List[str],
        midi_logic,
        append:     bool = True,
    ) -> List:
        """
        Import one or more MIDI files into the project.

        Args:
            paths      : MIDI file paths to import.
            midi_logic : MidiLogic instance that owns the track registry.
            append     : True  -- add tracks to existing project.
                         False -- clear existing project first, then import.

        Returns:
            Flat list of all newly created MidiTrack objects (across all files).
        """
        if not paths:
            return []

        # In replace mode, clear only once before importing the first file.
        if not append:
            midi_logic.clear_project()

        all_new_tracks: List = []
        for path in paths:
            if not os.path.isfile(path):
                logger.warning("ImportManager: MIDI file not found: %s", path)
                continue
            new_tracks = self._import_single_midi(path, midi_logic)
            all_new_tracks.extend(new_tracks)
            logger.info(
                "ImportManager: imported MIDI '%s' -> %d track(s)",
                os.path.basename(path), len(new_tracks),
            )

        return all_new_tracks

    def _import_single_midi(self, filepath: str, midi_logic) -> List:
        """
        Parse one MIDI file and append its tracks to midi_logic.

        This is the append-mode variant of MidiLogic.import_from_midi_file().
        It does NOT call self._tracks.clear() and resolves channel collisions
        against the FULL existing track registry (not just the tracks in this
        file).

        Returns a list of the newly created MidiTrack objects.
        """
        try:
            import mido
        except ImportError:
            logger.error("mido not installed -- cannot import MIDI")
            return []

        from .midi_logic import MidiTrack, MidiClip, MidiNote

        new_tracks: List[MidiTrack] = []

        try:
            mid = mido.MidiFile(filepath)
            tpb = mid.ticks_per_beat

            # Read the first tempo found in the file.
            file_bpm = 120.0
            for mtrack in mid.tracks:
                for msg in mtrack:
                    if msg.type == "set_tempo":
                        file_bpm = mido.tempo2bpm(msg.tempo)
                        break
                else:
                    continue
                break

            # If the project has no tracks yet, adopt this file's tempo.
            if not midi_logic.get_all_tracks():
                midi_logic.bpm = file_bpm

            for track_index, mtrack in enumerate(mid.tracks):
                # Determine the display name for this track.
                track_name = f"Track {track_index + 1}"
                channel    = track_index % 16

                for msg in mtrack:
                    if msg.type == "track_name" and msg.name.strip():
                        track_name = msg.name.strip()
                    if hasattr(msg, "channel"):
                        channel = msg.channel
                        break

                # Skip meta-only tracks (no note events).
                has_notes = any(
                    msg.type in ("note_on", "note_off") for msg in mtrack
                )
                if not has_notes:
                    continue

                # Resolve channel collision against ALL currently registered
                # tracks (existing + those added in this import batch).
                used_channels = set(midi_logic._tracks.keys())
                used_channels |= {t.channel for t in new_tracks}

                if channel in used_channels:
                    free = next(
                        (c for c in range(16) if c not in used_channels), None
                    )
                    if free is None:
                        logger.warning(
                            "ImportManager: all 16 MIDI channels occupied -- "
                            "skipping track '%s' from %s",
                            track_name, os.path.basename(filepath),
                        )
                        continue
                    channel = free

                seq_track = MidiTrack(name=track_name, channel=channel)
                midi_logic.add_track(seq_track)
                new_tracks.append(seq_track)

                # One clip per imported MIDI track, starting at beat 0.
                import_clip = MidiClip(
                    start_beat=0.0,
                    duration=32.0,
                    name=track_name,
                    clip_id=midi_logic._next_id(),
                )
                seq_track.clips.append(import_clip)

                # Parse delta-time events into MidiNote objects (relative to clip).
                current_tick = 0
                pending = {}  # pitch -> (start_tick, velocity)

                for msg in mtrack:
                    current_tick += msg.time
                    if not hasattr(msg, "channel"):
                        continue
                    if msg.type == "note_on" and msg.velocity > 0:
                        pending[msg.note] = (current_tick, msg.velocity)
                    elif msg.type == "note_off" or (
                        msg.type == "note_on" and msg.velocity == 0
                    ):
                        if msg.note in pending:
                            start_tick, velocity = pending.pop(msg.note)
                            start_beat = start_tick / tpb
                            duration   = max(0.0625, (current_tick - start_tick) / tpb)
                            note = MidiNote(
                                start_beat=start_beat,
                                duration=duration,
                                pitch=msg.note,
                                velocity=velocity,
                                channel=channel,
                                note_id=midi_logic._next_id(),
                            )
                            import_clip.notes.append(note)

                # Expand clip to cover all imported notes.
                if import_clip.notes:
                    last = max(n.start_beat + n.duration for n in import_clip.notes)
                    import_clip.duration = last + 0.25

        except Exception as exc:
            logger.error("ImportManager: MIDI parse error for %s -- %s", filepath, exc)

        return new_tracks

    # -------------------------------------------------------------------------
    # Audio import
    # -------------------------------------------------------------------------

    def import_audio_files(
        self,
        paths:      List[str],
        midi_logic,
        start_beat: float = 0.0,
    ) -> List:
        """
        Import one or more audio files, creating one AudioTrack per file.

        Each file is placed at start_beat on its own track. Tracks are stacked
        vertically (their start_beat is the same; they are visually separate
        rows in the arrangement view).

        Args:
            paths      : Audio file paths.
            midi_logic : MidiLogic instance.
            start_beat : Timeline position for the first sample of each clip.

        Returns:
            List of newly created AudioTrack objects.
        """
        new_audio_tracks = []
        for path in paths:
            if not os.path.isfile(path):
                logger.warning("ImportManager: audio file not found: %s", path)
                continue
            atrack = midi_logic.add_audio_track(
                path=path,
                start_beat=start_beat,
            )
            new_audio_tracks.append(atrack)
            logger.info(
                "ImportManager: imported audio '%s' -> track_id=%d",
                os.path.basename(path), atrack.track_id,
            )

        return new_audio_tracks
