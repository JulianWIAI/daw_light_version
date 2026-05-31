"""
automation_processor_python.py -- Pure-Python fallback for AutomationProcessor.
================================================================================
Mirrors the C++ AutomationProcessor API exactly so the rest of the pipeline
can call either backend without any conditional branching.

Used when the C++ daw_processors extension is unavailable (Win32 error 193
or missing MSVC runtime).

Factory
-------
  from .automation_processor_python import get_automation_processor
  auto = get_automation_processor()   # returns C++ or Python object

Both return objects share the same interface:
  add_point(time_secs, value)
  clear_points()
  has_points() -> bool
  value_at(time_secs) -> float
  fill_buffer(n_frames, start_secs, sample_rate) -> np.ndarray float32
"""

from __future__ import annotations

import bisect
import logging
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class AutomationProcessorPython:
    """
    Piecewise-linear automation curve stored as sorted (time_secs, value) pairs.

    Behaviour matches the C++ AutomationProcessor exactly:
      - Before the first point: hold first value.
      - After  the last  point: hold last  value.
      - Between two points    : linear interpolation.
      - Empty                 : value_at() returns 0.0.
    """

    def __init__(self) -> None:
        # Sorted list of (time_secs, value) tuples.
        self._points: List[Tuple[float, float]] = []

    # ── Population ────────────────────────────────────────────────────────────

    def add_point(self, time_secs: float, value: float) -> None:
        """Insert one control point.  Out-of-order inserts are allowed."""
        # bisect_left keeps the list sorted by time_secs.
        idx = bisect.bisect_left(self._points, (time_secs, value))
        self._points.insert(idx, (time_secs, value))

    def clear_points(self) -> None:
        """Remove all control points."""
        self._points.clear()

    def has_points(self) -> bool:
        """True if at least one control point exists."""
        return len(self._points) > 0

    # ── Query ─────────────────────────────────────────────────────────────────

    def value_at(self, time_secs: float) -> float:
        """Return linearly-interpolated value at time_secs."""
        pts = self._points
        if not pts:
            return 0.0
        if len(pts) == 1:
            return pts[0][1]

        # Clamp to range.
        if time_secs <= pts[0][0]:
            return pts[0][1]
        if time_secs >= pts[-1][0]:
            return pts[-1][1]

        # Binary search for the bracketing segment.
        idx = bisect.bisect_left(pts, (time_secs, float("-inf")))
        idx = max(1, min(idx, len(pts) - 1))  # ensure [prev, idx] is valid

        t0, v0 = pts[idx - 1]
        t1, v1 = pts[idx]
        alpha = (time_secs - t0) / (t1 - t0)
        return v0 + alpha * (v1 - v0)

    def fill_buffer(
        self,
        n_frames:    int,
        start_secs:  float,
        sample_rate: float,
    ) -> np.ndarray:
        """
        Return a (n_frames,) float32 array where out[i] = value_at(start_secs + i/sr).

        Uses vectorised numpy where possible:
          - Empty or single-point: fast constant-fill.
          - Multi-point          : per-sample interpolation via np.interp.
        """
        if not self._points:
            return np.zeros(n_frames, dtype=np.float32)

        if len(self._points) == 1:
            return np.full(n_frames, self._points[0][1], dtype=np.float32)

        # Build sample-time axis.
        t = start_secs + np.arange(n_frames, dtype=np.float64) / sample_rate

        # Unpack points into parallel arrays for np.interp.
        times  = np.array([p[0] for p in self._points], dtype=np.float64)
        values = np.array([p[1] for p in self._points], dtype=np.float64)

        # np.interp clamps to edge values outside the range — matches C++ behaviour.
        return np.interp(t, times, values).astype(np.float32)


# ── Factory ───────────────────────────────────────────────────────────────────

def get_automation_processor() -> AutomationProcessorPython:
    """
    Return a C++ AutomationProcessor if available, otherwise Python fallback.

    Both objects expose the same interface so callers need no conditionals.
    """
    try:
        import daw_processors as dp  # type: ignore[import]
        if hasattr(dp, "AutomationProcessor"):
            return dp.AutomationProcessor()
    except (ImportError, OSError):
        pass
    return AutomationProcessorPython()
