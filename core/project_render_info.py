"""
project_render_info.py -- Immutable snapshot data-models for full-project render.
==================================================================================
These dataclasses carry everything ProjectRenderPipeline needs to produce a
stereo offline render of the entire DAW project.  They are populated by the
GUI thread (_on_master_export in gui_windows.py) and passed to the worker
thread, so they must not hold any live GUI objects or Qt references.

Data-model overview
-------------------
  AutomationRenderInfo   -- one parameter's automation envelope as (t, v) pairs
  MidiTrackRenderInfo    -- one MIDI track: notes, instrument, automation, FX
  FullProjectRenderInfo  -- complete project snapshot (MIDI + audio + step events)

Relationship to mastering_export_worker.py
------------------------------------------
  TrackRenderInfo          (audio tracks, defined in mastering_export_worker.py)
  MidiTrackRenderInfo      (MIDI tracks, defined here)
  FullProjectRenderInfo    (aggregates both, defined here)

  MasteringExportWorker now accepts a FullProjectRenderInfo instead of a
  plain List[TrackRenderInfo] so every track type participates in the render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# AutomationRenderInfo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AutomationRenderInfo:
    """
    Automation envelope for one parameter, pre-converted to wall-clock time.

    Fields
    ------
    target_key : str
        Parameter name — "volume", "pan", or "PluginName.param_name".
        Only "volume" and "pan" are currently consumed by the render pipeline;
        other keys are silently ignored (plugin params are applied through the
        AudioFxChain at render time, not via this model).
    points : List[Tuple[float, float]]
        Sorted list of (time_secs, actual_value) pairs.  Values are in the
        real parameter unit (e.g. 0.0–2.0 for volume, -1.0–+1.0 for pan)
        NOT normalised 0–1.  The GUI snapshot code performs the conversion
        using AutomationEnvelope.evaluate() at each node's beat position.
    """
    target_key: str
    points:     List[Tuple[float, float]] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# MidiTrackRenderInfo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MidiTrackRenderInfo:
    """
    All data needed to render one MIDI instrument track offline via FluidSynth.

    Fields
    ------
    name     : str          Human-readable track label (used in stem filenames).
    channel  : int          MIDI channel 0–15 (must match the instrument plugin).
    notes    : list         MidiNote objects from MidiTrack.notes property.
                            Each note has: start_beat, duration, pitch, velocity.
    sf2_path : str          Absolute path to the SF2 soundfont file.
    bank     : int          GM bank number (default 0).
    preset   : int          GM program / preset number 0–127.
    volume   : float        Linear gain from the mixer strip (0–1 for MIDI).
    pan      : float        Stereo pan -1 (L) … +1 (R) from the mixer strip.
    automation : List[AutomationRenderInfo]
        Per-parameter envelopes.  "volume" and "pan" entries are converted to
        FluidSynth CC7 / CC10 events injected during the FluidSynth render pass.
    fx_chain : object       AudioFxChain or None (applied after FluidSynth PCM).
    """
    name:        str
    channel:     int
    notes:       list                          # list[MidiNote]
    sf2_path:    str
    bank:        int   = 0
    preset:      int   = 0
    volume:      float = 0.8                   # MIDI default matches mixer strip default (80/100)
    pan:         float = 0.0
    automation:  List[AutomationRenderInfo] = field(default_factory=list)
    fx_chain:    object = None                 # AudioFxChain | None


# ─────────────────────────────────────────────────────────────────────────────
# FullProjectRenderInfo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FullProjectRenderInfo:
    """
    Complete project snapshot for the offline mastering render pipeline.

    Passed from the GUI thread to MasteringExportWorker so the worker never
    needs to touch live Qt objects during the render.

    Fields
    ------
    midi_tracks  : List[MidiTrackRenderInfo]
        All MIDI instrument tracks (rendered via FluidSynth).
    audio_tracks : List[TrackRenderInfo]
        All audio file tracks (rendered by reading clips from disk).
        Uses the TrackRenderInfo dataclass from mastering_export_worker.py.
    step_events  : list
        Step-sequencer note events as (beat, channel, pitch, velocity, is_on)
        5-tuples.  Merged into the FluidSynth render pass at their beat positions.
    bpm          : float
        Project tempo used to convert beat positions to wall-clock seconds.
    sample_rate  : int
        Target sample rate for the rendered output (default 44100).
    """
    midi_tracks:   List[MidiTrackRenderInfo]
    audio_tracks:  list                       # List[TrackRenderInfo]
    step_events:   list = field(default_factory=list)  # [(beat, ch, note, vel, is_on)]
    bpm:           float = 120.0
    sample_rate:   int   = 44100
