"""
master_bus_python.py -- Pure-Python fallback for the C++ MasterBus.
=====================================================================
Mirrors the C++ MasterBus interface exactly so the rest of the codebase
works identically whether or not the C++ daw_processors extension is loaded.

Audition mode constants match AuditionMode enum in AuditionProcessor.h:
  AUDITION_BYPASS    = 0  -- Normal user FX chain + user limiter.
  AUDITION_PREVIEW   = 1  -- Simulate -7  LUFS (+7 dB pre-gain, -1 dBFS ceiling).
  AUDITION_STREAMING = 2  -- Simulate -14 LUFS ( 0 dB pre-gain, -1 dBFS ceiling).

Factory
-------
  from .master_bus_python import get_master_bus, AUDITION_BYPASS, ...
  bus = get_master_bus()    # returns dp.MasterBus or MasterBusPython
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Audition mode integer constants (match C++ AuditionMode enum) ─────────────
# Re-exported so GUI code can import them from here rather than using raw ints.

AUDITION_BYPASS    = 0   # Normal path: user FX chain + user limiter.
AUDITION_PREVIEW   = 1   # -7  LUFS simulation: +7 dB pre-gain, -1 dBFS ceiling.
AUDITION_STREAMING = 2   # -14 LUFS simulation:  0 dB pre-gain, -1 dBFS ceiling.

# Pre-computed linear gain / ceiling constants for the audition paths.
_PREVIEW_GAIN_LIN   = 10.0 ** (7.0 / 20.0)   # +7 dB linear factor ≈ 2.239
_AUDITION_CEIL_LIN  = 10.0 ** (-1.0 / 20.0)  # -1.0 dBFS hard-clip ceiling ≈ 0.891


class MasterBusPython:
    """
    Pure-Python stereo master bus with audition mode routing.

    Provides the same interface as the C++ MasterBus class so callers need
    no conditional logic.  The audition mode processing is a simplified
    version of the C++ path: hard-clip instead of Catmull-Rom true-peak
    limiting, which is safe but less transparent on transients.
    """

    def __init__(self, sample_rate: float = 44100.0) -> None:
        self._sr:           float               = float(sample_rate)
        self._n:            int                 = 0
        self._L:            Optional[np.ndarray] = None
        self._R:            Optional[np.ndarray] = None
        self._gain:         float               = 1.0
        self._ceiling_db:   float               = -0.1
        self._limiter_on:   bool                = True
        self._audition_mode: int                = AUDITION_BYPASS
        self._peak_L:       float               = 0.0
        self._peak_R:       float               = 0.0
        self._peak_decay:   float               = 0.9   # per-block coefficient

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def prepare(self, n_frames: int, sample_rate: float) -> None:
        """Allocate internal buffers and recalculate peak-decay coefficient."""
        self._n  = int(n_frames)
        self._sr = float(sample_rate)
        self._L  = np.zeros(self._n, dtype=np.float32)
        self._R  = np.zeros(self._n, dtype=np.float32)
        # Peak decay: ~200 ms release time expressed as a per-block multiplier.
        block_dur        = self._n / max(1.0, self._sr)
        self._peak_decay = float(np.exp(-block_dur / 0.2))
        self._peak_L = 0.0
        self._peak_R = 0.0

    def reset(self) -> None:
        """Zero the sum buffers. Call once per block before add_track()."""
        if self._L is not None:
            self._L[:] = 0.0
            self._R[:] = 0.0

    # ── Summing ───────────────────────────────────────────────────────────────

    def add_track(self, L: np.ndarray, R: np.ndarray) -> None:
        """Accumulate one stereo float32 track into the sum bus."""
        if self._L is None:
            return
        n = min(len(L), len(R), self._n)
        if n <= 0:
            return
        self._L[:n] += np.asarray(L[:n], dtype=np.float32)
        self._R[:n] += np.asarray(R[:n], dtype=np.float32)

    # ── Processing ────────────────────────────────────────────────────────────

    def process(self) -> None:
        """
        Apply master gain, route through the active audition path, and update
        peak meters.

        Routing:
          BYPASS    -> user BrickwallLimiter (hard-clip fallback).
          PREVIEW   -> +7 dB pre-gain + hard-clip at -1 dBFS.
          STREAMING ->  0 dB pre-gain + hard-clip at -1 dBFS.
        """
        if self._L is None or self._n == 0:
            return

        # Stage 1: Apply master gain.
        self._L *= self._gain
        self._R *= self._gain

        # Stage 2: Audition routing.
        mode = self._audition_mode

        if mode == AUDITION_PREVIEW:
            # Simulate -7 LUFS: boost +7 dB then hard-clip at -1.0 dBFS.
            # The C++ version uses the AuditionProcessor with a proper
            # BrickwallLimiter; here we use a simple hard clip.
            self._L *= _PREVIEW_GAIN_LIN
            self._R *= _PREVIEW_GAIN_LIN
            np.clip(self._L, -_AUDITION_CEIL_LIN, _AUDITION_CEIL_LIN, out=self._L)
            np.clip(self._R, -_AUDITION_CEIL_LIN, _AUDITION_CEIL_LIN, out=self._R)

        elif mode == AUDITION_STREAMING:
            # Simulate -14 LUFS / -1.0 dBFS: no pre-gain, clip at -1 dBFS.
            np.clip(self._L, -_AUDITION_CEIL_LIN, _AUDITION_CEIL_LIN, out=self._L)
            np.clip(self._R, -_AUDITION_CEIL_LIN, _AUDITION_CEIL_LIN, out=self._R)

        else:
            # BYPASS: apply the user-configured limiter (hard-clip fallback).
            if self._limiter_on:
                ceiling = 10.0 ** (self._ceiling_db / 20.0)
                np.clip(self._L, -ceiling, ceiling, out=self._L)
                np.clip(self._R, -ceiling, ceiling, out=self._R)

        # Stage 3: Measure block peak, blend into the hold value.
        block_peak_L = float(np.max(np.abs(self._L)))
        block_peak_R = float(np.max(np.abs(self._R)))
        self._peak_L = max(block_peak_L, self._peak_L * self._peak_decay)
        self._peak_R = max(block_peak_R, self._peak_R * self._peak_decay)

    # ── Output ────────────────────────────────────────────────────────────────

    def get_L(self) -> np.ndarray:
        """Return the processed left-channel buffer as a float32 numpy array."""
        return self._L if self._L is not None else np.array([], dtype=np.float32)

    def get_R(self) -> np.ndarray:
        """Return the processed right-channel buffer as a float32 numpy array."""
        return self._R if self._R is not None else np.array([], dtype=np.float32)

    # ── Peak metering ─────────────────────────────────────────────────────────

    def peak_L(self) -> float:
        """Peak level on the left channel (0.0–1.0+)."""
        return self._peak_L

    def peak_R(self) -> float:
        """Peak level on the right channel (0.0–1.0+)."""
        return self._peak_R

    # ── Audition mode ─────────────────────────────────────────────────────────

    def set_audition_mode(self, mode: int) -> None:
        """Switch audition mode: 0 = BYPASS, 1 = PREVIEW, 2 = STREAMING."""
        self._audition_mode = int(mode)

    def get_audition_mode(self) -> int:
        """Return the current audition mode integer."""
        return self._audition_mode

    # ── User limiter parameters ───────────────────────────────────────────────

    def set_gain(self, gain: float) -> None:
        """Set master gain (0.0 = silence, 1.0 = unity, 2.0 ≈ +6 dB)."""
        self._gain = float(gain)

    def get_gain(self) -> float:
        """Return the current master gain scalar."""
        return self._gain

    def set_ceiling(self, db: float) -> None:
        """Set the user limiter ceiling in dBFS (BYPASS mode only)."""
        self._ceiling_db = float(db)

    def get_ceiling(self) -> float:
        """Return the user limiter ceiling in dBFS."""
        return self._ceiling_db

    def set_limiter_enabled(self, enabled: bool) -> None:
        """Enable (True) or bypass (False) the user brickwall limiter."""
        self._limiter_on = bool(enabled)

    def get_limiter_enabled(self) -> bool:
        """Return True if the user brickwall limiter is active."""
        return self._limiter_on


# ── Factory ───────────────────────────────────────────────────────────────────

def get_master_bus(sample_rate: float = 44100.0) -> MasterBusPython:
    """
    Return a C++ MasterBus if the daw_processors extension is available,
    otherwise return the pure-Python fallback.

    Both objects expose the same interface so callers need no conditionals.
    """
    try:
        import daw_processors as dp  # type: ignore[import]
        if hasattr(dp, "MasterBus"):
            return dp.MasterBus(sample_rate)
    except (ImportError, OSError):
        pass
    logger.debug("MasterBus: using Python fallback (C++ extension not available)")
    return MasterBusPython(sample_rate)
