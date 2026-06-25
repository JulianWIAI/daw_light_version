"""
ai_mix_assistant.py -- Genre-Aware Intelligent Mix Orchestrator
===============================================================
Analyses the current track layout, detects track roles from names,
and applies a genre-specific FX template to every track's AudioFxChain.

Usage
-----
    assistant = AIMixAssistant(audio_file_player, tracks, master_chain)
    assistant.apply(genre="TRAP")

Supported genres: TRAP, TECHNO, PHONK, POP, HIPHOP, EDM, HOUSE, CINEMATIC

Architecture constraints
------------------------
* All coloration happens at the track level -- master chain receives only a
  static gain offset and a BrickwallLimiterPlugin.
* Every plugin added by this module is tagged with ``_ai_managed = True``
  so ``AudioFxChain.clear_ai_slots()`` can undo the whole pass non-destructively.
* Spectral panning is applied across lead / backing groups to reduce
  inter-track frequency masking.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Role keyword banks ────────────────────────────────────────────────────────

_ROLE_KEYWORDS: Dict[str, List[str]] = {
    "kick":   ["kick", "808", "bd", "bassdrum", "bass drum"],
    "drums":  ["drum", "kit", "trap", "perc", "snare", "clap", "hat", "cymbal", "rim"],
    "bass":   ["bass", "sub", "low end"],
    "vocals": ["vocal", "vox", "voice", "sing", "rap", "hook", "verse"],
    "lead":   ["lead", "melody", "arp", "riff", "theme"],
    "pad":    ["pad", "string", "choir", "ambient", "atmo", "texture", "layer"],
    "synth":  ["synth", "keys", "pluck", "stab", "bell"],
    "guitar": ["guitar", "gtr"],
    "piano":  ["piano", "epiano", "keys", "keyboard"],
    "fx":     ["fx", "riser", "foley", "sfx", "noise", "sweep"],
}

# Roles considered "lead" for spectral panning group assignment.
_LEAD_ROLES = {"lead", "vocals"}
_BACKING_ROLES = {"pad", "synth", "guitar", "piano"}


def _detect_role(name: str) -> str:
    """Return the best-matching role key for a track name, or 'other'."""
    lower = name.lower()
    # Kick before drums so "kick drum" → kick
    for role in ("kick", "drums", "bass", "vocals", "lead", "pad", "synth",
                 "guitar", "piano", "fx"):
        if any(kw in lower for kw in _ROLE_KEYWORDS[role]):
            return role
    return "other"


# ── Plugin import helpers (lazy, so missing modules degrade gracefully) ───────

def _import_plugins():
    """Return a namespace dict with all plugin classes, or empty dict on failure."""
    ns: dict = {}
    try:
        from .fx_plugins_cpp import (
            BrickwallLimiterPlugin, MultibandCompressorPlugin, DynamicEQPlugin,
            DeEsserPlugin, TransientShaperPlugin, GateExpanderPlugin,
        )
        ns.update(dict(
            BrickwallLimiterPlugin=BrickwallLimiterPlugin,
            MultibandCompressorPlugin=MultibandCompressorPlugin,
            DynamicEQPlugin=DynamicEQPlugin,
            DeEsserPlugin=DeEsserPlugin,
            TransientShaperPlugin=TransientShaperPlugin,
            GateExpanderPlugin=GateExpanderPlugin,
        ))
    except Exception as exc:
        logger.warning("AIMixAssistant: could not import fx_plugins_cpp — %s", exc)

    try:
        from .fx_plugins_harmonic import (
            SaturationPlugin, ExciterPlugin, BitcrusherPlugin, OverdrivePlugin,
        )
        ns.update(dict(
            SaturationPlugin=SaturationPlugin,
            ExciterPlugin=ExciterPlugin,
            BitcrusherPlugin=BitcrusherPlugin,
            OverdrivePlugin=OverdrivePlugin,
        ))
    except Exception as exc:
        logger.warning("AIMixAssistant: could not import fx_plugins_harmonic — %s", exc)

    try:
        from .fx_plugins_filter import AutoFilterPlugin
        ns["AutoFilterPlugin"] = AutoFilterPlugin
    except Exception as exc:
        logger.warning("AIMixAssistant: could not import fx_plugins_filter — %s", exc)

    try:
        from .fx_plugins_pedalboard import (
            EQPlugin, ReverbPlugin, CompressorPlugin, ChorusPlugin,
        )
        ns.update(dict(
            EQPlugin=EQPlugin,
            ReverbPlugin=ReverbPlugin,
            CompressorPlugin=CompressorPlugin,
            ChorusPlugin=ChorusPlugin,
        ))
    except Exception as exc:
        logger.warning("AIMixAssistant: could not import fx_plugins_pedalboard — %s", exc)

    try:
        from .fx_plugins_spatial import (
            StereoImagerPlugin, DelayEchoPlugin, FlangerPlugin, PhaserPlugin,
        )
        ns.update(dict(
            StereoImagerPlugin=StereoImagerPlugin,
            DelayEchoPlugin=DelayEchoPlugin,
            FlangerPlugin=FlangerPlugin,
            PhaserPlugin=PhaserPlugin,
        ))
    except Exception as exc:
        logger.warning("AIMixAssistant: could not import fx_plugins_spatial — %s", exc)

    try:
        from .fx_plugins_spectral_panning import SpectralPanningPlugin
        ns["SpectralPanningPlugin"] = SpectralPanningPlugin
    except Exception as exc:
        logger.warning("AIMixAssistant: could not import fx_plugins_spectral_panning — %s", exc)

    try:
        from .fx_plugins_loudness import LoudnessAutomationPlugin
        ns["LoudnessAutomationPlugin"] = LoudnessAutomationPlugin
    except Exception as exc:
        logger.warning("AIMixAssistant: could not import fx_plugins_loudness — %s", exc)

    return ns


# ── Track wrapper ─────────────────────────────────────────────────────────────

class _TrackView:
    """Thin wrapper combining a track object with its resolved role."""
    def __init__(self, track, chain, role: str):
        self.track = track
        self.chain = chain    # AudioFxChain
        self.role  = role
        self.name  = getattr(track, "name", "") or ""


# ── AIMixAssistant ────────────────────────────────────────────────────────────

class AIMixAssistant:
    """
    Genre-aware mix orchestrator.

    Parameters
    ----------
    audio_file_player : AudioFilePlayer
        Used to retrieve AudioFxChain instances.
    tracks : list
        List of track objects (must have .track_id and .name attributes).
    master_chain : AudioFxChain or None
        Master bus chain.  Receives only a brickwall limiter + gain offset.
    """

    def __init__(self, audio_file_player, tracks, master_chain=None):
        self._player       = audio_file_player
        self._tracks       = tracks
        self._master_chain = master_chain

    # ── Public API ────────────────────────────────────────────────────────────

    def apply(self, genre: str) -> None:
        """
        Apply the FX template for *genre* to all tracks.

        Clears any previously AI-managed slots first, then:
        1. Applies per-track templates.
        2. Resolves inter-track spectral panning.
        3. Configures the master loudness.
        """
        genre = genre.upper()
        logger.info("AIMixAssistant: applying genre '%s' to %d tracks.", genre, len(self._tracks))

        views = self._build_views()

        # Clear previous AI-managed plugins from all chains.
        for tv in views:
            tv.chain.clear_ai_slots()
        if self._master_chain is not None:
            self._master_chain.clear_ai_slots()

        px = _import_plugins()
        if not px:
            logger.error("AIMixAssistant: no plugins available — aborting.")
            return

        self._apply_track_templates(genre, views, px)
        self._resolve_inter_track_masking(views, px)
        self._configure_master_loudness(genre, px)

    def clear(self) -> None:
        """Remove all AI-managed plugins from every chain."""
        for track in self._tracks:
            chain = self._player.get_fx_chain(getattr(track, "track_id", -1))
            if chain is not None:
                chain.clear_ai_slots()
        if self._master_chain is not None:
            self._master_chain.clear_ai_slots()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_views(self) -> List[_TrackView]:
        views = []
        for track in self._tracks:
            tid   = getattr(track, "track_id", None)
            chain = self._player.get_fx_chain(tid) if tid is not None else None
            if chain is None:
                continue
            role = _detect_role(getattr(track, "name", "") or "")
            views.append(_TrackView(track, chain, role))
        return views

    # ── Per-track genre templates ─────────────────────────────────────────────

    def _apply_track_templates(
        self,
        genre: str,
        views: List[_TrackView],
        px: dict,
    ) -> None:
        handler = {
            "TRAP":      self._tmpl_trap,
            "TECHNO":    self._tmpl_techno,
            "PHONK":     self._tmpl_phonk,
            "POP":       self._tmpl_pop,
            "HIPHOP":    self._tmpl_hiphop,
            "EDM":       self._tmpl_edm,
            "HOUSE":     self._tmpl_house,
            "CINEMATIC": self._tmpl_cinematic,
        }.get(genre)

        if handler is None:
            logger.warning("AIMixAssistant: unknown genre '%s'.", genre)
            return

        for tv in views:
            try:
                handler(tv, px)
            except Exception as exc:
                logger.warning(
                    "AIMixAssistant: template error track='%s' role='%s' — %s",
                    tv.name, tv.role, exc,
                )

    # ─── TRAP ─────────────────────────────────────────────────────────────────

    def _tmpl_trap(self, tv: _TrackView, px: dict) -> None:
        add = tv.chain.add_ai_plugin

        if tv.role == "kick":
            # 808-style punchy sub with saturated character
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 2.0
                ts.sustain_gain = 1.4
                add(ts)
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 4.0
                sat.output_db = -2.0
                add(sat)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  8.0   # massive sub
                eq.eq_250 = -3.0   # cut mud
                eq.eq_4k  = -1.0
                add(eq)

        elif tv.role == "drums":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 1.6
                ts.sustain_gain = 0.7
                add(ts)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -18.0
                c.ratio     = 4.0
                c.attack    = 5.0
                c.release   = 80.0
                add(c)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_250 = -4.0
                eq.eq_4k  =  3.0   # air / presence
                eq.eq_16k =  2.0
                add(eq)

        elif tv.role == "bass":
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 6.0
                sat.output_db = -3.0
                add(sat)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -16.0
                c.ratio     = 5.0
                c.attack    = 10.0
                c.release   = 120.0
                add(c)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  6.0
                eq.eq_250 = -2.0
                add(eq)

        elif tv.role == "vocals":
            if "DeEsserPlugin" in px:
                ds = px["DeEsserPlugin"]()
                ds.frequency_hz  = 7500.0
                ds.threshold_db  = -22.0
                ds.ratio         = 4.0
                ds.attack_ms     = 1.0
                ds.release_ms    = 60.0
                add(ds)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -20.0
                c.ratio     = 3.5
                c.attack    = 8.0
                c.release   = 100.0
                add(c)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.35
                rv.damp = 0.6
                rv.wet  = 0.18
                add(rv)
            if "DelayEchoPlugin" in px:
                add(px["DelayEchoPlugin"]())

        elif tv.role in ("lead", "synth"):
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 3.0
                sat.output_db = -1.5
                add(sat)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.4
                rv.damp = 0.5
                rv.wet  = 0.20
                add(rv)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.3
                add(si)

        elif tv.role == "pad":
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.65
                rv.damp = 0.4
                rv.wet  = 0.35
                add(rv)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.5
                add(si)

    # ─── TECHNO ───────────────────────────────────────────────────────────────

    def _tmpl_techno(self, tv: _TrackView, px: dict) -> None:
        add = tv.chain.add_ai_plugin

        if tv.role == "kick":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 2.2
                ts.sustain_gain = 0.8
                add(ts)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  5.0
                eq.eq_250 = -5.0   # cut mid-mud for punch
                eq.eq_4k  =  2.0
                add(eq)

        elif tv.role == "drums":
            if "GateExpanderPlugin" in px:
                add(px["GateExpanderPlugin"]())
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 1.8
                ts.sustain_gain = 0.5
                add(ts)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_250 = -6.0
                eq.eq_4k  =  4.0
                eq.eq_16k =  3.0
                add(eq)

        elif tv.role == "bass":
            if "AutoFilterPlugin" in px:
                lp = px["AutoFilterPlugin"]()
                lp.filter_mode = 0    # LP
                lp.cutoff_hz   = 180.0
                lp.resonance   = 0.3
                add(lp)
            if "OverdrivePlugin" in px:
                od = px["OverdrivePlugin"]()
                od.pregain_db  = 8.0
                od.output_db   = -4.0
                add(od)

        elif tv.role in ("lead", "synth"):
            if "FlangerPlugin" in px:
                fl = px["FlangerPlugin"]()
                fl.rate_hz   = 0.3
                fl.depth_ms  = 3.0
                fl.feedback  = 0.4
                fl.wet       = 0.35
                add(fl)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.4
                add(si)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.5
                rv.damp = 0.3
                rv.wet  = 0.22
                add(rv)

        elif tv.role == "pad":
            if "PhaserPlugin" in px:
                ph = px["PhaserPlugin"]()
                ph.stages  = 4
                ph.rate_hz = 0.2
                ph.depth   = 0.7
                ph.wet     = 0.4
                add(ph)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.6
                add(si)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.7
                rv.damp = 0.2
                rv.wet  = 0.40
                add(rv)

        elif tv.role == "vocals":
            if "DeEsserPlugin" in px:
                ds = px["DeEsserPlugin"]()
                ds.frequency_hz = 8000.0
                ds.threshold_db = -24.0
                ds.ratio        = 3.0
                ds.attack_ms    = 1.0
                ds.release_ms   = 50.0
                add(ds)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -22.0
                c.ratio     = 3.0
                c.attack    = 5.0
                c.release   = 80.0
                add(c)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.55
                rv.damp = 0.4
                rv.wet  = 0.30
                add(rv)

    # ─── PHONK ────────────────────────────────────────────────────────────────

    def _tmpl_phonk(self, tv: _TrackView, px: dict) -> None:
        add = tv.chain.add_ai_plugin

        if tv.role == "kick":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 2.5
                ts.sustain_gain = 1.8
                add(ts)
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 8.0
                sat.output_db = -3.0
                add(sat)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  10.0
                eq.eq_250 = -4.0
                add(eq)

        elif tv.role == "drums":
            if "BitcrusherPlugin" in px:
                bc = px["BitcrusherPlugin"]()
                bc.bit_depth = 14
                bc.wet       = 0.30
                add(bc)
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 2.0
                ts.sustain_gain = 0.6
                add(ts)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_250 = -5.0
                eq.eq_4k  =  5.0
                add(eq)

        elif tv.role == "bass":
            if "OverdrivePlugin" in px:
                od = px["OverdrivePlugin"]()
                od.pregain_db = 12.0
                od.output_db  = -4.0
                add(od)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  8.0
                eq.eq_250 = -3.0
                add(eq)

        elif tv.role == "vocals":
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 3.0
                sat.output_db = -1.0
                add(sat)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -18.0
                c.ratio     = 4.0
                c.attack    = 8.0
                c.release   = 100.0
                add(c)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.5
                rv.damp = 0.55
                rv.wet  = 0.25
                add(rv)

        elif tv.role in ("lead", "synth", "pad", "other"):
            # Lo-fi sample character
            if "BitcrusherPlugin" in px:
                bc = px["BitcrusherPlugin"]()
                bc.bit_depth = 12
                bc.wet       = 0.45
                add(bc)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.6
                rv.damp = 0.5
                rv.wet  = 0.30
                add(rv)

    # ─── POP ──────────────────────────────────────────────────────────────────

    def _tmpl_pop(self, tv: _TrackView, px: dict) -> None:
        add = tv.chain.add_ai_plugin

        if tv.role == "kick":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 1.8
                ts.sustain_gain = 0.9
                add(ts)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  3.0
                eq.eq_250 = -3.0
                eq.eq_4k  =  2.0
                add(eq)

        elif tv.role == "drums":
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -16.0
                c.ratio     = 3.5
                c.attack    = 8.0
                c.release   = 90.0
                add(c)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_250 = -3.0
                eq.eq_4k  =  2.0
                eq.eq_16k =  2.0
                add(eq)

        elif tv.role == "bass":
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -14.0
                c.ratio     = 4.0
                c.attack    = 12.0
                c.release   = 150.0
                add(c)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  2.0
                eq.eq_250 = -1.0
                add(eq)

        elif tv.role == "vocals":
            if "DeEsserPlugin" in px:
                ds = px["DeEsserPlugin"]()
                ds.frequency_hz = 7000.0
                ds.threshold_db = -20.0
                ds.ratio        = 4.0
                ds.attack_ms    = 1.0
                ds.release_ms   = 50.0
                add(ds)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -18.0
                c.ratio     = 3.0
                c.attack    = 6.0
                c.release   = 80.0
                add(c)
            if "ExciterPlugin" in px:
                ex = px["ExciterPlugin"]()
                ex.crossover_hz = 5000.0
                ex.harmonics    = 0.3
                ex.air_db       = 2.0
                ex.wet          = 0.25
                add(ex)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.28
                rv.damp = 0.65
                rv.wet  = 0.15
                add(rv)
            if "DelayEchoPlugin" in px:
                add(px["DelayEchoPlugin"]())

        elif tv.role in ("lead", "synth"):
            if "ChorusPlugin" in px:
                ch = px["ChorusPlugin"]()
                ch.rate  = 1.2
                ch.depth = 0.4
                ch.wet   = 0.30
                add(ch)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.3
                add(si)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.35
                rv.damp = 0.55
                rv.wet  = 0.20
                add(rv)

        elif tv.role == "pad":
            if "ChorusPlugin" in px:
                ch = px["ChorusPlugin"]()
                ch.rate  = 0.8
                ch.depth = 0.5
                ch.wet   = 0.35
                add(ch)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.5
                add(si)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.55
                rv.damp = 0.45
                rv.wet  = 0.30
                add(rv)

    # ─── HIPHOP ───────────────────────────────────────────────────────────────

    def _tmpl_hiphop(self, tv: _TrackView, px: dict) -> None:
        add = tv.chain.add_ai_plugin

        if tv.role == "kick":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 2.0
                ts.sustain_gain = 1.5
                add(ts)
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 5.0
                sat.output_db = -2.5
                add(sat)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  7.0
                eq.eq_250 = -4.0
                add(eq)

        elif tv.role == "drums":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 1.7
                ts.sustain_gain = 0.8
                add(ts)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -18.0
                c.ratio     = 4.5
                c.attack    = 6.0
                c.release   = 100.0
                add(c)

        elif tv.role == "bass":
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -15.0
                c.ratio     = 5.0
                c.attack    = 10.0
                c.release   = 130.0
                add(c)
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 4.0
                sat.output_db = -2.0
                add(sat)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  5.0
                eq.eq_250 = -2.0
                add(eq)

        elif tv.role == "vocals":
            if "DeEsserPlugin" in px:
                ds = px["DeEsserPlugin"]()
                ds.frequency_hz = 7500.0
                ds.threshold_db = -22.0
                ds.ratio        = 4.0
                ds.attack_ms    = 1.0
                ds.release_ms   = 60.0
                add(ds)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -18.0
                c.ratio     = 3.5
                c.attack    = 8.0
                c.release   = 90.0
                add(c)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.30
                rv.damp = 0.6
                rv.wet  = 0.15
                add(rv)

        elif tv.role in ("lead", "synth", "pad"):
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 2.0
                sat.output_db = -1.0
                add(sat)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.45
                rv.damp = 0.5
                rv.wet  = 0.22
                add(rv)

    # ─── EDM ──────────────────────────────────────────────────────────────────

    def _tmpl_edm(self, tv: _TrackView, px: dict) -> None:
        add = tv.chain.add_ai_plugin

        if tv.role == "kick":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 2.4
                ts.sustain_gain = 0.7
                add(ts)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  6.0
                eq.eq_250 = -6.0
                eq.eq_4k  =  3.0
                add(eq)

        elif tv.role == "drums":
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -16.0
                c.ratio     = 5.0
                c.attack    = 4.0
                c.release   = 60.0
                add(c)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_250 = -5.0
                eq.eq_4k  =  4.0
                eq.eq_16k =  3.0
                add(eq)

        elif tv.role == "bass":
            if "AutoFilterPlugin" in px:
                lp = px["AutoFilterPlugin"]()
                lp.filter_mode = 0    # LP
                lp.cutoff_hz   = 200.0
                lp.resonance   = 0.4
                add(lp)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -14.0
                c.ratio     = 5.0
                c.attack    = 8.0
                c.release   = 100.0
                add(c)

        elif tv.role in ("lead", "synth"):
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.5
                add(si)
            if "ChorusPlugin" in px:
                ch = px["ChorusPlugin"]()
                ch.rate  = 1.5
                ch.depth = 0.45
                ch.wet   = 0.28
                add(ch)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.45
                rv.damp = 0.4
                rv.wet  = 0.22
                add(rv)

        elif tv.role == "pad":
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.7
                add(si)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.70
                rv.damp = 0.3
                rv.wet  = 0.40
                add(rv)
            if "ChorusPlugin" in px:
                ch = px["ChorusPlugin"]()
                ch.rate  = 0.6
                ch.depth = 0.6
                ch.wet   = 0.30
                add(ch)

        elif tv.role == "vocals":
            if "DeEsserPlugin" in px:
                ds = px["DeEsserPlugin"]()
                ds.frequency_hz = 7500.0
                ds.threshold_db = -22.0
                ds.ratio        = 3.5
                ds.attack_ms    = 1.0
                ds.release_ms   = 55.0
                add(ds)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -19.0
                c.ratio     = 3.0
                c.attack    = 6.0
                c.release   = 80.0
                add(c)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.40
                rv.damp = 0.5
                rv.wet  = 0.22
                add(rv)

    # ─── HOUSE ────────────────────────────────────────────────────────────────

    def _tmpl_house(self, tv: _TrackView, px: dict) -> None:
        add = tv.chain.add_ai_plugin

        if tv.role == "kick":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 2.0
                ts.sustain_gain = 1.0
                add(ts)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  4.0
                eq.eq_250 = -4.0
                eq.eq_4k  =  1.5
                add(eq)

        elif tv.role == "drums":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 1.6
                ts.sustain_gain = 0.8
                add(ts)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -15.0
                c.ratio     = 4.0
                c.attack    = 6.0
                c.release   = 80.0
                add(c)

        elif tv.role == "bass":
            if "SaturationPlugin" in px:
                sat = px["SaturationPlugin"]()
                sat.drive_db  = 3.0
                sat.output_db = -1.5
                add(sat)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -15.0
                c.ratio     = 4.0
                c.attack    = 12.0
                c.release   = 140.0
                add(c)

        elif tv.role in ("lead", "synth"):
            if "PhaserPlugin" in px:
                ph = px["PhaserPlugin"]()
                ph.stages  = 4
                ph.rate_hz = 0.5
                ph.depth   = 0.6
                ph.wet     = 0.30
                add(ph)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.3
                add(si)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.38
                rv.damp = 0.5
                rv.wet  = 0.20
                add(rv)

        elif tv.role == "pad":
            if "ChorusPlugin" in px:
                ch = px["ChorusPlugin"]()
                ch.rate  = 0.7
                ch.depth = 0.55
                ch.wet   = 0.32
                add(ch)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.5
                add(si)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.60
                rv.damp = 0.4
                rv.wet  = 0.32
                add(rv)

        elif tv.role == "vocals":
            if "DeEsserPlugin" in px:
                ds = px["DeEsserPlugin"]()
                ds.frequency_hz = 7000.0
                ds.threshold_db = -21.0
                ds.ratio        = 3.5
                ds.attack_ms    = 1.0
                ds.release_ms   = 55.0
                add(ds)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -17.0
                c.ratio     = 3.0
                c.attack    = 7.0
                c.release   = 85.0
                add(c)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.40
                rv.damp = 0.55
                rv.wet  = 0.22
                add(rv)

    # ─── CINEMATIC ────────────────────────────────────────────────────────────

    def _tmpl_cinematic(self, tv: _TrackView, px: dict) -> None:
        add = tv.chain.add_ai_plugin

        if tv.role == "kick":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 1.6
                ts.sustain_gain = 1.2
                add(ts)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.55
                rv.damp = 0.5
                rv.wet  = 0.25
                add(rv)

        elif tv.role == "drums":
            if "TransientShaperPlugin" in px:
                ts = px["TransientShaperPlugin"]()
                ts.attack_gain  = 1.5
                ts.sustain_gain = 1.1
                add(ts)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.65
                rv.damp = 0.45
                rv.wet  = 0.30
                add(rv)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_250 = -2.0
                eq.eq_4k  =  1.5
                add(eq)

        elif tv.role == "bass":
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -14.0
                c.ratio     = 3.5
                c.attack    = 15.0
                c.release   = 180.0
                add(c)
            if "EQPlugin" in px:
                eq = px["EQPlugin"]()
                eq.eq_32  =  3.0
                eq.eq_250 = -1.5
                add(eq)

        elif tv.role == "vocals":
            if "DeEsserPlugin" in px:
                ds = px["DeEsserPlugin"]()
                ds.frequency_hz = 6500.0
                ds.threshold_db = -20.0
                ds.ratio        = 3.0
                ds.attack_ms    = 1.5
                ds.release_ms   = 60.0
                add(ds)
            if "CompressorPlugin" in px:
                c = px["CompressorPlugin"]()
                c.threshold = -16.0
                c.ratio     = 2.5
                c.attack    = 10.0
                c.release   = 120.0
                add(c)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.60
                rv.damp = 0.45
                rv.wet  = 0.28
                add(rv)

        elif tv.role in ("lead", "synth"):
            if "ExciterPlugin" in px:
                ex = px["ExciterPlugin"]()
                ex.crossover_hz = 4000.0
                ex.harmonics    = 0.25
                ex.air_db       = 1.5
                ex.wet          = 0.20
                add(ex)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.65
                rv.damp = 0.4
                rv.wet  = 0.32
                add(rv)
            if "DelayEchoPlugin" in px:
                add(px["DelayEchoPlugin"]())
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.4
                add(si)

        elif tv.role == "pad":
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.8
                add(si)
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.80
                rv.damp = 0.3
                rv.wet  = 0.45
                add(rv)
            if "ChorusPlugin" in px:
                ch = px["ChorusPlugin"]()
                ch.rate  = 0.4
                ch.depth = 0.6
                ch.wet   = 0.28
                add(ch)

        elif tv.role in ("guitar", "piano"):
            if "ReverbPlugin" in px:
                rv = px["ReverbPlugin"]()
                rv.room = 0.55
                rv.damp = 0.5
                rv.wet  = 0.28
                add(rv)
            if "StereoImagerPlugin" in px:
                si = px["StereoImagerPlugin"]()
                si.width = 1.2
                add(si)

    # ── Inter-track spectral panning ──────────────────────────────────────────

    def _resolve_inter_track_masking(
        self,
        views: List[_TrackView],
        px: dict,
    ) -> None:
        """
        Assign SpectralPanningPlugin to lead and backing tracks so their
        frequency content is steered apart in the stereo field.

        Lead tracks → group 0, slot A (narrows to centre for the hot band).
        Backing tracks → group 0, slot B (spreads around the lead).
        """
        if "SpectralPanningPlugin" not in px:
            return

        lead_views    = [tv for tv in views if tv.role in _LEAD_ROLES]
        backing_views = [tv for tv in views if tv.role in _BACKING_ROLES]

        if not lead_views or not backing_views:
            return

        for tv in lead_views:
            sp = px["SpectralPanningPlugin"]()
            sp.group_id      = 0
            sp.slot          = 0     # A = lead
            sp.tolerance_hz  = 300.0
            sp.max_pan       = 0.25
            sp.smooth_ms     = 30.0
            tv.chain.add_ai_plugin(sp)

        for tv in backing_views:
            sp = px["SpectralPanningPlugin"]()
            sp.group_id      = 0
            sp.slot          = 1     # B = backing fills around lead
            sp.tolerance_hz  = 300.0
            sp.max_pan       = 0.40
            sp.smooth_ms     = 30.0
            tv.chain.add_ai_plugin(sp)

    # ── Master loudness ───────────────────────────────────────────────────────

    # Ceiling dBFS per genre — louder genres sit higher.
    _MASTER_CEILING: Dict[str, float] = {
        "TRAP":      -0.5,
        "TECHNO":    -0.1,
        "PHONK":     -1.0,
        "POP":       -0.1,
        "HIPHOP":    -1.0,
        "EDM":       -0.3,
        "HOUSE":     -0.3,
        "CINEMATIC": -1.5,
    }

    # Static gain offset (dB) added before the limiter.
    _MASTER_GAIN: Dict[str, float] = {
        "TRAP":      1.0,
        "TECHNO":    1.5,
        "PHONK":     0.5,
        "POP":       2.0,
        "HIPHOP":    0.5,
        "EDM":       2.0,
        "HOUSE":     1.5,
        "CINEMATIC": 0.0,
    }

    def _configure_master_loudness(self, genre: str, px: dict) -> None:
        if self._master_chain is None:
            return

        ceiling = self._MASTER_CEILING.get(genre, -1.0)
        gain_db = self._MASTER_GAIN.get(genre, 0.0)

        # Apply static gain via master chain volume (linear conversion).
        import math
        self._master_chain.volume = 10 ** (gain_db / 20.0)

        if "BrickwallLimiterPlugin" in px:
            lim = px["BrickwallLimiterPlugin"]()
            lim.ceiling_db = ceiling
            self._master_chain.add_ai_plugin(lim)
            logger.info(
                "AIMixAssistant: master ceiling=%.1f dBFS, gain=+%.1f dB.",
                ceiling, gain_db,
            )
