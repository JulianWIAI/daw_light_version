"""
dspreset_engine.py -- Factory for the Decent Sampler audio engine.
===================================================================
Tries to return the C++ DecentSamplerEngine (from the daw_processors
extension).  Falls back to DsEnginePython, which uses pygame.mixer to
play WAV files at their recorded root pitch only -- no pitch shifting,
no ADSR, no polyphony beyond what pygame provides.

The fallback is intentionally minimal: all real DSP lives in C++.
Python callers should always prefer the C++ engine via get_ds_engine().

Public API
----------
  get_ds_engine(sample_rate, block_size) -> C++ DecentSamplerEngine
                                             or DsEnginePython
  load_preset_into_engine(engine, info)  -> send parsed zones to engine
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .dspreset_parser import DsInstrumentInfo, DsSampleZone, parse_dspreset

logger = logging.getLogger(__name__)


# ── Try to import the C++ extension ──────────────────────────────────────────

try:
    import daw_processors as dp
    _HAS_CPP = hasattr(dp, "DecentSamplerEngine")
except ImportError:
    dp = None           # type: ignore[assignment]
    _HAS_CPP = False


# ═══════════════════════════════════════════════════════════════════════════════
# Python fallback (root-pitch-only playback via pygame.mixer)
# ═══════════════════════════════════════════════════════════════════════════════

class DsEnginePython:
    """
    Minimal Decent Sampler fallback using pygame.mixer.

    Capabilities:
      - Plays each zone's WAV file when a matching MIDI note is triggered.
      - Pitch shifting: NONE (all samples play at their recorded root pitch).
      - ADSR: NONE (raw WAV played as-is).
      - Polyphony: limited to what pygame.Sound provides (one Sound per note).

    This class exists only so the application doesn't crash when the C++
    extension is unavailable.  Do not use it in production.
    """

    def __init__(self, sample_rate: int = 44100) -> None:
        self._sample_rate = sample_rate
        self._info:   Optional[DsInstrumentInfo] = None
        # MIDI note → pygame.Sound (root-pitch only, best-match zone)
        self._sounds: dict[int, object] = {}
        self._loaded = False
        logger.warning(
            "DsEnginePython: C++ DecentSamplerEngine not available. "
            "Using root-pitch-only pygame fallback — no pitch shifting."
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def load_preset(self, path: str) -> bool:
        """
        Parse the .dspreset, pre-load pygame.Sound objects for each zone's
        sample file.  Returns True if at least one zone loaded successfully.
        """
        self._sounds.clear()
        self._loaded = False

        info = parse_dspreset(path)
        if not info.zones:
            logger.error("DsEnginePython.load_preset: no zones in '%s'", path)
            return False

        self._info = info
        self._ensure_pygame()

        ok = 0
        for zone in info.zones:
            abs_path = os.path.join(info.base_dir, zone.path)
            if not os.path.isfile(abs_path):
                logger.debug("DsEnginePython: sample not found: %s", abs_path)
                continue
            try:
                import pygame.mixer as pgm
                snd = pgm.Sound(abs_path)
                # Map every MIDI note in this zone's range to this Sound.
                # Later zones overwrite earlier ones for the same note —
                # this is "last zone wins" which is fine for a fallback.
                for note in range(zone.lo_note, zone.hi_note + 1):
                    self._sounds[note] = snd
                ok += 1
            except Exception as exc:
                logger.debug("DsEnginePython: could not load '%s': %s",
                             abs_path, exc)

        self._loaded = ok > 0
        logger.info("DsEnginePython: loaded %d/%d zones from '%s'",
                    ok, len(info.zones), path)
        return self._loaded

    def is_loaded(self) -> bool:
        return self._loaded

    # ── MIDI ───────────────────────────────────────────────────────────────

    def note_on(self, channel: int, note: int, velocity: int) -> None:
        """Play the best-match WAV for this note (root pitch, no transposing)."""
        snd = self._sounds.get(note)
        if snd is None:
            return
        try:
            volume = max(0.0, min(1.0, velocity / 127.0))
            snd.set_volume(volume)
            snd.play()
        except Exception as exc:
            logger.debug("DsEnginePython.note_on: %s", exc)

    def note_off(self, channel: int, note: int, velocity: int) -> None:
        """Stop a playing note (pygame has no per-channel stop; best effort)."""
        snd = self._sounds.get(note)
        if snd is not None:
            try:
                snd.stop()
            except Exception:
                pass

    def all_notes_off(self, channel: int = 0) -> None:
        """Silence every loaded Sound."""
        for snd in set(self._sounds.values()):
            try:
                snd.stop()
            except Exception:
                pass

    # ── Stub methods expected by DsPresetPanel ─────────────────────────────

    def set_parameter(self, param: str, value: float) -> None:
        """No-op: Python fallback does not support real-time parameter control."""

    def render(self, num_samples: int):
        """No-op: Python fallback uses pygame's own audio thread, not render()."""
        return None, None

    # ── Internal helpers ───────────────────────────────────────────────────

    def _ensure_pygame(self) -> None:
        """Initialise pygame.mixer if not already running."""
        try:
            import pygame.mixer as pgm
            if not pgm.get_init():
                pgm.init(frequency=self._sample_rate, size=-16,
                         channels=2, buffer=512)
        except Exception as exc:
            logger.warning("DsEnginePython: pygame.mixer init failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Public factory + zone-load helper
# ═══════════════════════════════════════════════════════════════════════════════

def get_ds_engine(sample_rate: int = 44100, block_size: int = 512) -> object:
    """
    Return a C++ DecentSamplerEngine if available, otherwise DsEnginePython.

    The returned object satisfies the following interface:
      .load_preset(path: str) -> bool
      .note_on(channel, note, velocity)
      .note_off(channel, note, velocity)
      .all_notes_off(channel)
      .set_parameter(name: str, value: float)
      .render(num_samples: int) -> (left_array, right_array)  [C++ only]
      .is_loaded() -> bool
    """
    if _HAS_CPP:
        try:
            engine = dp.DecentSamplerEngine(float(sample_rate), block_size)
            logger.debug("get_ds_engine: using C++ DecentSamplerEngine")
            return engine
        except Exception as exc:
            logger.warning(
                "get_ds_engine: C++ engine construction failed (%s) — "
                "falling back to Python engine.", exc)

    return DsEnginePython(sample_rate)


def load_preset_into_engine(engine: object, info: DsInstrumentInfo) -> bool:
    """
    Transfer a pre-parsed DsInstrumentInfo into a DecentSamplerEngine.

    The C++ engine accepts zone data directly via load_zones() so the XML
    is never re-parsed.  The Python fallback uses load_preset() which
    re-parses internally (acceptable since it's a degraded path).

    Returns True if the engine reports it is loaded.
    """
    # C++ engine accepts structured zone data without re-reading the file.
    if _HAS_CPP and isinstance(engine, dp.DecentSamplerEngine):
        try:
            zones_cpp = []
            for z in info.zones:
                zd = dp.DsZoneData()
                zd.path       = os.path.join(info.base_dir, z.path)
                zd.root_note  = z.root_note
                zd.lo_note    = z.lo_note
                zd.hi_note    = z.hi_note
                zd.lo_vel     = z.lo_vel
                zd.hi_vel     = z.hi_vel
                zd.volume_db  = z.volume_db
                zd.pan        = z.pan
                zd.attack     = z.attack
                zd.decay      = z.decay
                zd.sustain    = z.sustain
                zd.release    = z.release
                zd.loop_enabled = z.loop_enabled
                zd.loop_start   = z.loop_start
                zd.loop_end     = z.loop_end
                zd.seq_position = z.seq_position
                zd.seq_length   = z.seq_length
                zones_cpp.append(zd)
            engine.load_zones(zones_cpp)
            return engine.is_loaded()
        except Exception as exc:
            logger.warning(
                "load_preset_into_engine: load_zones failed (%s) — "
                "trying load_preset() path.", exc)

    # Python fallback or C++ fallback: just pass the file path.
    if info.path and os.path.isfile(info.path):
        try:
            return bool(engine.load_preset(info.path))
        except Exception as exc:
            logger.error("load_preset_into_engine: load_preset failed: %s", exc)

    return False
