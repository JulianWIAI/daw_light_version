"""
sfz_engine_python.py -- Pure-Python SFZ metadata parser + factory.
====================================================================
Provides two things:

1. SfzMetaParser  -- lightweight SFZ text parser (no audio) that mirrors
                     the C++ SfzParser interface so the GUI always has access
                     to instrument metadata, even without the C++ extension.

2. get_sfz_engine(sample_rate) / parse_sfz(path)
                  -- factory functions that prefer the C++ implementation
                     (dp.SfizzEngine / dp.SfzParser) and fall back to this
                     module when the extension is not available.

Audio playback fallback:
   SfizzEnginePython uses pygame.mixer to play raw .wav samples mapped to
   MIDI notes.  It supports single-sample-per-key mappings only (no
   round-robin, no disk streaming, no polyphonic SFZ DSP).  It is provided
   as a graceful degradation path — for production use, build the C++ extension.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Data classes (mirror the C++ structs) ─────────────────────────────────────

@dataclass
class SfzKeyRange:
    lo: int = 0
    hi: int = 127

@dataclass
class SfzVelRange:
    lo: int = 0
    hi: int = 127

@dataclass
class SfzRegionInfo:
    key_range:    SfzKeyRange = field(default_factory=SfzKeyRange)
    vel_range:    SfzVelRange = field(default_factory=SfzVelRange)
    sample:       str   = ""
    volume:       float = 0.0     # dB
    pan:          float = 0.0     # -100 to +100
    seq_length:   int   = 1
    seq_position: int   = 1
    group:        int   = 0

@dataclass
class SfzGroupInfo:
    key_range: SfzKeyRange         = field(default_factory=SfzKeyRange)
    vel_range: SfzVelRange         = field(default_factory=SfzVelRange)
    volume:    float               = 0.0
    pan:       float               = 0.0
    regions:   List[SfzRegionInfo] = field(default_factory=list)

@dataclass
class SfzInstrumentInfo:
    name:        str                          = ""
    path:        str                          = ""
    num_regions: int                          = 0
    num_groups:  int                          = 0
    groups:      List[SfzGroupInfo]           = field(default_factory=list)
    regions:     List[SfzRegionInfo]          = field(default_factory=list)
    cc_labels:   List[Tuple[int, str]]        = field(default_factory=list)

# ── Note name → MIDI number helper ────────────────────────────────────────────

_NOTE_SEMITONES = {'c': 0, 'd': 2, 'e': 4, 'f': 5, 'g': 7, 'a': 9, 'b': 11}

def _parse_note(s: str) -> int:
    """Convert a note name ('C4', 'D#3') or integer string to a MIDI number."""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    if not s:
        return -1
    c = s[0].lower()
    if c not in _NOTE_SEMITONES:
        return -1
    note = _NOTE_SEMITONES[c]
    idx = 1
    if idx < len(s) and s[idx] == '#':
        note += 1; idx += 1
    elif idx < len(s) and s[idx] == 'b':
        note -= 1; idx += 1
    try:
        octave = int(s[idx:])
        return max(0, min(127, (octave + 1) * 12 + note))
    except (ValueError, IndexError):
        return -1

# ── Lightweight SFZ text parser ───────────────────────────────────────────────

class SfzMetaParser:
    """
    Pure-Python SFZ metadata extractor.

    Parses <group> / <region> structure, key/velocity ranges, sample paths,
    round-robin info, and CC labels.  No audio engine required.
    """

    @staticmethod
    def parse(sfz_path: str) -> SfzInstrumentInfo:
        """
        Parse an SFZ file and return an SfzInstrumentInfo.
        Returns an empty SfzInstrumentInfo on failure.
        """
        info = SfzInstrumentInfo()
        info.path = sfz_path
        info.name = os.path.splitext(os.path.basename(sfz_path))[0]

        try:
            text = SfzMetaParser._load(sfz_path, os.path.dirname(sfz_path))
        except OSError as exc:
            logger.warning("SfzMetaParser: cannot read %s: %s", sfz_path, exc)
            return info

        tokens = SfzMetaParser._tokenize(text)
        SfzMetaParser._build(tokens, info)
        return info

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _load(path: str, base_dir: str) -> str:
        """Load file text, expand single-level #include, strip // comments."""
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        out: List[str] = []
        for line in lines:
            # Strip comment.
            ci = line.find("//")
            if ci != -1:
                line = line[:ci]
            line_s = line.strip()
            # #include expansion (single level).
            m = re.match(r'#include\s+"([^"]+)"', line_s, re.IGNORECASE)
            if m:
                inc_path = os.path.join(base_dir, m.group(1).replace("\\", "/"))
                try:
                    out.append(SfzMetaParser._load(inc_path, base_dir))
                except OSError:
                    pass
                continue
            out.append(line)
        return "\n".join(out)

    @staticmethod
    def _tokenize(text: str) -> List[Tuple[str, str]]:
        """
        Return list of (type, value) tokens:
          ("header", "region") or ("header", "group")
          ("opcode", "key=value")
        """
        tokens: List[Tuple[str, str]] = []
        for m in re.finditer(r'<(\w+)>|(\w+)\s*=\s*([^\s<]+(?:\s[^\s=<]+)*)',
                             text):
            if m.group(1):
                tokens.append(("header", m.group(1).lower()))
            else:
                key = m.group(2).lower()
                val = m.group(3).strip()
                tokens.append(("opcode", f"{key}={val}"))
        return tokens

    @staticmethod
    def _apply_region(region: SfzRegionInfo, key: str, val: str) -> None:
        if   key == "lokey":        v = _parse_note(val); region.key_range.lo = v if v >= 0 else region.key_range.lo
        elif key == "hikey":        v = _parse_note(val); region.key_range.hi = v if v >= 0 else region.key_range.hi
        elif key == "key":          v = _parse_note(val); region.key_range.lo = region.key_range.hi = v if v >= 0 else region.key_range.lo
        elif key == "lovel":        region.vel_range.lo = int(val)
        elif key == "hivel":        region.vel_range.hi = int(val)
        elif key == "sample":       region.sample = val
        elif key == "volume":       region.volume = float(val)
        elif key == "pan":          region.pan = float(val)
        elif key == "seq_length":   region.seq_length = max(1, int(val))
        elif key == "seq_position": region.seq_position = max(1, int(val))
        elif key == "group":        region.group = int(val)

    @staticmethod
    def _build(tokens: List[Tuple[str, str]], info: SfzInstrumentInfo) -> None:
        ctx = "none"
        cur_group  = SfzGroupInfo()
        cur_region = SfzRegionInfo()

        def flush_region():
            nonlocal cur_region, ctx
            if ctx == "region":
                cur_group.regions.append(cur_region)
                cur_region = SfzRegionInfo()
                ctx = "group"

        def flush_group():
            nonlocal cur_group, ctx
            flush_region()
            if cur_group.regions:
                info.groups.append(cur_group)
            cur_group = SfzGroupInfo()
            ctx = "none"

        for typ, val in tokens:
            if typ == "header":
                if val == "group":
                    flush_group()
                    ctx = "group"
                elif val == "region":
                    if ctx == "none":
                        ctx = "group"
                    flush_region()
                    ctx = "region"
                continue

            # opcode
            try:
                key, v = val.split("=", 1)
            except ValueError:
                continue

            # CC label
            if key.startswith("label_cc"):
                try:
                    cc_num = int(key[8:])
                    info.cc_labels.append((cc_num, v))
                except ValueError:
                    pass
                continue

            if ctx == "region":
                try:
                    SfzMetaParser._apply_region(cur_region, key, v)
                except (ValueError, TypeError):
                    pass

        flush_group()

        # Flat region list and counts.
        for g in info.groups:
            info.regions.extend(g.regions)
        info.num_groups  = len(info.groups)
        info.num_regions = len(info.regions)
        info.cc_labels.sort(key=lambda x: x[0])


# ── Audio playback fallback ───────────────────────────────────────────────────

class SfizzEnginePython:
    """
    Minimal SFZ audio fallback using pygame.mixer.

    Supports single-sample playback per MIDI note (no polyphonic DSP,
    no round-robin, no disk-streaming).  Intended only for environments
    where the C++ sfizz extension is not available.
    """

    def __init__(self, sample_rate: float = 44100.0, block_size: int = 512) -> None:
        self._sample_rate = float(sample_rate)
        self._block_size  = int(block_size)
        self._meta: Optional[SfzInstrumentInfo] = None
        self._sfz_dir: str = ""
        # Map MIDI note → pygame Sound (loaded on demand).
        self._sounds: Dict[int, object] = {}
        self._loaded = False

    def set_sample_rate(self, sr: float) -> None:
        self._sample_rate = float(sr)

    def set_block_size(self, block_size: int) -> None:
        self._block_size = int(block_size)

    def load_sfz(self, path: str) -> bool:
        self._meta    = SfzMetaParser.parse(path)
        self._sfz_dir = os.path.dirname(path)
        self._sounds.clear()
        self._loaded  = bool(self._meta.num_regions)
        return self._loaded

    def is_loaded(self) -> bool:
        return self._loaded

    def get_metadata(self) -> Optional[SfzInstrumentInfo]:
        return self._meta

    def note_on(self, delay: int, note: int, velocity: int, channel: int = 0) -> None:
        sound = self._get_sound(note)
        if sound is not None:
            try:
                sound.set_volume(velocity / 127.0)
                sound.play()
            except Exception:
                pass

    def note_off(self, delay: int, note: int, velocity: int, channel: int = 0) -> None:
        sound = self._sounds.get(note)
        if sound is not None:
            try:
                sound.stop()
            except Exception:
                pass

    def control_change(self, delay: int, cc: int, cc_value: int, channel: int = 0) -> None:
        pass  # Not supported in the fallback.

    def pitch_wheel(self, delay: int, pitch: int, channel: int = 0) -> None:
        pass

    def aftertouch(self, delay: int, pressure: int, channel: int = 0) -> None:
        pass

    def all_notes_off(self, delay: int = 0) -> None:
        for sound in self._sounds.values():
            try:
                sound.stop()
            except Exception:
                pass

    def render(self, num_samples: int):
        """Returns (left, right) numpy arrays filled with silence."""
        import numpy as np
        zeros = np.zeros(num_samples, dtype=np.float32)
        return zeros, zeros.copy()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_sound(self, note: int):
        """Load and cache the pygame Sound for the closest sample."""
        if note in self._sounds:
            return self._sounds[note]
        region = self._find_region(note, 64)
        if region is None or not region.sample:
            return None
        sample_abs = os.path.join(self._sfz_dir, region.sample.replace("\\", "/"))
        if not os.path.isfile(sample_abs):
            return None
        try:
            import pygame.mixer as pgm
            sound = pgm.Sound(sample_abs)
            self._sounds[note] = sound
            return sound
        except Exception as exc:
            logger.debug("SfizzEnginePython: cannot load sample %s: %s", sample_abs, exc)
            return None

    def _find_region(self, note: int, velocity: int) -> Optional[SfzRegionInfo]:
        if not self._meta:
            return None
        for r in self._meta.regions:
            if r.key_range.lo <= note <= r.key_range.hi:
                if r.vel_range.lo <= velocity <= r.vel_range.hi:
                    return r
        # Fall back to key-only match.
        for r in self._meta.regions:
            if r.key_range.lo <= note <= r.key_range.hi:
                return r
        return None


# ── Factory ───────────────────────────────────────────────────────────────────

def get_sfz_engine(sample_rate: float = 44100.0, block_size: int = 512):
    """
    Return a dp.SfizzEngine (C++ sfizz) if available, else SfizzEnginePython.
    Both expose the same interface.
    """
    try:
        import daw_processors as dp   # type: ignore[import]
        if hasattr(dp, "SfizzEngine"):
            return dp.SfizzEngine(sample_rate, block_size)
    except (ImportError, OSError):
        pass
    logger.debug("SfizzEngine: C++ extension unavailable, using Python fallback")
    return SfizzEnginePython(sample_rate, block_size)


def parse_sfz(path: str) -> SfzInstrumentInfo:
    """
    Parse an SFZ file and return SfzInstrumentInfo metadata.
    Uses the C++ SfzParser if available, falls back to SfzMetaParser.
    """
    try:
        import daw_processors as dp   # type: ignore[import]
        if hasattr(dp, "SfzParser"):
            cpp_info = dp.SfzParser.parse(path)
            # Convert C++ struct to Python dataclass for consistent typing.
            info = SfzInstrumentInfo()
            info.name        = cpp_info.name
            info.path        = cpp_info.path
            info.num_regions = cpp_info.num_regions
            info.num_groups  = cpp_info.num_groups
            for r in cpp_info.regions:
                ri = SfzRegionInfo()
                ri.key_range    = SfzKeyRange(r.key_range.lo, r.key_range.hi)
                ri.vel_range    = SfzVelRange(r.vel_range.lo, r.vel_range.hi)
                ri.sample       = r.sample
                ri.volume       = r.volume
                ri.pan          = r.pan
                ri.seq_length   = r.seq_length
                ri.seq_position = r.seq_position
                info.regions.append(ri)
            info.cc_labels = list(cpp_info.cc_labels)
            return info
    except (ImportError, OSError):
        pass
    return SfzMetaParser.parse(path)
