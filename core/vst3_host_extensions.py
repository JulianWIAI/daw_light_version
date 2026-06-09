"""
vst3_host_extensions.py -- Python bridge for VST3 advanced hosting features.
=============================================================================
Wraps the C++ Vst3StateManager, Vst3AutomationQueue, and Vst3TransportContext
classes exposed via daw_processors.  Provides:

  Vst3StateStore        -- save/restore/serialize plugin state for project files
  Vst3AutomationCurve   -- build per-block sample-accurate automation data
  Vst3TransportBridge   -- manage tempo/transport state fed to every plugin

All classes degrade gracefully: if daw_processors is not available or was
built without HAVE_VST3_SDK, they fall back to no-op Python stubs so the
rest of the GUI does not need conditional logic.

----------------------------------------------------------------------
Quick start — state save/restore:

    from core.vst3_host_extensions import Vst3StateStore

    store = Vst3StateStore()
    # (your C++ host sets comp_ptr and ctrl_ptr via ctypes/existing bindings)
    state_bytes = store.save_from_ptrs(comp_ptr, ctrl_ptr)   # bytes
    # ... store state_bytes in project JSON as base64 ...
    store.restore_to_ptrs(comp_ptr, ctrl_ptr, state_bytes)

----------------------------------------------------------------------
Quick start — automation:

    from core.vst3_host_extensions import Vst3AutomationCurve

    curve = Vst3AutomationCurve()
    curve.add_ramp(param_id=0x10001,
                   start_beat=0.0, end_beat=4.0,
                   start_value=0.0, end_value=1.0,
                   bpm=120.0, sample_rate=44100.0, block_size=512)

    # Per block in the audio loop:
    block_points = curve.points_for_block(block_index=0, block_size=512)
    for param_id, offset, value in block_points:
        queue.add_point(param_id, offset, value)

----------------------------------------------------------------------
Quick start — transport:

    from core.vst3_host_extensions import Vst3TransportBridge

    transport = Vst3TransportBridge(sample_rate=44100.0)
    transport.set_tempo(128.0)
    transport.play()

    # Per block:
    transport.advance(512)
    print(f"Beat: {transport.beat_position:.2f}")
"""

from __future__ import annotations

import base64
import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Try to import the C++ daw_processors extension ───────────────────────────

try:
    import daw_processors as _dp                            # type: ignore[import]
    _HAS_VST3 = hasattr(_dp, "Vst3TransportContext")
except (ImportError, OSError):
    _dp      = None                                         # type: ignore[assignment]
    _HAS_VST3 = False

if not _HAS_VST3:
    logger.debug(
        "vst3_host_extensions: C++ VST3 extension not available — "
        "using Python stubs (no-op)."
    )


# ── Vst3StateStore ─────────────────────────────────────────────────────────────

class Vst3StateStore:
    """
    Save and restore the complete state of a VST3 plugin.

    The actual IComponent* / IEditController* COM pointers must come from
    your existing C++ VST3 host.  Pass them as integers (ctypes pointer values)
    or via whatever binding your host exposes.

    State is serialised as a base64-encoded JSON field for project files:
        { "vst3_state": "<base64 bytes>" }
    """

    def serialize(self, state_bytes: bytes) -> str:
        """Encode raw state bytes to a base64 string for JSON storage."""
        return base64.b64encode(state_bytes).decode("ascii")

    def deserialize(self, encoded: str) -> bytes:
        """Decode a base64 string back to raw state bytes."""
        return base64.b64decode(encoded)

    def pack_state_bytes(self, processor_bytes: bytes, controller_bytes: bytes) -> bytes:
        """
        Pack processor + controller bytes into the wire format used by
        Vst3StateManager::serialize() (little-endian length-prefix).
        """
        import struct
        return (struct.pack("<I", len(processor_bytes))
                + processor_bytes
                + struct.pack("<I", len(controller_bytes))
                + controller_bytes)

    def unpack_state_bytes(self, data: bytes) -> Tuple[bytes, bytes]:
        """
        Unpack bytes produced by pack_state_bytes() into (processor, controller).
        Returns (b"", b"") on format error.
        """
        import struct
        if len(data) < 4:
            return b"", b""
        proc_len = struct.unpack_from("<I", data, 0)[0]
        if 4 + proc_len + 4 > len(data):
            return b"", b""
        proc = data[4: 4 + proc_len]
        offset = 4 + proc_len
        ctrl_len = struct.unpack_from("<I", data, offset)[0]
        ctrl = data[offset + 4: offset + 4 + ctrl_len]
        return proc, ctrl

    # ── C++ delegation ────────────────────────────────────────────────────────
    # These methods require the C++ Vst3StateManager to be available AND
    # require raw pointer values from the C++ host.

    def cpp_serialize(self, vst3_plugin_state) -> bytes:
        """
        Serialize a dp.Vst3PluginState object → bytes.
        Requires the C++ extension with HAVE_VST3_SDK.
        """
        if not _HAS_VST3:
            return b""
        return bytes(_dp.Vst3StateManager.serialize(vst3_plugin_state))

    def cpp_deserialize(self, data: bytes):
        """
        Deserialize bytes → dp.Vst3PluginState.
        Requires the C++ extension with HAVE_VST3_SDK.
        """
        if not _HAS_VST3:
            return None
        return _dp.Vst3StateManager.deserialize(list(data))

    def to_project_dict(self, vst3_plugin_state) -> Dict[str, str]:
        """Convert a dp.Vst3PluginState to a JSON-serialisable dict."""
        raw = self.cpp_serialize(vst3_plugin_state)
        return {"vst3_state": self.serialize(raw)}

    def from_project_dict(self, d: Dict[str, str]):
        """Reconstruct a dp.Vst3PluginState from a project-file dict."""
        raw = self.deserialize(d.get("vst3_state", ""))
        return self.cpp_deserialize(raw)


# ── Vst3AutomationCurve ────────────────────────────────────────────────────────

class Vst3AutomationCurve:
    """
    Build sample-accurate VST3 parameter automation data for the audio thread.

    Internally stores (param_id, beat_position, value) breakpoints and can
    generate the per-block (param_id, sample_offset, value) tuples required
    by Vst3AutomationQueue.add_point().

    Values are always normalised [0.0, 1.0] as required by the VST3 spec.
    """

    def __init__(self) -> None:
        # List of (param_id, beat_pos, normalized_value).
        self._points: List[Tuple[int, float, float]] = []

    def clear(self) -> None:
        """Remove all breakpoints."""
        self._points.clear()

    def add_point(self, param_id: int, beat_pos: float, value: float) -> None:
        """Add a single normalised automation breakpoint."""
        value = max(0.0, min(1.0, float(value)))
        self._points.append((param_id, float(beat_pos), value))
        self._points.sort(key=lambda p: (p[0], p[1]))

    def add_ramp(self, param_id: int,
                 start_beat: float, end_beat: float,
                 start_value: float, end_value: float,
                 num_steps: int = 16) -> None:
        """
        Add a linear ramp from start_value to end_value over the beat range.
        num_steps controls how many breakpoints are inserted.
        """
        for i in range(num_steps + 1):
            t = i / num_steps
            beat  = start_beat + t * (end_beat - start_beat)
            value = start_value + t * (end_value - start_value)
            self.add_point(param_id, beat, value)

    def points_for_block(
        self,
        block_start_beat: float,
        block_end_beat: float,
        block_size: int,
        bpm: float,
        sample_rate: float,
    ) -> List[Tuple[int, int, float]]:
        """
        Return (param_id, sample_offset, value) tuples for all breakpoints
        that fall within [block_start_beat, block_end_beat).

        sample_offset is the integer sample position within the current block.
        """
        if bpm <= 0 or sample_rate <= 0 or block_size <= 0:
            return []

        beats_per_sample = bpm / (60.0 * sample_rate)
        result: List[Tuple[int, int, float]] = []

        for param_id, beat, value in self._points:
            if block_start_beat <= beat < block_end_beat:
                beat_offset   = beat - block_start_beat
                sample_offset = int(beat_offset / beats_per_sample)
                sample_offset = max(0, min(sample_offset, block_size - 1))
                result.append((param_id, sample_offset, value))

        return result

    def populate_cpp_queue(
        self,
        queue,                    # dp.Vst3AutomationQueue instance
        block_start_beat: float,
        block_end_beat: float,
        block_size: int,
        bpm: float,
        sample_rate: float,
    ) -> None:
        """
        Convenience method: populate a dp.Vst3AutomationQueue for one block.
        Clears the queue first, then adds all in-block breakpoints.
        """
        if queue is None:
            return
        queue.clear()
        for param_id, sample_offset, value in self.points_for_block(
            block_start_beat, block_end_beat, block_size, bpm, sample_rate
        ):
            queue.add_point(param_id, sample_offset, value)


# ── Vst3TransportBridge ────────────────────────────────────────────────────────

class Vst3TransportBridge:
    """
    Python-side manager for VST3 transport / tempo sync.

    Wraps dp.Vst3TransportContext when available; otherwise provides a
    pure-Python implementation that tracks the same state so the rest of
    the codebase can call the same API regardless.
    """

    def __init__(self, sample_rate: float = 44100.0) -> None:
        if _HAS_VST3:
            self._ctx = _dp.Vst3TransportContext()
            self._ctx.set_sample_rate(sample_rate)
            self._cpp = True
        else:
            # Pure-Python fallback state.
            self._ctx    = None
            self._cpp    = False
            self._sr     = float(sample_rate)
            self._bpm    = 120.0
            self._num    = 4
            self._denom  = 4
            self._pos    = 0       # sample position
            self._playing    = False
            self._cycling    = False
            self._recording  = False

    # ── Configuration ─────────────────────────────────────────────────────────

    def set_sample_rate(self, sr: float) -> None:
        if self._cpp:
            self._ctx.set_sample_rate(sr)
        else:
            self._sr = float(sr)

    def set_tempo(self, bpm: float) -> None:
        if self._cpp:
            self._ctx.set_tempo(bpm)
        else:
            self._bpm = float(bpm)

    def set_time_signature(self, numerator: int, denominator: int) -> None:
        if self._cpp:
            self._ctx.set_time_signature(numerator, denominator)
        else:
            self._num   = numerator
            self._denom = denominator

    # ── Transport state ───────────────────────────────────────────────────────

    def play(self) -> None:
        if self._cpp:
            self._ctx.set_playing(True)
        else:
            self._playing = True

    def stop(self) -> None:
        if self._cpp:
            self._ctx.set_playing(False)
        else:
            self._playing = False

    def record(self, on: bool) -> None:
        if self._cpp:
            self._ctx.set_recording(on)
        else:
            self._recording = on

    def set_cycling(self, on: bool) -> None:
        if self._cpp:
            self._ctx.set_cycling(on)
        else:
            self._cycling = on

    # ── Per-block update ──────────────────────────────────────────────────────

    def advance(self, num_samples: int) -> None:
        """Advance the transport position by num_samples.  Call once per block."""
        if self._cpp:
            self._ctx.advance(num_samples)
        else:
            self._pos += num_samples

    def seek(self, sample_pos: int) -> None:
        """Jump to an absolute sample position."""
        if self._cpp:
            self._ctx.set_sample_position(sample_pos)
        else:
            self._pos = int(sample_pos)

    def reset(self) -> None:
        if self._cpp:
            self._ctx.reset()
        else:
            self._pos = 0

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def sample_position(self) -> int:
        if self._cpp:
            return int(self._ctx.sample_position)
        return self._pos

    @property
    def beat_position(self) -> float:
        if self._cpp:
            return float(self._ctx.beat_position)
        if self._sr > 0 and self._bpm > 0:
            return (self._pos / self._sr) * (self._bpm / 60.0)
        return 0.0

    @property
    def tempo(self) -> float:
        if self._cpp:
            return float(self._ctx.tempo)
        return self._bpm

    @property
    def is_playing(self) -> bool:
        if self._cpp:
            return bool(getattr(self._ctx, "is_playing", False))
        return self._playing

    # ── C++ context pointer (for passing to process()) ────────────────────────

    def get_cpp_context(self) -> Optional[Any]:
        """Return the underlying dp.Vst3TransportContext, or None."""
        return self._ctx if self._cpp else None
