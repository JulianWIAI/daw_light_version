"""
full_project_renderer_python.py -- Pure-Python fallback for FullProjectRenderer.
==================================================================================
Mirrors the C++ FullProjectRenderer API so the render pipeline works
identically whether the C++ extension is loaded or not.

Factory
-------
  from .full_project_renderer_python import get_full_project_renderer
  renderer = get_full_project_renderer()   # C++ or Python object

Both share the interface:
  prepare(n_frames, sample_rate)
  reset()
  mix_track(L, R, at_frame, volume, pan, vol_auto, pan_auto)
  get_L() -> np.ndarray float32
  get_R() -> np.ndarray float32
  get_n_frames() -> int
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class FullProjectRendererPython:
    """
    Pure-Python stereo offline mix bus.

    Accumulates track contributions into two float32 channel buffers.
    Per-frame volume / pan automation is applied via AutomationProcessor
    objects (C++ or Python fallback — both share the same fill_buffer API).
    """

    def __init__(self) -> None:
        self._L:  Optional[np.ndarray] = None
        self._R:  Optional[np.ndarray] = None
        self._n:  int = 0
        self._sr: int = 44100

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def prepare(self, n_frames: int, sample_rate: int) -> None:
        """Allocate the stereo mix bus.  Must be called before mix_track()."""
        self._n  = n_frames
        self._sr = max(1, sample_rate)
        self._L  = np.zeros(n_frames, dtype=np.float32)
        self._R  = np.zeros(n_frames, dtype=np.float32)

    def reset(self) -> None:
        """Zero the mix bus without reallocating."""
        if self._L is not None:
            self._L[:] = 0.0
        if self._R is not None:
            self._R[:] = 0.0

    # ── Mixing ────────────────────────────────────────────────────────────────

    def mix_track(
        self,
        L:         np.ndarray,
        R:         np.ndarray,
        at_frame:  int   = 0,
        volume:    float = 1.0,
        pan:       float = 0.0,
        vol_auto          = None,   # AutomationProcessorPython or C++ equivalent
        pan_auto          = None,
    ) -> None:
        """
        Accumulate one stereo track buffer into the mix bus.

        Parameters match the C++ FullProjectRenderer.mix_track() signature
        exactly so this class can replace it without any code changes in
        the render pipeline.
        """
        if self._L is None or self._R is None:
            logger.warning("FullProjectRendererPython: mix_track called before prepare()")
            return

        L = np.asarray(L, dtype=np.float32)
        R = np.asarray(R, dtype=np.float32)

        # How many frames fit in the mix bus from at_frame onward.
        available = self._n - at_frame
        if available <= 0:
            return
        n = min(len(L), available)
        if n <= 0:
            return

        end_frame  = at_frame + n
        start_secs = at_frame / float(self._sr)

        # ── Volume automation buffer ─────────────────────────────────────────
        if vol_auto is not None and vol_auto.has_points():
            vol_arr = vol_auto.fill_buffer(n, start_secs, float(self._sr))
        else:
            vol_arr = np.full(n, volume, dtype=np.float32)

        # ── Pan automation buffer ────────────────────────────────────────────
        if pan_auto is not None and pan_auto.has_points():
            pan_arr = pan_auto.fill_buffer(n, start_secs, float(self._sr))
        else:
            pan_arr = np.full(n, pan, dtype=np.float32)

        # ── Equal-power linear pan: g_L = vol*(1-pan), g_R = vol*(1+pan) ────
        g_l = vol_arr * np.maximum(0.0, 1.0 - pan_arr)
        g_r = vol_arr * np.maximum(0.0, 1.0 + pan_arr)

        self._L[at_frame:end_frame] += L[:n] * g_l
        self._R[at_frame:end_frame] += R[:n] * g_r

    # ── Output ────────────────────────────────────────────────────────────────

    def get_L(self) -> np.ndarray:
        """Return the left channel mix buffer as a float32 numpy array."""
        return self._L if self._L is not None else np.array([], dtype=np.float32)

    def get_R(self) -> np.ndarray:
        """Return the right channel mix buffer as a float32 numpy array."""
        return self._R if self._R is not None else np.array([], dtype=np.float32)

    def get_n_frames(self) -> int:
        """Total allocated frame count."""
        return self._n


# ── Factory ───────────────────────────────────────────────────────────────────

def get_full_project_renderer() -> FullProjectRendererPython:
    """
    Return a C++ FullProjectRenderer if available, otherwise Python fallback.

    Both objects expose the same interface (prepare / reset / mix_track /
    get_L / get_R / get_n_frames) so callers need no conditionals.
    """
    try:
        import daw_processors as dp  # type: ignore[import]
        if hasattr(dp, "FullProjectRenderer"):
            return dp.FullProjectRenderer()
    except (ImportError, OSError):
        pass
    return FullProjectRendererPython()
