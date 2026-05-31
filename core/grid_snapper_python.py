"""
grid_snapper_python.py  --  Python fallback for the C++ GridSnapper system.
============================================================================
Pure-Python / stdlib implementation that mirrors the four C++ modules:
  GridDefinition, QuantizeEngine, TimelineRuler, GridSnapper.

Used automatically when the daw_processors C++ extension is unavailable.

Factory:
    get_grid_snapper()  →  C++ GridSnapper wrapper  *or*  GridSnapperPython
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

# ── PPQN constant ────────────────────────────────────────────────────────────
PPQN: int = 960   # ticks per quarter note


# ─────────────────────────────────────────────────────────────────────────────
# GridDefinition (Python)
# ─────────────────────────────────────────────────────────────────────────────

# All supported grid values stored as (label, type_tag, ticks, beats).
# beats = ticks / PPQN (quarter note = 1 beat).
_GRID_TABLE: List[Dict] = [
    # Straight notes
    {"label": "1/1",   "type": "straight", "ticks": 3840, "beats": 3840 / PPQN},
    {"label": "1/2",   "type": "straight", "ticks": 1920, "beats": 1920 / PPQN},
    {"label": "1/4",   "type": "straight", "ticks":  960, "beats":  960 / PPQN},
    {"label": "1/8",   "type": "straight", "ticks":  480, "beats":  480 / PPQN},
    {"label": "1/16",  "type": "straight", "ticks":  240, "beats":  240 / PPQN},
    {"label": "1/32",  "type": "straight", "ticks":  120, "beats":  120 / PPQN},
    {"label": "1/64",  "type": "straight", "ticks":   60, "beats":   60 / PPQN},
    {"label": "1/128", "type": "straight", "ticks":   30, "beats":   30 / PPQN},
    # Triplet notes (2/3 of straight ticks)
    {"label": "1/4T",  "type": "triplet",  "ticks": 640, "beats": 640 / PPQN},
    {"label": "1/8T",  "type": "triplet",  "ticks": 320, "beats": 320 / PPQN},
    {"label": "1/16T", "type": "triplet",  "ticks": 160, "beats": 160 / PPQN},
    {"label": "1/32T", "type": "triplet",  "ticks":  80, "beats":  80 / PPQN},
    {"label": "1/64T", "type": "triplet",  "ticks":  40, "beats":  40 / PPQN},
    # Dotted notes (3/2 of straight ticks)
    {"label": "1/4D",  "type": "dotted",   "ticks": 1440, "beats": 1440 / PPQN},
    {"label": "1/8D",  "type": "dotted",   "ticks":  720, "beats":  720 / PPQN},
    {"label": "1/16D", "type": "dotted",   "ticks":  360, "beats":  360 / PPQN},
    {"label": "1/32D", "type": "dotted",   "ticks":  180, "beats":  180 / PPQN},
    {"label": "1/64D", "type": "dotted",   "ticks":   90, "beats":   90 / PPQN},
    # Free / single-tick grid
    {"label": "Free",  "type": "free",     "ticks":    1, "beats":   1 / PPQN},
]

# Fast lookup by label.
_GRID_BY_LABEL: Dict[str, Dict] = {g["label"]: g for g in _GRID_TABLE}


class GridDefinitionPython:
    """Static helpers mirroring C++ GridDefinition."""

    @staticmethod
    def all_grids() -> List[Dict]:
        return _GRID_TABLE

    @staticmethod
    def find(label: str) -> Optional[Dict]:
        return _GRID_BY_LABEL.get(label)

    @staticmethod
    def ticks_to_beats(ticks: int) -> float:
        return ticks / PPQN

    @staticmethod
    def beats_to_ticks(beats: float) -> int:
        return int(beats * PPQN)


# ─────────────────────────────────────────────────────────────────────────────
# QuantizeEngine (Python)
# ─────────────────────────────────────────────────────────────────────────────

def _round6(v: float) -> float:
    """Round to 6 decimal places to suppress floating-point drift."""
    return round(v * 1_000_000) / 1_000_000


class QuantizeEnginePython:
    """Stateless snap/quantize helpers mirroring C++ QuantizeEngine."""

    @staticmethod
    def snap_nearest(beat: float, grid_beats: float) -> float:
        if grid_beats <= 0.0:
            return beat
        return max(0.0, _round6(round(beat / grid_beats) * grid_beats))

    @staticmethod
    def snap_floor(beat: float, grid_beats: float) -> float:
        if grid_beats <= 0.0:
            return beat
        return max(0.0, _round6(math.floor(beat / grid_beats) * grid_beats))

    @staticmethod
    def snap_ceil(beat: float, grid_beats: float) -> float:
        if grid_beats <= 0.0:
            return beat
        return max(0.0, _round6(math.ceil(beat / grid_beats) * grid_beats))

    @staticmethod
    def quantize(beat: float, grid_beats: float, strength: float) -> float:
        if grid_beats <= 0.0 or strength <= 0.0:
            return beat
        strength = max(0.0, min(1.0, strength))
        snapped = QuantizeEnginePython.snap_nearest(beat, grid_beats)
        return beat + strength * (snapped - beat)

    @staticmethod
    def grid_positions(start_beat: float, end_beat: float,
                       grid_beats: float) -> List[float]:
        if grid_beats <= 0.0 or end_beat < start_beat:
            return []
        first = QuantizeEnginePython.snap_ceil(start_beat, grid_beats)
        positions: List[float] = []
        pos = first
        safety = 100_000
        while pos <= end_beat + 1e-9 and safety > 0:
            positions.append(pos)
            pos = _round6(pos + grid_beats)
            safety -= 1
        return positions


# ─────────────────────────────────────────────────────────────────────────────
# TimelineRuler (Python)
# ─────────────────────────────────────────────────────────────────────────────

class RulerLabel:
    """Mirrors C++ RulerLabel struct."""
    __slots__ = ("beat", "text", "is_major")

    def __init__(self, beat: float, text: str, is_major: bool) -> None:
        self.beat     = beat
        self.text     = text
        self.is_major = is_major


class TimelineRulerPython:
    """Ruler label formatter mirroring C++ TimelineRuler."""

    @staticmethod
    def _beats_to_seconds(beat: float, bpm: float) -> float:
        if bpm <= 0.0:
            bpm = 120.0
        return beat * 60.0 / bpm

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        if seconds < 0.0:
            seconds = 0.0
        total_ms = int(round(seconds * 1000.0))
        ms  = total_ms % 1000
        sec = (total_ms // 1000) % 60
        mn  = total_ms // 60_000
        return f"{mn}:{sec:02d}.{ms:03d}"

    @staticmethod
    def _fmt_smpte(seconds: float, fps: float) -> str:
        if seconds < 0.0:
            seconds = 0.0
        if fps <= 0.0:
            fps = 30.0
        is_df = abs(fps - 29.97) < 0.01
        if is_df:
            real_fps = 30000 / 1001
            total_f  = int(seconds * real_fps)
            ff = total_f % 30
            ss = (total_f // 30) % 60
            mm = (total_f // 1800) % 60
            hh = total_f // 108_000
            return f"{hh:02d};{mm:02d};{ss:02d};{ff:02d}"
        int_fps   = int(round(fps))
        total_f   = int(seconds * fps)
        ff = total_f % int_fps
        ss = (total_f // int_fps) % 60
        mm = (total_f // (int_fps * 60)) % 60
        hh = total_f // (int_fps * 3600)
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

    @staticmethod
    def format_bars_beats(beat: float, time_sig: int = 4) -> str:
        if time_sig <= 0:
            time_sig = 4
        bar      = int(beat / time_sig) + 1
        in_bar   = beat - (bar - 1) * time_sig
        beat_num = int(in_bar) + 1
        if beat_num <= 1:
            return f"BAR {bar}"
        return f"{bar}:{beat_num}"

    @staticmethod
    def format_time(beat: float, bpm: float) -> str:
        return TimelineRulerPython._fmt_time(
            TimelineRulerPython._beats_to_seconds(beat, bpm))

    @staticmethod
    def format_smpte(beat: float, bpm: float, fps: float) -> str:
        return TimelineRulerPython._fmt_smpte(
            TimelineRulerPython._beats_to_seconds(beat, bpm), fps)

    @staticmethod
    def ruler_labels(start_beat: float, end_beat: float,
                     pixels_per_beat: float, bpm: float, mode: str,
                     fps: float = 30.0, time_sig: int = 4) -> List[RulerLabel]:
        labels: List[RulerLabel] = []
        if pixels_per_beat <= 0.0 or end_beat < start_beat:
            return labels
        if time_sig <= 0:
            time_sig = 4

        # Choose label stride so adjacent labels stay >= 60 px apart.
        MIN_PX = 60.0
        stride = 1.0
        while stride * pixels_per_beat < MIN_PX:
            stride *= 2.0

        bar_beats = float(time_sig)
        if stride >= bar_beats:
            stride = math.ceil(stride / bar_beats) * bar_beats

        first = math.ceil(start_beat / stride) * stride
        pos   = first
        safety = 10_000
        while pos <= end_beat + 1e-9 and safety > 0:
            is_major = (abs(pos % bar_beats) < 1e-6)
            if mode == "Time":
                text = TimelineRulerPython.format_time(pos, bpm)
            elif mode == "SMPTE":
                text = TimelineRulerPython.format_smpte(pos, bpm, fps)
            else:
                text = TimelineRulerPython.format_bars_beats(pos, time_sig)
            labels.append(RulerLabel(pos, text, is_major))
            pos = round((pos + stride) * 1_000_000) / 1_000_000
            safety -= 1
        return labels


# ─────────────────────────────────────────────────────────────────────────────
# GridSnapperPython  --  mirrors C++ GridSnapper
# ─────────────────────────────────────────────────────────────────────────────

class GridSnapperPython:
    """
    High-level grid snap + ruler interface.

    All public methods mirror the C++ GridSnapper interface so that the
    GUI code works identically with both backends.
    """

    def __init__(self) -> None:
        self._label      = "1/16"
        self._grid_beats = 0.25
        self._strength   = 1.0
        self._ruler_mode = "BarsBeats"
        self._fps        = 30.0
        self._bpm        = 120.0
        self._time_sig   = 4
        self._refresh()

    # ── Configuration ─────────────────────────────────────────────────────────

    def set_grid(self, label: str) -> None:
        gv = GridDefinitionPython.find(label)
        if gv is not None:
            self._label      = label
            self._grid_beats = gv["beats"]

    def grid_label(self) -> str:
        return self._label

    def grid_beats(self) -> float:
        return self._grid_beats

    def set_strength(self, s: float) -> None:
        self._strength = max(0.0, min(1.0, s))

    def strength(self) -> float:
        return self._strength

    def set_ruler_mode(self, mode: str) -> None:
        self._ruler_mode = mode if mode in ("Time", "SMPTE") else "BarsBeats"

    def ruler_mode_str(self) -> str:
        return self._ruler_mode

    def set_fps(self, fps: float) -> None:
        self._fps = fps if fps > 0.0 else 30.0

    def fps(self) -> float:
        return self._fps

    def set_bpm(self, bpm: float) -> None:
        self._bpm = bpm if bpm > 0.0 else 120.0

    def bpm(self) -> float:
        return self._bpm

    def set_time_sig(self, beats_per_bar: int) -> None:
        self._time_sig = beats_per_bar if beats_per_bar > 0 else 4

    def time_sig(self) -> int:
        return self._time_sig

    # ── Grid operations ───────────────────────────────────────────────────────

    def snap(self, beat: float) -> float:
        return QuantizeEnginePython.quantize(beat, self._grid_beats, self._strength)

    def grid_lines(self, start_beat: float, end_beat: float) -> List[float]:
        return QuantizeEnginePython.grid_positions(start_beat, end_beat,
                                                   self._grid_beats)

    # ── Ruler labels ──────────────────────────────────────────────────────────

    def ruler_labels(self, start_beat: float, end_beat: float,
                     pixels_per_beat: float) -> List[RulerLabel]:
        return TimelineRulerPython.ruler_labels(
            start_beat, end_beat, pixels_per_beat, self._bpm,
            self._ruler_mode, self._fps, self._time_sig)

    def format_position(self, beat: float) -> str:
        if self._ruler_mode == "Time":
            return TimelineRulerPython.format_time(beat, self._bpm)
        if self._ruler_mode == "SMPTE":
            return TimelineRulerPython.format_smpte(beat, self._bpm, self._fps)
        return TimelineRulerPython.format_bars_beats(beat, self._time_sig)

    def _refresh(self) -> None:
        gv = GridDefinitionPython.find(self._label)
        if gv is not None:
            self._grid_beats = gv["beats"]


# ─────────────────────────────────────────────────────────────────────────────
# Factory: prefer C++, fall back to Python
# ─────────────────────────────────────────────────────────────────────────────

def get_grid_snapper() -> object:
    """
    Return a GridSnapper instance, preferring the C++ backend.

    The returned object always exposes:
        .set_grid(label)
        .grid_label() -> str
        .grid_beats()  -> float
        .set_strength(s)
        .set_ruler_mode(mode)
        .set_bpm(bpm)
        .set_fps(fps)
        .set_time_sig(n)
        .snap(beat) -> float
        .grid_lines(start, end) -> list[float]
        .ruler_labels(start, end, ppb) -> list[RulerLabel-like]
        .format_position(beat) -> str
    """
    try:
        import daw_processors as dp  # type: ignore[import]

        class _CppWrapper:
            """Thin wrapper giving C++ GridSnapper a uniform Python API."""

            def __init__(self) -> None:
                self._s = dp.GridSnapper()

            def set_grid(self, label: str)       -> None:   self._s.set_grid(label)
            def grid_label(self)                  -> str:    return self._s.grid_label()
            def grid_beats(self)                  -> float:  return self._s.grid_beats()
            def set_strength(self, s: float)      -> None:   self._s.set_strength(s)
            def strength(self)                    -> float:  return self._s.strength()
            def set_ruler_mode(self, m: str)      -> None:   self._s.set_ruler_mode(m)
            def ruler_mode_str(self)              -> str:    return self._s.ruler_mode_str()
            def set_fps(self, fps: float)         -> None:   self._s.set_fps(fps)
            def fps(self)                         -> float:  return self._s.fps()
            def set_bpm(self, bpm: float)         -> None:   self._s.set_bpm(bpm)
            def bpm(self)                         -> float:  return self._s.bpm()
            def set_time_sig(self, n: int)        -> None:   self._s.set_time_sig(n)
            def time_sig(self)                    -> int:    return self._s.time_sig()
            def snap(self, beat: float)           -> float:  return self._s.snap(beat)
            def grid_lines(self, s: float,
                           e: float)              -> List[float]: return list(self._s.grid_lines(s, e))
            def ruler_labels(self, s: float,
                             e: float,
                             ppb: float)          -> list:   return list(self._s.ruler_labels(s, e, ppb))
            def format_position(self, b: float)  -> str:    return self._s.format_position(b)

        return _CppWrapper()

    except Exception:
        return GridSnapperPython()


# Module-level convenience accessor for the GUI layer.
# The GUI imports this and uses it as the single shared snapper.
_DEFAULT_SNAPPER: Optional[object] = None


def default_snapper() -> object:
    """Return the process-level shared GridSnapper, creating it on first call."""
    global _DEFAULT_SNAPPER
    if _DEFAULT_SNAPPER is None:
        _DEFAULT_SNAPPER = get_grid_snapper()
    return _DEFAULT_SNAPPER


def all_grid_labels() -> List[str]:
    """Return the ordered list of all grid labels for populating the GUI."""
    return [g["label"] for g in _GRID_TABLE]
