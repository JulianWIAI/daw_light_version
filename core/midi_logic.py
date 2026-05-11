"""
midi_logic.py — SBS-Synth Master  ✦  Sequencer Engine
======================================================
Everything MIDI-related that is NOT synthesis lives here.

Data hierarchy (mirrors professional DAW conventions):
    Project
      └─ MidiTrack  (one per MIDI channel / instrument)
           └─ MidiClip  (a named, draggable region on the timeline)
                └─ MidiNote  (pitch, velocity, duration — relative to clip start)
      └─ AudioTrack  (one per imported audio file)
           └─ AudioClip  (the file's placement on the timeline)

Design principles:
    • MidiNote positions are RELATIVE to their containing MidiClip.start_beat.
      Absolute position = note.start_beat + clip.start_beat.
    • MidiTrack.notes property returns an absolute-position flat list for
      playback and MIDI export — existing callers need no changes.
    • MidiLogic is the single source of truth for sequencing; AudioEngine
      handles synthesis.  They communicate via a note-event callback so
      neither module imports the other.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(order=True)
class MidiNote:
    """
    A single MIDI note stored at a position RELATIVE to its parent MidiClip.

    Attributes:
        start_beat : Beats from the START OF THE CLIP (not the project).
        duration   : Length in beats (quarter note = 1.0).
        pitch      : MIDI note number 0–127 (60 = Middle C / C4).
        velocity   : Strike intensity 1–127.
        channel    : MIDI channel (matches the parent MidiTrack channel).
        note_id    : Globally unique id assigned by MidiLogic.
    """
    start_beat: float
    duration:   float
    pitch:      int
    velocity:   int
    channel:    int
    note_id:    int = field(default=0, compare=False, repr=False)


@dataclass
class MidiClip:
    """
    A named, draggable region on a MidiTrack's timeline.

    Notes inside the clip use RELATIVE beat positions — add clip.start_beat
    to convert to absolute project-time.  This lets the clip be moved or
    duplicated without touching any note data.

    Attributes:
        start_beat : Clip's left edge on the project timeline (beats).
        duration   : Visual length of the clip block (beats).  Auto-expanded
                     when notes extend beyond it.
        notes      : Note list with positions relative to clip start.
        name       : Display label (defaults to parent track name).
        color      : Hex colour; empty string = inherit from parent track.
        clip_id    : Globally unique id assigned by MidiLogic.
    """
    start_beat: float
    duration:   float
    notes:      List[MidiNote] = field(default_factory=list)
    name:       str  = ""
    color:      str  = ""
    clip_id:    int  = field(default=0, compare=False, repr=False)

    # ── Note management ───────────────────────────────────────────────────

    def add_note(self, note: MidiNote) -> None:
        self.notes.append(note)

    def remove_note(self, note_id: int) -> bool:
        for i, n in enumerate(self.notes):
            if n.note_id == note_id:
                self.notes.pop(i)
                return True
        return False

    def sorted_notes(self) -> List[MidiNote]:
        """Notes sorted by relative start_beat — required for playback order."""
        return sorted(self.notes)

    @property
    def end_beat(self) -> float:
        """Absolute beat where this clip ends (accounts for note content)."""
        content_end = (max(n.start_beat + n.duration for n in self.notes)
                       if self.notes else 0.0)
        return self.start_beat + max(self.duration, content_end)

    def absolute_notes(self) -> List[MidiNote]:
        """Return new MidiNote objects with absolute start_beat positions."""
        return [
            MidiNote(
                start_beat=n.start_beat + self.start_beat,
                duration=n.duration,
                pitch=n.pitch,
                velocity=n.velocity,
                channel=n.channel,
                note_id=n.note_id,
            )
            for n in self.notes
        ]


@dataclass
class AudioClip:
    """
    One audio file placed at a position on an AudioTrack's timeline.

    Attributes:
        path             : Absolute file-system path to the audio file.
        start_beat       : Beat position where playback begins.
        name             : Short display label.
        duration_seconds : Actual audio duration (used for visual block width).
        color            : Hex colour for the clip block.
        clip_id          : Globally unique id.
    """
    path:             str
    start_beat:       float
    name:             str   = ""
    duration_seconds: float = 0.0
    color:            str   = "#FFD700"
    clip_id:          int   = field(default=0, compare=False, repr=False)


@dataclass
class AudioTrack:
    """
    One audio file source shown as its own track row in the arrangement.

    Unlike MIDI tracks (keyed by MIDI channel), audio tracks use a
    monotonically-increasing track_id.  Typically one clip lives here, but
    the user can place the same file at multiple positions.

    Attributes:
        name     : Display name (usually the audio file's base name).
        track_id : Unique identifier (assigned by MidiLogic).
        color    : Hex colour for the track header and clip blocks.
        clips    : List of placed AudioClip regions.
    """
    name:     str
    track_id: int
    color:    str             = "#FFD700"
    clips:    List[AudioClip] = field(default_factory=list)


@dataclass
class MidiTrack:
    """
    One sequencer lane corresponding to one AudioEngine instrument channel.

    Notes are stored inside MidiClip regions.  The read-only .notes property
    assembles a flat absolute-position list for playback and export so all
    existing callers continue to work without modification.

    Attributes:
        name    : Display name shown in the mixer and piano roll header.
        channel : MIDI channel (0–15); must match the InstrumentPlugin channel.
        clips   : Ordered list of MidiClip regions on this track's timeline.
        color   : Qt hex colour for track header tinting.
    """
    name:    str
    channel: int
    clips:   List[MidiClip] = field(default_factory=list)
    color:   str = "#4A90D9"

    @property
    def notes(self) -> List[MidiNote]:
        """
        All notes with ABSOLUTE start_beat positions (clips offset applied).

        Returns a NEW list each call — do not mutate the result.
        """
        result: List[MidiNote] = []
        for clip in self.clips:
            result.extend(clip.absolute_notes())
        return result

    def sorted_notes(self) -> List[MidiNote]:
        """Absolute-position notes sorted by start_beat for playback."""
        return sorted(self.notes)

    # ── Convenience helpers ───────────────────────────────────────────────

    def add_note(self, note: MidiNote) -> None:
        """
        Add *note* to the first clip, creating a default clip at beat 0 if
        no clips exist.  The note position is treated as ABSOLUTE and
        adjusted to be relative to the target clip.
        """
        if not self.clips:
            self.clips.append(MidiClip(start_beat=0.0, duration=32.0))
        clip = self.clips[0]
        note.start_beat = max(0.0, note.start_beat - clip.start_beat)
        clip.add_note(note)

    def remove_note(self, note_id: int) -> bool:
        """Search every clip and remove the note with the given id."""
        for clip in self.clips:
            if clip.remove_note(note_id):
                return True
        return False

    def clear(self) -> None:
        """Erase all clips (and therefore all notes)."""
        self.clips.clear()


# ─────────────────────────────────────────────────────────────────────────────
# MidiLogic — the sequencer
# ─────────────────────────────────────────────────────────────────────────────

NoteEventCallback = Callable[[int, int, int, bool], None]
"""Signature: (channel, pitch, velocity, is_note_on) → None."""


def _probe_audio_duration(path: str) -> float:
    """
    Best-effort read of audio file length in seconds.

    Tries (in order): stdlib wave module (WAV only), then soundfile,
    then pedalboard's AudioFile.  Returns 0.0 if all attempts fail.
    """
    import os as _os
    if not _os.path.isfile(path):
        return 0.0
    if path.lower().endswith(".wav"):
        try:
            import wave as _wave
            with _wave.open(path, "rb") as wf:
                return wf.getnframes() / max(1, wf.getframerate())
        except Exception:
            pass
    try:
        import soundfile as _sf
        return _sf.info(path).duration
    except Exception:
        pass
    try:
        from pedalboard.io import AudioFile as _AF
        with _AF(path) as af:
            return af.frames / af.samplerate
    except Exception:
        pass
    return 0.0


class MidiLogic:
    """
    Sequencer and live-recording engine.

    Owns the complete project data model (tracks, clips, notes, audio tracks)
    and drives playback via a background thread that fires note-event callbacks
    at exact wall-clock times.

    MidiLogic never imports AudioEngine — decoupling is maintained through the
    NoteEventCallback that the Controller wires at startup.
    """

    DEFAULT_BPM:            int   = 120
    DEFAULT_TIME_SIGNATURE: tuple = (4, 4)

    def __init__(self) -> None:
        self._bpm:       float = self.DEFAULT_BPM
        self._time_sig:  tuple = self.DEFAULT_TIME_SIGNATURE
        self._tracks:    Dict[int, MidiTrack]  = {}     # channel → MidiTrack
        self._audio_tracks: Dict[int, AudioTrack] = {}  # track_id → AudioTrack
        self._note_callback: Optional[NoteEventCallback] = None

        # Playback state
        self._playback_thread: Optional[threading.Thread] = None
        self._playing       = False
        self._playhead_beat: float = 0.0

        # Recording state
        self._recording         = False
        self._record_start_time: float = 0.0
        self._record_track:      Optional[MidiTrack] = None
        self._record_clip:       Optional[MidiClip]  = None
        self._pending_notes:     Dict[int, float]    = {}   # pitch → start_beat

        # Monotonically-increasing id counter for notes, clips, and audio tracks
        self._id_counter: int = 1

        # Audio clip scheduling
        self._audio_callback:    Optional[Callable[[str], None]] = None
        self._pending_timers:    List[threading.Timer] = []
        self._audio_track_counter: int = 0

        # Loop region
        self._loop_enabled: bool  = False
        self._loop_start:   float = 0.0
        self._loop_end:     float = 8.0

    def _next_id(self) -> int:
        """Return a unique id and advance the counter."""
        uid = self._id_counter
        self._id_counter += 1
        return uid

    # ── Track management ──────────────────────────────────────────────────────

    def add_track(self, track: MidiTrack) -> None:
        """Register a MidiTrack.  Channel must be unique."""
        if track.channel in self._tracks:
            logger.warning("Channel %d already has a track — replacing.", track.channel)
        self._tracks[track.channel] = track
        logger.info("Added MIDI track '%s' on channel %d", track.name, track.channel)

    def remove_track(self, channel: int) -> None:
        self._tracks.pop(channel, None)

    def get_track(self, channel: int) -> Optional[MidiTrack]:
        return self._tracks.get(channel)

    def get_all_tracks(self) -> List[MidiTrack]:
        return sorted(self._tracks.values(), key=lambda t: t.channel)

    # ── Tempo ─────────────────────────────────────────────────────────────────

    @property
    def bpm(self) -> float:
        return self._bpm

    @bpm.setter
    def bpm(self, value: float) -> None:
        self._bpm = max(20.0, min(300.0, value))

    @property
    def seconds_per_beat(self) -> float:
        return 60.0 / self._bpm

    def beat_to_seconds(self, beat: float) -> float:
        return beat * self.seconds_per_beat

    # ── Note callback ─────────────────────────────────────────────────────────

    def set_note_callback(self, callback: NoteEventCallback) -> None:
        self._note_callback = callback

    def _fire_note(self, channel: int, pitch: int, velocity: int, on: bool) -> None:
        if self._note_callback:
            try:
                self._note_callback(channel, pitch, velocity, on)
            except Exception as exc:
                logger.error("Note callback error: %s", exc)

    # ── Clip management ───────────────────────────────────────────────────────

    def create_clip(
        self,
        channel:    int,
        start_beat: float,
        duration:   float  = 8.0,
        name:       str    = "",
    ) -> Optional[MidiClip]:
        """
        Create a new empty MidiClip on a track and return it.

        Args:
            channel:    Target MIDI channel.
            start_beat: Clip position on the timeline.
            duration:   Initial clip length in beats.
            name:       Display name; defaults to the track name.

        Returns:
            The new MidiClip, or None if the channel has no track.
        """
        track = self._tracks.get(channel)
        if track is None:
            return None
        clip = MidiClip(
            start_beat=start_beat,
            duration=duration,
            name=name or track.name,
            clip_id=self._next_id(),
        )
        track.clips.append(clip)
        track.clips.sort(key=lambda c: c.start_beat)
        logger.info("Created clip '%s' at beat %.2f on channel %d",
                    clip.name, start_beat, channel)
        return clip

    def move_clip(self, channel: int, clip_id: int, new_start_beat: float) -> bool:
        """Reposition a clip to *new_start_beat* without touching its notes."""
        track = self._tracks.get(channel)
        if not track:
            return False
        for clip in track.clips:
            if clip.clip_id == clip_id:
                clip.start_beat = max(0.0, new_start_beat)
                track.clips.sort(key=lambda c: c.start_beat)
                return True
        return False

    def resize_clip(self, channel: int, clip_id: int, new_duration: float) -> bool:
        """Set a clip's visual duration (does not erase existing notes)."""
        track = self._tracks.get(channel)
        if not track:
            return False
        for clip in track.clips:
            if clip.clip_id == clip_id:
                # Duration must cover all existing notes
                if clip.notes:
                    min_dur = max(n.start_beat + n.duration
                                  for n in clip.notes) + 0.25
                else:
                    min_dur = 0.25
                clip.duration = max(min_dur, new_duration)
                return True
        return False

    def delete_clip(self, channel: int, clip_id: int) -> bool:
        """Remove a clip (and all its notes) from a track."""
        track = self._tracks.get(channel)
        if not track:
            return False
        for i, clip in enumerate(track.clips):
            if clip.clip_id == clip_id:
                track.clips.pop(i)
                logger.info("Deleted clip id=%d from channel %d", clip_id, channel)
                return True
        return False

    def duplicate_clip(self, channel: int, clip_id: int) -> Optional[MidiClip]:
        """
        Create a copy of a clip placed immediately after the original.

        Returns the new clip, or None if the source clip is not found.
        """
        track = self._tracks.get(channel)
        if not track:
            return None
        src = next((c for c in track.clips if c.clip_id == clip_id), None)
        if src is None:
            return None
        new_clip = MidiClip(
            start_beat=src.end_beat,
            duration=src.duration,
            name=src.name,
            color=src.color,
            clip_id=self._next_id(),
        )
        for n in src.notes:
            new_clip.notes.append(MidiNote(
                start_beat=n.start_beat,
                duration=n.duration,
                pitch=n.pitch,
                velocity=n.velocity,
                channel=n.channel,
                note_id=self._next_id(),
            ))
        track.clips.append(new_clip)
        track.clips.sort(key=lambda c: c.start_beat)
        return new_clip

    # ── Note editing ──────────────────────────────────────────────────────────

    def add_note_to_clip(
        self,
        clip:     MidiClip,
        rel_beat: float,
        duration: float,
        pitch:    int,
        velocity: int = 100,
        channel:  int = 0,
    ) -> MidiNote:
        """
        Add a note to a specific clip at a RELATIVE beat position.

        This is the primary API used by the piano roll.  The note is stored
        at *rel_beat* (relative to clip.start_beat) and the clip's duration
        is auto-expanded if needed.

        Args:
            clip     : Target MidiClip object.
            rel_beat : Beat position relative to clip start (>= 0).
            duration : Note length in beats.
            pitch    : MIDI note number 0–127.
            velocity : Strike intensity 1–127.
            channel  : MIDI channel (should match parent track channel).

        Returns:
            The created MidiNote.
        """
        note = MidiNote(
            start_beat=max(0.0, rel_beat),
            duration=max(0.0625, duration),
            pitch=pitch,
            velocity=velocity,
            channel=channel,
            note_id=self._next_id(),
        )
        clip.add_note(note)
        clip.duration = max(clip.duration, note.start_beat + note.duration + 0.25)
        return note

    def add_note_to_track(
        self,
        channel:    int,
        start_beat: float,
        duration:   float,
        pitch:      int,
        velocity:   int = 100,
    ) -> Optional[MidiNote]:
        """
        Add a note using an ABSOLUTE beat position.

        Finds the clip that contains *start_beat*, or creates one at beat 0
        if the track has no clips yet.  The note is converted to a relative
        position before storage.

        This method preserves backward compatibility with code that passes
        absolute beat positions.
        """
        track = self._tracks.get(channel)
        if track is None:
            return None

        # Find the best clip: prefers the one whose range contains start_beat.
        clip = None
        for c in track.clips:
            if c.start_beat <= start_beat < c.start_beat + c.duration:
                clip = c
                break
        if clip is None and track.clips:
            clip = min(track.clips, key=lambda c: abs(c.start_beat - start_beat))
        if clip is None:
            # Auto-create a clip spanning from beat 0 onwards.
            clip = MidiClip(
                start_beat=0.0,
                duration=max(32.0, start_beat + duration + 4.0),
                name=track.name,
                clip_id=self._next_id(),
            )
            track.clips.append(clip)

        return self.add_note_to_clip(
            clip,
            rel_beat=start_beat - clip.start_beat,
            duration=duration,
            pitch=pitch,
            velocity=velocity,
            channel=channel,
        )

    def remove_note_from_track(self, channel: int, note_id: int) -> bool:
        """Remove a note by id from whichever clip of the track contains it."""
        track = self._tracks.get(channel)
        if track:
            return track.remove_note(note_id)
        return False

    def remove_note_from_clip(self, clip: MidiClip, note_id: int) -> bool:
        """Remove a note from a specific clip by id."""
        return clip.remove_note(note_id)

    def move_note_in_clip(
        self,
        clip:         MidiClip,
        note_id:      int,
        new_rel_beat: float,
        new_pitch:    int,
    ) -> bool:
        """
        Move a note to a new relative position and pitch within its clip.

        Returns True if the note was found and updated.
        """
        for note in clip.notes:
            if note.note_id == note_id:
                note.start_beat = max(0.0, new_rel_beat)
                note.pitch      = max(0, min(127, new_pitch))
                clip.duration   = max(clip.duration,
                                      note.start_beat + note.duration + 0.25)
                return True
        return False

    def set_note_velocity(self, note_id: int, velocity: int) -> bool:
        """Update the velocity of a note anywhere in the project by note_id."""
        for track in self._tracks.values():
            for clip in track.clips:
                for note in clip.notes:
                    if note.note_id == note_id:
                        note.velocity = max(1, min(127, velocity))
                        return True
        return False

    def resize_note_in_clip(
        self,
        clip:         MidiClip,
        note_id:      int,
        new_duration: float,
    ) -> bool:
        """
        Resize a note's duration.

        Returns True if the note was found and updated.
        """
        for note in clip.notes:
            if note.note_id == note_id:
                note.duration = max(0.0625, new_duration)
                clip.duration = max(clip.duration,
                                    note.start_beat + note.duration + 0.25)
                return True
        return False

    # ── Audio track management ────────────────────────────────────────────────

    def add_audio_track(
        self,
        path:             str,
        start_beat:       float,
        name:             str   = "",
        duration_seconds: float = 0.0,
        color:            str   = "#FFD700",
    ) -> AudioTrack:
        """
        Import one audio file as a new dedicated AudioTrack.

        A single AudioClip is placed at *start_beat*.  More clips can be
        added later via add_clip_to_audio_track().

        Returns the newly created AudioTrack.
        """
        track_id = self._audio_track_counter
        self._audio_track_counter += 1

        file_name = name or path.rsplit("/", 1)[-1]
        if duration_seconds <= 0.0:
            duration_seconds = _probe_audio_duration(path)
        atrack = AudioTrack(
            name=file_name,
            track_id=track_id,
            color=color,
        )
        clip = AudioClip(
            path=path,
            start_beat=start_beat,
            name=file_name,
            duration_seconds=duration_seconds,
            color=color,
            clip_id=self._next_id(),
        )
        atrack.clips.append(clip)
        self._audio_tracks[track_id] = atrack
        logger.info("Added audio track '%s' (id=%d) at beat %.2f",
                    file_name, track_id, start_beat)
        return atrack

    def remove_audio_track(self, track_id: int) -> bool:
        if track_id in self._audio_tracks:
            del self._audio_tracks[track_id]
            return True
        return False

    def get_audio_track(self, track_id: int) -> Optional[AudioTrack]:
        return self._audio_tracks.get(track_id)

    def get_audio_tracks(self) -> List[AudioTrack]:
        return sorted(self._audio_tracks.values(), key=lambda t: t.track_id)

    def move_audio_clip(
        self, track_id: int, clip_id: int, new_start_beat: float
    ) -> bool:
        track = self._audio_tracks.get(track_id)
        if not track:
            return False
        for clip in track.clips:
            if clip.clip_id == clip_id:
                clip.start_beat = max(0.0, new_start_beat)
                return True
        return False

    def remove_audio_clip(self, clip_id: int) -> bool:
        """Remove an audio clip by id from whichever track contains it."""
        for atrack in list(self._audio_tracks.values()):
            for i, clip in enumerate(atrack.clips):
                if clip.clip_id == clip_id:
                    atrack.clips.pop(i)
                    if not atrack.clips:          # empty track → remove it
                        del self._audio_tracks[atrack.track_id]
                    return True
        return False

    def duplicate_audio_clip(self, track_id: int, clip_id: int) -> Optional[AudioClip]:
        """
        Copy an audio clip and place it immediately after the original.

        The duplicate lands right after the original clip's end so it is
        immediately visible without overlapping.  Returns the new clip, or
        None if the source clip was not found.
        """
        track = self._audio_tracks.get(track_id)
        if not track:
            return None
        for clip in track.clips:
            if clip.clip_id == clip_id:
                dur_beats = (clip.duration_seconds * self._bpm / 60.0
                             if clip.duration_seconds > 0.0 else 4.0)
                new_clip = AudioClip(
                    path=clip.path,
                    start_beat=clip.start_beat + dur_beats,
                    name=clip.name,
                    duration_seconds=clip.duration_seconds,
                    color=clip.color,
                    clip_id=self._next_id(),
                )
                track.clips.append(new_clip)
                return new_clip
        return None

    def resize_audio_clip(
        self, track_id: int, clip_id: int, new_duration_seconds: float
    ) -> bool:
        """
        Trim an audio clip to a new duration in seconds.

        The duration is clamped to a minimum of 0.1 seconds so clips
        never disappear completely.  Returns True on success.
        """
        track = self._audio_tracks.get(track_id)
        if not track:
            return False
        for clip in track.clips:
            if clip.clip_id == clip_id:
                clip.duration_seconds = max(0.1, new_duration_seconds)
                return True
        return False

    # ── Backward-compatible clip accessors ───────────────────────────────────

    def add_clip(
        self,
        path:             str,
        start_beat:       float,
        name:             str   = "",
        duration_seconds: float = 0.0,
        color:            str   = "#FFD700",
    ) -> AudioClip:
        """Legacy wrapper: add audio file as a new AudioTrack, return the clip."""
        atrack = self.add_audio_track(
            path=path, start_beat=start_beat, name=name,
            duration_seconds=duration_seconds, color=color,
        )
        return atrack.clips[0]

    def remove_clip(self, clip_id: int) -> bool:
        return self.remove_audio_clip(clip_id)

    def get_clips(self) -> List[AudioClip]:
        """Flat list of all AudioClips across all AudioTracks (for display)."""
        return [c for at in self._audio_tracks.values() for c in at.clips]

    def clear_clips(self) -> None:
        self._audio_tracks.clear()

    def set_audio_callback(self, callback: Callable[[str, float], None]) -> None:
        self._audio_callback = callback

    # ── Loop region ───────────────────────────────────────────────────────────

    def set_loop_region(self, enabled: bool, start: float, end: float) -> None:
        self._loop_enabled = enabled
        self._loop_start   = max(0.0, start)
        self._loop_end     = max(self._loop_start + 0.25, end)

    # ── Live recording ────────────────────────────────────────────────────────

    def start_recording(self, channel: int,
                        into_clip: Optional["MidiClip"] = None) -> bool:
        """
        Arm a track for live recording.

        If *into_clip* is provided, notes are appended to that existing clip
        (piano-roll recording mode). Otherwise a new clip is created at the
        current playhead position (arrangement recording mode).
        """
        track = self._tracks.get(channel)
        if track is None:
            logger.warning("Cannot record — no track on channel %d", channel)
            return False

        self._record_track = track
        self._recording    = True
        self._record_start_time = time.perf_counter()
        self._pending_notes.clear()

        if into_clip is not None:
            # Record into the existing clip — don't create a new one.
            self._record_clip = into_clip
            logger.info("Recording into clip '%s' on track '%s'",
                        into_clip.name, track.name)
        else:
            # Create a recording clip starting at the current playhead.
            rec_clip = MidiClip(
                start_beat=self._playhead_beat,
                duration=32.0,
                name=f"{track.name} (rec)",
                clip_id=self._next_id(),
            )
            track.clips.append(rec_clip)
            track.clips.sort(key=lambda c: c.start_beat)
            self._record_clip = rec_clip
            logger.info("Recording started on track '%s'", track.name)

        return True

    @property
    def last_record_clip(self) -> Optional["MidiClip"]:
        """The clip that received (or is receiving) notes from the last recording."""
        return self._record_clip

    def stop_recording(self) -> None:
        """Stop live recording; finalise any notes still held."""
        self._recording = False
        current_beat = self._elapsed_beats()

        for pitch, start_beat in list(self._pending_notes.items()):
            note = MidiNote(
                start_beat=start_beat,
                duration=max(0.0625, current_beat - start_beat),
                pitch=pitch,
                velocity=100,
                channel=(self._record_track.channel
                         if self._record_track else 0),
                note_id=self._next_id(),
            )
            if self._record_clip:
                self._record_clip.add_note(note)

        self._pending_notes.clear()
        if self._record_clip and self._record_clip.notes:
            last = max(n.start_beat + n.duration for n in self._record_clip.notes)
            self._record_clip.duration = last + 0.25

        logger.info("Recording stopped.")

    def record_note_on(self, pitch: int, velocity: int) -> None:
        if not self._recording:
            return
        # Store the exact beat this note started
        self._pending_notes[pitch] = self._elapsed_beats()

    def record_note_off(self, pitch: int) -> None:
        if not self._recording or pitch not in self._pending_notes:
            return
        start_beat = self._pending_notes.pop(pitch)
        duration   = max(0.0625, self._elapsed_beats() - start_beat)
        note = MidiNote(
            start_beat=start_beat,
            duration=duration,
            pitch=pitch,
            velocity=100,
            channel=(self._record_track.channel if self._record_track else 0),
            note_id=self._next_id(),
        )
        if self._record_clip:
            self._record_clip.add_note(note)

    def _elapsed_beats(self) -> float:
        # If project is playing, use playhead. If stopped, use time since record hit.
        if self._playing:
            return self._playhead_beat
        return (time.perf_counter() - self._record_start_time) / self.seconds_per_beat

    # ── Playback ──────────────────────────────────────────────────────────────

    def play(self, from_beat: float = 0.0) -> None:
        """Start playback from *from_beat* in a background thread."""
        if self._playing:
            self.stop()
        self._playhead_beat = from_beat
        self._playing       = True
        self._playback_thread = threading.Thread(
            target=self._playback_loop,
            daemon=True,
            name="SequencerPlayback",
        )
        self._playback_thread.start()
        logger.info("Playback started from beat %.2f", from_beat)

    def stop(self) -> None:
        """Stop playback, cancel audio timers, and silence all notes."""
        self._playing = False
        self._cancel_pending_timers()
        if self._playback_thread:
            self._playback_thread.join(timeout=1.0)
            self._playback_thread = None
        for track in self._tracks.values():
            for pitch in range(128):
                self._fire_note(track.channel, pitch, 0, False)
        logger.info("Playback stopped.")

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def playhead_beat(self) -> float:
        return self._playhead_beat

    def _cancel_pending_timers(self) -> None:
        for t in self._pending_timers:
            t.cancel()
        self._pending_timers.clear()

    def _schedule_audio_clips(
        self, loop_start: float, iter_start_wall: float, loop_end: float
    ) -> None:
        """Fire the audio callback for every clip that falls in [loop_start, loop_end)."""
        if not self._audio_callback:
            return
        for atrack in self._audio_tracks.values():
            for clip in atrack.clips:
                if clip.start_beat < loop_start or clip.start_beat >= loop_end:
                    continue
                delay = self.beat_to_seconds(clip.start_beat - loop_start)
                t = threading.Timer(delay, self._audio_callback,
                                    args=[clip.path, clip.duration_seconds])
                t.daemon = True
                t.start()
                self._pending_timers.append(t)

    def _build_flat_events(self) -> List[Tuple]:
        """
        Collect all note-on / note-off events from all tracks and clips,
        sorted by absolute beat position.

        MidiTrack.notes returns absolute positions, so this method is
        unchanged from the pre-clip architecture.
        """
        events: List[Tuple] = []
        for track in self._tracks.values():
            for note in track.sorted_notes():
                events.append(
                    (note.start_beat, track.channel, note.pitch, note.velocity, True))
                events.append(
                    (note.start_beat + note.duration, track.channel, note.pitch, 0, False))
        events.sort(key=lambda e: e[0])
        return events

    def _playback_loop(self) -> None:
        """
        Background thread: fires note events and audio clips in precise time.

        Normal mode: plays once from the current beat to the last event.
        Loop mode:   repeats the defined region until stop() is called.
        """
        play_from = self._playhead_beat

        while self._playing:
            all_events = self._build_flat_events()
            has_audio  = any(at.clips for at in self._audio_tracks.values())

            if not all_events and not has_audio and not self._loop_enabled:
                break

            if self._loop_enabled:
                loop_start = self._loop_start
                loop_end   = self._loop_end
            else:
                loop_start = play_from
                loop_end   = (all_events[-1][0] + 0.25
                              if all_events else play_from + 4.0)

            iter_start_wall = time.perf_counter()
            self._schedule_audio_clips(loop_start, iter_start_wall, loop_end)

            for beat, channel, pitch, velocity, is_on in all_events:
                if not self._playing:
                    break
                if beat < loop_start:
                    continue
                if beat >= loop_end:
                    break

                target_wall = iter_start_wall + self.beat_to_seconds(beat - loop_start)
                sleep_time  = target_wall - time.perf_counter()
                if sleep_time > 0:
                    time.sleep(sleep_time)

                if not self._playing:
                    break

                self._playhead_beat = beat
                self._fire_note(channel, pitch, velocity, is_on)

            if not self._playing:
                break
            if not self._loop_enabled:
                break

            # Wait until the exact loop boundary, then restart.
            loop_end_wall = (iter_start_wall
                             + self.beat_to_seconds(loop_end - loop_start))
            wait = loop_end_wall - time.perf_counter()
            if wait > 0 and self._playing:
                time.sleep(wait)

            if not self._playing:
                break

            # Silence all notes before looping to prevent stuck notes.
            seen: set = set()
            for track in self._tracks.values():
                for note in track.notes:
                    key = (track.channel, note.pitch)
                    if key not in seen:
                        seen.add(key)
                        self._fire_note(track.channel, note.pitch, 0, False)

            self._cancel_pending_timers()
            self._playhead_beat = loop_start
            play_from           = loop_start

        self._playing = False
        logger.info("Playback loop finished.")

    # ── MIDI file export / import ─────────────────────────────────────────────

    def export_to_midi_file(
        self,
        filepath: str,
        instruments: Optional[Dict[int, tuple]] = None,
    ) -> bool:
        """
        Save all tracks to a Standard MIDI File (Format 1 / multi-track).

        MidiTrack.notes returns absolute positions, so the export logic is
        identical to the flat-note architecture.

        Args:
            filepath:    Output .mid path.
            instruments: Optional {channel: (bank, preset)} mapping.  When
                         provided, bank-select + program-change messages are
                         inserted at tick 0 of each track so the rendered audio
                         uses the same instrument as the live session.
        """
        try:
            import mido
        except ImportError:
            logger.error("mido not installed — cannot export MIDI.")
            return False

        TICKS_PER_BEAT = 480
        try:
            mid = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)

            tempo_track = mido.MidiTrack()
            mid.tracks.append(tempo_track)
            tempo_track.append(
                mido.MetaMessage("set_tempo",
                                 tempo=mido.bpm2tempo(self._bpm), time=0))
            tempo_track.append(
                mido.MetaMessage("time_signature",
                                 numerator=4, denominator=4, time=0))
            tempo_track.append(mido.MetaMessage("end_of_track", time=0))

            for track in self.get_all_tracks():
                mtrack = mido.MidiTrack()
                mid.tracks.append(mtrack)
                mtrack.append(
                    mido.MetaMessage("track_name", name=track.name, time=0))

                ch = track.channel
                # Instrument setup at tick 0
                if instruments and ch in instruments:
                    bank, preset = instruments[ch]
                    mtrack.append(mido.Message(
                        'control_change', channel=ch, control=0,
                        value=bank >> 7, time=0))        # bank select MSB
                    mtrack.append(mido.Message(
                        'control_change', channel=ch, control=32,
                        value=bank & 0x7F, time=0))      # bank select LSB
                    mtrack.append(mido.Message(
                        'program_change', channel=ch,
                        program=preset & 0x7F, time=0))

                # Kill reverb/chorus sends so the CLI render matches the DAW
                mtrack.append(mido.Message(
                    'control_change', channel=ch, control=91, value=0, time=0))
                mtrack.append(mido.Message(
                    'control_change', channel=ch, control=93, value=0, time=0))

                raw: List[tuple] = []
                for note in track.notes:    # absolute positions via property
                    start_tick = int(note.start_beat * TICKS_PER_BEAT)
                    end_tick   = int((note.start_beat + note.duration) * TICKS_PER_BEAT)
                    raw.append(
                        (start_tick, "note_on",  ch, note.pitch, note.velocity))
                    raw.append(
                        (end_tick,   "note_off", ch, note.pitch, 0))
                raw.sort(key=lambda e: e[0])

                current_tick = 0
                for abs_tick, msg_type, channel, pitch, velocity in raw:
                    delta = abs_tick - current_tick
                    current_tick = abs_tick
                    mtrack.append(
                        mido.Message(msg_type, channel=channel,
                                     note=pitch, velocity=velocity, time=delta))
                mtrack.append(mido.MetaMessage("end_of_track", time=0))

            mid.save(filepath)
            logger.info("MIDI exported → %s  (%d tracks)",
                        filepath, len(mid.tracks) - 1)
            return True

        except Exception as exc:
            logger.error("MIDI export failed: %s", exc)
            return False

    def import_from_midi_file(self, filepath: str) -> bool:
        """
        Load a Standard MIDI File (Format 0 or 1) and rebuild all tracks.

        Each MIDI track is wrapped in a single MidiClip starting at beat 0.
        Notes are stored RELATIVE to clip start (= absolute, since clip
        starts at beat 0).
        """
        try:
            import mido
        except ImportError:
            logger.error("mido not installed — cannot import MIDI.")
            return False

        try:
            mid = mido.MidiFile(filepath)
            tpb = mid.ticks_per_beat

            # Read tempo from the first set_tempo message
            new_bpm = 120.0
            for mtrack in mid.tracks:
                for msg in mtrack:
                    if msg.type == "set_tempo":
                        new_bpm = mido.tempo2bpm(msg.tempo)
                        break
                else:
                    continue
                break
            self.bpm = new_bpm

            self._tracks.clear()

            for track_index, mtrack in enumerate(mid.tracks):
                track_name = f"Track {track_index + 1}"
                channel    = track_index % 16

                for msg in mtrack:
                    if msg.type == "track_name":
                        track_name = msg.name
                    if hasattr(msg, "channel"):
                        channel = msg.channel
                        break

                has_notes = any(
                    msg.type in ("note_on", "note_off") for msg in mtrack)
                if not has_notes:
                    continue

                # Resolve channel collision in multi-track Format 1 files
                if channel in self._tracks:
                    free = next(
                        (c for c in range(16) if c not in self._tracks),
                        track_index % 16,
                    )
                    channel = free

                seq_track = MidiTrack(name=track_name, channel=channel)
                self.add_track(seq_track)

                # One clip per imported MIDI track, starting at beat 0
                import_clip = MidiClip(
                    start_beat=0.0,
                    duration=32.0,
                    name=track_name,
                    clip_id=self._next_id(),
                )
                seq_track.clips.append(import_clip)

                # Parse delta-time → note events
                current_tick = 0
                pending: Dict[int, tuple] = {}  # pitch → (start_tick, velocity)

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
                                start_beat=start_beat,  # relative to clip (= absolute here)
                                duration=duration,
                                pitch=msg.note,
                                velocity=velocity,
                                channel=channel,
                                note_id=self._next_id(),
                            )
                            import_clip.notes.append(note)

                # Expand clip duration to cover all imported notes
                if import_clip.notes:
                    last = max(n.start_beat + n.duration
                               for n in import_clip.notes)
                    import_clip.duration = last + 0.25

            logger.info(
                "MIDI imported ← %s  (%.1f BPM, %d tracks)",
                filepath, self._bpm, len(self._tracks))
            return True

        except Exception as exc:
            logger.error("MIDI import failed: %s", exc)
            return False