"""
project_manager.py  --  Crystal DAW Project Serialiser
=======================================================
Saves and restores the complete DAW state to/from a UTF-8 JSON file
with the extension .dawproj.

Format (version 1)
------------------
{
    "version":    1,
    "bpm":        120.0,
    "time_signature": [4, 4],
    "midi_tracks":  [ { name, channel, color, clips:[…] }, … ],
    "audio_tracks": [ { name, track_id, color, clips:[…] }, … ],
    "audio_fx_chains": { "<track_id>": [ plugin_spec | null, … ], … },
    "midi_fx_chains":  { "<channel>":  [ plugin_spec | null, … ], … },
    "instruments": [ { name, sf2_path, bank, preset, channel }, … ]
}

plugin_spec: { "display_name": str, "enabled": bool, "params": dict }
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .midi_logic import MidiLogic
    from .audio_fx_chain import AudioFxChain

logger = logging.getLogger(__name__)

# Increment this whenever the saved format changes in a breaking way.
PROJECT_FILE_VERSION = 1


class ProjectManager:
    """Static-method helper for saving and loading Crystal DAW projects."""

    # ─────────────────────────────────────────────────────────────────────────
    # Save
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def save_project(
        path: str,
        midi_logic,
        audio_engine,
        audio_fx_chains: Dict[int, Any],
        midi_fx_chains:  Dict[int, Any],
        channel_rack_rows: Optional[list] = None,
    ) -> bool:
        """
        Serialise the full project state to a JSON file.

        Args:
            path              : Output .dawproj file path.
            midi_logic        : MidiLogic instance owning all tracks and notes.
            audio_engine      : AudioEngine instance owning InstrumentPlugin list.
            audio_fx_chains   : Dict mapping track_id → AudioFxChain.
            midi_fx_chains    : Dict mapping MIDI channel → AudioFxChain.
            channel_rack_rows : Optional list of ChannelStepData rows.

        Returns:
            True on success, False if an exception occurred.
        """
        try:
            doc: Dict[str, Any] = {
                "version":       PROJECT_FILE_VERSION,
                "bpm":           midi_logic.bpm,
                "time_signature": list(midi_logic._time_sig),
                "midi_tracks":    ProjectManager._midi_tracks_to_list(midi_logic),
                "audio_tracks":   ProjectManager._audio_tracks_to_list(midi_logic),
                "audio_fx_chains": {
                    str(tid): ProjectManager._chain_to_list(chain)
                    for tid, chain in audio_fx_chains.items()
                },
                "midi_fx_chains": {
                    str(ch): ProjectManager._chain_to_list(chain)
                    for ch, chain in midi_fx_chains.items()
                },
                "instruments": ProjectManager._instruments_to_list(audio_engine),
                # Automation envelopes keyed by track_id or MIDI channel
                "automation_lanes": ProjectManager._automation_to_dict(
                    audio_fx_chains, midi_fx_chains),
                # Channel rack / step sequencer rows
                "channel_rack": [
                    row.to_dict() for row in (channel_rack_rows or [])
                ],
            }

            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, ensure_ascii=False)

            logger.info("Project saved → %s", path)
            return True

        except Exception as exc:
            logger.error("ProjectManager.save_project failed: %s", exc)
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Load
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def load_project(path: str) -> Optional[Dict[str, Any]]:
        """
        Deserialise a .dawproj JSON file.

        Returns the raw dict, or None on failure.  The caller (MainWindow) is
        responsible for applying the dict to the live model objects.
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)

            ver = doc.get("version", 0)
            if ver != PROJECT_FILE_VERSION:
                logger.warning(
                    "Project file version %d differs from expected %d — "
                    "attempting best-effort load.",
                    ver, PROJECT_FILE_VERSION,
                )

            logger.info("Project loaded ← %s", path)
            return doc

        except Exception as exc:
            logger.error("ProjectManager.load_project failed: %s", exc)
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Rebuild helpers (called by MainWindow after load_project())
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def rebuild_fx_chain_from_list(
        slot_list: List[Optional[dict]],
        track_id_or_channel: int,
    ):
        """
        Re-instantiate an AudioFxChain from a saved slot list.

        Each item in slot_list is either None (empty slot) or a dict:
            { display_name, enabled, params }

        Returns the rebuilt AudioFxChain (or None if the import fails).
        """
        try:
            from .audio_fx_chain import AudioFxChain
            from .fx_plugin_registry import PLUGIN_REGISTRY

            chain = AudioFxChain(track_id=track_id_or_channel)

            for spec in slot_list:
                if spec is None:
                    chain.add_plugin(None)
                    continue

                cls = PLUGIN_REGISTRY.get(spec.get("display_name", ""))
                if cls is None:
                    logger.warning(
                        "Unknown plugin '%s' — slot will be empty.",
                        spec.get("display_name"),
                    )
                    chain.add_plugin(None)
                    continue

                try:
                    plugin = cls()
                    plugin.enabled = bool(spec.get("enabled", True))
                    plugin.set_params(spec.get("params", {}))
                    chain.add_plugin(plugin)
                except Exception as exc:
                    logger.warning(
                        "Could not instantiate plugin '%s': %s",
                        spec.get("display_name"), exc,
                    )
                    chain.add_plugin(None)

            return chain

        except Exception as exc:
            logger.error("rebuild_fx_chain_from_list failed: %s", exc)
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Private serialisation helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _midi_tracks_to_list(midi_logic) -> List[dict]:
        tracks = []
        for track in midi_logic.get_all_tracks():
            clips = []
            for clip in track.clips:
                notes = [
                    {
                        "start_beat": n.start_beat,
                        "duration":   n.duration,
                        "pitch":      n.pitch,
                        "velocity":   n.velocity,
                        "channel":    n.channel,
                    }
                    for n in clip.notes
                ]
                clips.append({
                    "start_beat": clip.start_beat,
                    "duration":   clip.duration,
                    "name":       clip.name,
                    "color":      clip.color,
                    "notes":      notes,
                })
            tracks.append({
                "name":    track.name,
                "channel": track.channel,
                "color":   track.color,
                "clips":   clips,
            })
        return tracks

    @staticmethod
    def _audio_tracks_to_list(midi_logic) -> List[dict]:
        tracks = []
        for at in midi_logic.get_audio_tracks():
            clips = [
                {
                    "path":             c.path,
                    "start_beat":       c.start_beat,
                    "name":             c.name,
                    "duration_seconds": c.duration_seconds,
                    "color":            c.color,
                }
                for c in at.clips
            ]
            tracks.append({
                "name":     at.name,
                "track_id": at.track_id,
                "color":    at.color,
                "clips":    clips,
            })
        return tracks

    @staticmethod
    def _chain_to_list(chain) -> List[Optional[dict]]:
        """Serialise every plugin slot in an AudioFxChain to a list."""
        if chain is None:
            return []
        result = []
        for plugin in chain.plugins:
            if plugin is None:
                result.append(None)
            else:
                result.append({
                    "display_name": plugin.DISPLAY_NAME,
                    "enabled":      bool(plugin.enabled),
                    "params":       plugin.get_params(),
                })
        return result

    @staticmethod
    def _instruments_to_list(audio_engine) -> List[dict]:
        """Serialise InstrumentPlugin records from AudioEngine."""
        try:
            result = []
            for p in audio_engine.get_instruments():
                result.append({
                    "name":     p.name,
                    "sf2_path": p.sf2_path,
                    "bank":     p.bank,
                    "preset":   p.preset,
                    "channel":  p.channel,
                })
            return result
        except Exception:
            return []

    @staticmethod
    def _automation_to_dict(
        audio_fx_chains: Dict[int, Any],
        midi_fx_chains:  Dict[int, Any],
    ) -> dict:
        """
        Serialise all non-empty AutomationEnvelope objects from both chain dicts.
        Returns {"audio": {str(track_id): {key: env_dict}},
                 "midi":  {str(channel):  {key: env_dict}}}.
        """
        def _envelopes_from_chain(chain) -> dict:
            if chain is None or not hasattr(chain, "envelopes"):
                return {}
            return {
                key: env.to_dict()
                for key, env in chain.envelopes.items()
                if getattr(env, "nodes", None)   # Skip empty envelopes
            }

        return {
            "audio": {
                str(tid): _envelopes_from_chain(chain)
                for tid, chain in audio_fx_chains.items()
            },
            "midi": {
                str(ch): _envelopes_from_chain(chain)
                for ch, chain in midi_fx_chains.items()
            },
        }

    @staticmethod
    def restore_automation_lanes(
        doc: dict,
        audio_fx_chains: Dict[int, Any],
        midi_fx_chains:  Dict[int, Any],
    ) -> None:
        """
        Read "automation_lanes" from a loaded project dict and restore
        AutomationEnvelope objects into the appropriate chains.
        Called by MainWindow after rebuilding FX chains on project load.
        """
        try:
            from .automation_lane import AutomationEnvelope
        except ImportError:
            logger.warning("automation_lane module not available -- skipping restore")
            return

        auto_data = doc.get("automation_lanes", {})

        for tid_str, env_dict in auto_data.get("audio", {}).items():
            chain = audio_fx_chains.get(int(tid_str))
            if chain is None:
                continue
            for key, ed in env_dict.items():
                chain.envelopes[key] = AutomationEnvelope.from_dict(ed)

        for ch_str, env_dict in auto_data.get("midi", {}).items():
            chain = midi_fx_chains.get(int(ch_str))
            if chain is None:
                continue
            for key, ed in env_dict.items():
                chain.envelopes[key] = AutomationEnvelope.from_dict(ed)

    @staticmethod
    def restore_channel_rack(doc: dict) -> list:
        """
        Deserialise the "channel_rack" section of a project doc.
        Returns a list of ChannelStepData objects, or [] on failure.
        """
        try:
            from .channel_rack import ChannelStepData
            return [ChannelStepData.from_dict(d)
                    for d in doc.get("channel_rack", [])]
        except Exception as exc:
            logger.warning("restore_channel_rack failed: %s", exc)
            return []
