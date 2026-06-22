"""
midi_drop_importer.py — Async multi-track MIDI drag-and-drop ingestion
======================================================================

Parses a standard .mid file on a background thread so the Qt GUI never
blocks, maps each track's GM program ID to a default SFZ instrument
template, and dispatches the assembled payloads to the C++ TimelineEngine
via the existing pybind11 bridge.

Public entry point::

    importer = MidiDropImporter(bridge)
    importer.import_file(
        "/path/to/song.mid",
        on_complete=lambda payloads: print(f"loaded {len(payloads)} tracks"),
        on_error=lambda err: show_error_dialog(err),
    )

Thread model
------------
- ``import_file()``  — called from the Qt GUI thread; returns immediately.
- ``_worker()``      — runs on a daemon thread; never touches Qt objects.
- ``on_complete``/``on_error`` — fired from the worker thread.  Callers that
  need to update GUI widgets must marshal back to the main thread via a Qt
  signal, ``QMetaObject.invokeMethod``, or a ``QTimer.singleShot(0, ...)``
  call.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .gm_defaults_manager import GmDefaultsManager

# Absolute path to the application root (the folder that contains main.py).
# All relative SFZ paths are resolved against this directory.
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

# MIDI channel 10 is 0-indexed as channel 9.
_DRUMS_CHANNEL        = 9
_DRUMS_INSTRUMENT_ID  = 128   # sentinel value: drums are not a GM program


# ─────────────────────────────────────────────────────────────────────────────
# Data models (Python-side mirror of the C++ structs in MidiDropImporter.h)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MidiNoteEventPy:
    """
    One raw MIDI note event with an absolute tick position.

    The tick → sample-frame conversion is performed later in
    ``_dispatch_to_bridge()`` using the file's own BPM + the engine's
    sample rate.  Keeping ticks here lets callers inspect the raw
    timing before dispatch.
    """
    abs_tick:  int     # cumulative ticks from the track start
    msg_type:  int     # 0x90 = note-on, 0x80 = note-off
    note:      int     # MIDI note number 0-127
    velocity:  int     # 0-127 (note-off always 0)
    channel:   int     # MIDI channel 0-15


@dataclass
class MidiTrackPayloadPy:
    """
    All the data needed to create one instrument track in the C++ engine.

    Python assembles this during the parse phase.  ``_dispatch_to_bridge()``
    converts it to a ``daw_processors.MidiTrackPayload`` C++ struct.
    """
    name:           str
    track_index:    int
    gm_program_id:  int              # 0-127 (melodic) or 128 (drums)
    sfz_path:       str              # relative path to the default SFZ template
    events:         List[MidiNoteEventPy] = field(default_factory=list)
    ticks_per_beat: int   = 480
    bpm:            float = 120.0


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_sfz_path(path: str) -> str:
    """
    Return an absolute path.

    If *path* is already absolute (user override stored in gm_defaults.json)
    it is returned unchanged.  Relative paths (built-in defaults) are
    anchored at the app root directory next to ``main.py``.
    """
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(_APP_ROOT, path))


# ─────────────────────────────────────────────────────────────────────────────
# Synchronous MIDI parser  (always runs on a background thread)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_midi_file(
    path: str,
    overrides: Optional[Dict[str, str]] = None,
) -> Optional[tuple]:
    """
    Open and parse a Standard MIDI File (Format 0 or Format 1).

    Returns ``(bpm: float, payloads: List[MidiTrackPayloadPy])`` on success,
    or ``None`` on any parse error.

    Design notes
    ------------
    - ``mido.MidiFile`` decodes delta-times into ticks automatically.
    - We accumulate a running ``abs_tick`` counter per track; this is simpler
      and less error-prone than relying on ``mido.tick2second``.
    - Note-on with velocity=0 is normalised to a note-off here so the C++
      engine never has to handle that MIDI quirk.
    - Channel-9 drum detection happens in two passes:
        1. Check for a ``program_change`` on channel 9.
        2. Check for any note event on channel 9 (some files omit the PC).
    """
    try:
        import mido
    except ImportError:
        logger.error("midi_drop_importer: 'mido' is not installed — "
                     "run  pip install mido  to enable MIDI import.")
        return None

    try:
        mid = mido.MidiFile(path)
    except Exception as exc:
        logger.error("midi_drop_importer: cannot open '%s': %s", path, exc)
        return None

    tpb = mid.ticks_per_beat

    # ── Global BPM from the first set_tempo meta message ─────────────────────
    bpm = 120.0
    for mtrack in mid.tracks:
        for msg in mtrack:
            if msg.type == "set_tempo":
                bpm = mido.tempo2bpm(msg.tempo)
                break
        else:
            continue
        break  # stop after the first tempo event found in any track

    # ── Per-track parsing ─────────────────────────────────────────────────────
    payloads: List[MidiTrackPayloadPy] = []

    for track_index, mtrack in enumerate(mid.tracks):

        # 1. Extract track name from the first track_name meta message.
        track_name = f"Track {track_index + 1}"
        for msg in mtrack:
            if msg.type == "track_name" and msg.name.strip():
                track_name = msg.name.strip()
                break

        # Skip pure meta tracks (tempo maps, time-signature tracks, etc.)
        has_notes = any(msg.type in ("note_on", "note_off") for msg in mtrack)
        if not has_notes:
            continue

        # 2. GM Program ID — single-pass scan that sets a safe initial state and
        #    lets channel-9 evidence override the program_change result.
        #
        #    Priority (highest wins):
        #      a) ANY note_on / note_off on channel 9  → drums (128)
        #      b) program_change on channel 9           → drums (128)
        #      c) first program_change on another ch    → that GM ID
        #      d) no program_change at all              → piano (0)
        gm_program_id = 0       # Piano — guaranteed fallback for naked tracks
        forced_drums  = False
        first_pc      = None    # first non-drum program_change seen

        for msg in mtrack:
            if msg.type == "program_change":
                if msg.channel == _DRUMS_CHANNEL:
                    forced_drums = True          # ch-9 PC always means drums
                elif first_pc is None:
                    first_pc = msg.program       # record but keep scanning
            elif (hasattr(msg, "channel")
                  and msg.channel == _DRUMS_CHANNEL
                  and msg.type in ("note_on", "note_off")):
                forced_drums = True              # any ch-9 note overrides PC

        if forced_drums:
            gm_program_id = _DRUMS_INSTRUMENT_ID
        elif first_pc is not None:
            gm_program_id = first_pc
        # else: gm_program_id stays 0 (Piano)

        # Resolve instrument path: user override first, built-in default fallback.
        sfz_path = GmDefaultsManager().get_sfz_path(gm_program_id, overrides)

        # 4. Collect all note-on/note-off events with absolute tick counts.
        events:   List[MidiNoteEventPy] = []
        abs_tick: int                   = 0

        for msg in mtrack:
            abs_tick += msg.time           # delta → cumulative

            if not hasattr(msg, "channel"):
                continue                  # skip meta messages with no channel

            if msg.type == "note_on" and msg.velocity > 0:
                events.append(MidiNoteEventPy(
                    abs_tick=abs_tick,
                    msg_type=0x90,
                    note=msg.note,
                    velocity=msg.velocity,
                    channel=msg.channel,
                ))

            elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0
            ):
                events.append(MidiNoteEventPy(
                    abs_tick=abs_tick,
                    msg_type=0x80,
                    note=msg.note,
                    velocity=0,
                    channel=msg.channel,
                ))

        if not events:
            continue

        payloads.append(MidiTrackPayloadPy(
            name=track_name,
            track_index=track_index,
            gm_program_id=gm_program_id,
            sfz_path=sfz_path,
            events=events,
            ticks_per_beat=tpb,
            bpm=bpm,
        ))

    logger.info(
        "midi_drop_importer: parsed '%s' → %.1f BPM, %d note tracks",
        path, bpm, len(payloads),
    )
    return (bpm, payloads)


# ─────────────────────────────────────────────────────────────────────────────
# Public class
# ─────────────────────────────────────────────────────────────────────────────

class MidiDropImporter:
    """
    Async MIDI drag-and-drop importer.

    One instance per DAW session is sufficient — it serialises concurrent
    import requests so only one file is in flight at a time.
    """

    def __init__(self, bridge) -> None:
        """
        Args:
            bridge: ``TimelineEngineBridge`` instance.  Used to push parsed
                    track data to the C++ engine once parsing is complete.
        """
        self._bridge = bridge
        self._lock   = threading.Lock()
        self._active: Optional[threading.Thread] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def import_file(
        self,
        path:        str,
        on_complete: Optional[Callable[[List[MidiTrackPayloadPy]], None]] = None,
        on_error:    Optional[Callable[[str], None]]                      = None,
    ) -> None:
        """
        Start an asynchronous import of a ``.mid`` file.

        Returns immediately — parsing runs on a daemon thread.  Only one
        import can be active at a time; a second call while the first is
        running logs a warning and is silently dropped.

        Args:
            path:        Absolute path to the .mid file.
            on_complete: Called with the parsed payload list on success.
                         Invoked from the worker thread — marshal to the main
                         thread before touching any Qt widgets.
            on_error:    Called with a human-readable string on failure.
        """
        with self._lock:
            if self._active and self._active.is_alive():
                logger.warning(
                    "midi_drop_importer: import already in progress; "
                    "dropping request for '%s'.", path,
                )
                return

            # Join the previously finished thread to release its OS resources.
            if self._active:
                self._active.join(timeout=0)

            self._active = threading.Thread(
                target=self._worker,
                args=(path, on_complete, on_error),
                daemon=True,
                name="MidiDropImporter",
            )
            self._active.start()

    @property
    def is_importing(self) -> bool:
        """True while the background parse thread is still running."""
        return bool(self._active and self._active.is_alive())

    # ── Worker (background thread) ─────────────────────────────────────────────

    def _worker(
        self,
        path:        str,
        on_complete: Optional[Callable],
        on_error:    Optional[Callable],
    ) -> None:
        """Background thread body — never called directly."""
        try:
            # Load user overrides once per import (tiny JSON read, off GUI thread).
            # GmDefaultsManager.get_sfz_path() returns absolute paths directly,
            # so no further resolution is needed after _parse_midi_file returns.
            overrides = GmDefaultsManager().load()

            result = _parse_midi_file(path, overrides)

            if result is None:
                _safe_call(on_error, f"Failed to parse MIDI file: {path}")
                return

            bpm, payloads = result

            if not payloads:
                _safe_call(on_error, f"No playable tracks found in: {path}")
                return

            # Push parsed tracks to C++ before notifying the GUI.
            self._dispatch_to_bridge(bpm, payloads)

            _safe_call(on_complete, payloads)

        except Exception as exc:
            logger.exception("midi_drop_importer: unhandled exception: %s", exc)
            _safe_call(on_error, str(exc))

    # ── Bridge dispatch ────────────────────────────────────────────────────────

    def _dispatch_to_bridge(
        self,
        bpm:      float,
        payloads: List[MidiTrackPayloadPy],
    ) -> None:
        """
        Convert parsed Python payloads to C++ structs and pass them to
        ``TimelineEngine.importMultiTrackMidi()`` via pybind11.

        Tick → sample-frame conversion uses the file's own BPM so playback
        matches the original song tempo.

        Falls back to the existing per-event bridge API (``add_midi_event()``)
        when the newer ``importMultiTrackMidi()`` binding is unavailable — this
        keeps older compiled .pyd files working without a recompile.
        """
        bridge = self._bridge
        if bridge is None or not bridge.is_available:
            logger.warning(
                "midi_drop_importer: C++ bridge unavailable; "
                "skipping engine dispatch."
            )
            return

        # Align the engine's BPM with the imported file's tempo.
        bridge.set_bpm(bpm)

        sample_rate = bridge._sample_rate
        spb         = 60.0 / bpm                          # seconds per beat
        tpb         = payloads[0].ticks_per_beat if payloads else 480

        # ── Fast path: importMultiTrackMidi() (C++ async, batch) ──────────────
        try:
            dp = bridge._daw_processors
            if dp is None or not hasattr(dp, "MidiTrackPayload"):
                raise AttributeError("MidiTrackPayload binding not available")

            cpp_payloads = []
            for py_p in payloads:
                cpp_p               = dp.MidiTrackPayload()
                cpp_p.name          = py_p.name
                cpp_p.track_index   = py_p.track_index
                cpp_p.gm_program_id = py_p.gm_program_id
                cpp_p.sfz_path      = py_p.sfz_path  # already absolute (resolved in _worker)

                for ev in py_p.events:
                    cpp_ev           = dp.MidiNoteEvent()
                    seconds          = (ev.abs_tick / tpb) * spb
                    cpp_ev.abs_frame = int(seconds * sample_rate)
                    cpp_ev.msg_type  = ev.msg_type
                    cpp_ev.note      = ev.note
                    cpp_ev.velocity  = ev.velocity
                    cpp_ev.channel   = ev.channel
                    cpp_p.events.append(cpp_ev)

                cpp_payloads.append(cpp_p)

            bridge._engine.importMultiTrackMidi(cpp_payloads)
            logger.info(
                "midi_drop_importer: dispatched %d tracks via "
                "importMultiTrackMidi().", len(cpp_payloads),
            )
            return

        except (AttributeError, TypeError) as exc:
            logger.warning(
                "midi_drop_importer: importMultiTrackMidi() not available "
                "(%s) — using per-event fallback.", exc,
            )

        # ── Fallback: per-event bridge API ────────────────────────────────────
        engine = bridge._engine
        for py_p in payloads:
            # Re-use an existing C++ track mapped to this MIDI channel, or
            # create a new one.  Track index is clamped to 0-15 so it always
            # maps to a valid MIDI channel even if the file has >16 tracks.
            midi_channel = py_p.track_index % 16
            cpp_id       = bridge.get_or_create_instrument_track(midi_channel)

            if cpp_id < 0:
                logger.warning(
                    "midi_drop_importer: could not create C++ track for '%s'.",
                    py_p.name,
                )
                continue

            for ev in py_p.events:
                seconds   = (ev.abs_tick / tpb) * spb
                frame_pos = int(seconds * sample_rate)
                engine.add_midi_event(
                    cpp_id,
                    frame_pos,
                    ev.msg_type,
                    ev.channel,
                    ev.note,
                    ev.velocity,
                )

            engine.sort_midi_events(cpp_id)
            logger.debug(
                "midi_drop_importer: '%s' → %d events (fallback path).",
                py_p.name, len(py_p.events),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_call(fn: Optional[Callable], *args) -> None:
    """Call *fn* with *args* if it is not None; log but swallow exceptions."""
    if fn is None:
        return
    try:
        fn(*args)
    except Exception as exc:
        logger.error("midi_drop_importer: callback raised %s", exc)
