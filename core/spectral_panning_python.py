"""
spectral_panning_python.py  --  Spectral Panning & Masking Resolution (Python fallback)
=========================================================================================
Pure-Python / NumPy implementation of the spectral panning system, used when
the daw_processors C++ extension is unavailable.

Mirrors the C++ implementation exactly:

  SpectralAnalyzerPython
      Accumulates audio samples in a ring buffer, applies a Hann window,
      runs numpy.fft.rfft, and computes the spectral centroid:
          C = sum( f(k) * |X(k)| ) / sum( |X(k)| )

  SpectralMaskingManagerPython
      Module-level singleton (thread-safe via threading.Lock) that receives
      centroid values from paired processors and computes equal-and-opposite
      pan vectors with LP smoothing.

  SpectralPanningProcessorPython
      Per-track processor that chains analyzer → manager → pan application.
      process_block(left, right) → (out_left, out_right) as numpy arrays.

Factory:
    get_spectral_panning_processor(sample_rate, params_dict)
        Returns C++ SpectralPanningProcessor when daw_processors is available,
        otherwise SpectralPanningProcessorPython.
"""

from __future__ import annotations

import math
import threading
from typing import Dict, Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# SpectralAnalyzerPython
# ─────────────────────────────────────────────────────────────────────────────

class SpectralAnalyzerPython:
    """
    Real-time spectral centroid analyzer using numpy FFT.

    Accumulates samples into a ring buffer (size FFT_SIZE = 2048), applies a
    Hann window, and re-computes the spectral centroid every HOP_SIZE = 512
    new samples.

    Spectral centroid formula:
        C = sum( f(k) * |X(k)| ) / sum( |X(k)| )
    where f(k) = k * sample_rate / FFT_SIZE
    """

    FFT_SIZE = 2048
    HOP_SIZE = FFT_SIZE // 4  # 512

    def __init__(self, sample_rate: float = 44100.0) -> None:
        self.sample_rate = max(1.0, sample_rate)

        # Pre-compute the Hann window once.
        n = np.arange(self.FFT_SIZE)
        self._hann = 0.5 * (1.0 - np.cos(2.0 * math.pi * n / (self.FFT_SIZE - 1)))
        self._hann = self._hann.astype(np.float32)

        # Ring buffer and state.
        self._ring  = np.zeros(self.FFT_SIZE, dtype=np.float32)
        self._write_pos        = 0
        self._samples_since_fft = 0
        self._centroid = 0.0
        self._rms      = 0.0

    def prepare(self, sample_rate: float) -> None:
        """Re-initialise at a new sample rate."""
        self.sample_rate = max(1.0, sample_rate)
        self.reset()

    def reset(self) -> None:
        """Zero the ring buffer and results."""
        self._ring[:]          = 0.0
        self._write_pos        = 0
        self._samples_since_fft = 0
        self._centroid = 0.0
        self._rms      = 0.0

    def push_block(self, left: np.ndarray, right: np.ndarray) -> bool:
        """
        Feed a stereo block into the analyzer.

        Returns True when a new centroid was computed during this call.
        """
        # Down-mix stereo to mono.
        mono = ((left.astype(np.float64) + right.astype(np.float64)) * 0.5).astype(np.float32)
        n    = len(mono)
        updated = False

        for i in range(n):
            self._ring[self._write_pos] = mono[i]
            self._write_pos = (self._write_pos + 1) % self.FFT_SIZE
            self._samples_since_fft += 1

            if self._samples_since_fft >= self.HOP_SIZE:
                self._samples_since_fft = 0
                self._run_fft()
                updated = True

        return updated

    def get_centroid(self) -> float:
        """Return the most recent spectral centroid in Hz."""
        return self._centroid

    def get_rms(self) -> float:
        """Return the approximate spectral RMS of the last frame."""
        return self._rms

    def _run_fft(self) -> None:
        """Run FFT on the current ring-buffer content and update centroid."""
        # Unroll the ring buffer in chronological order (oldest → newest).
        ordered = np.roll(self._ring, -self._write_pos)

        # Apply Hann window to reduce spectral leakage.
        windowed = ordered * self._hann

        # rfft returns N/2 + 1 complex bins for real input (DC … Nyquist).
        spectrum = np.fft.rfft(windowed)

        # Use magnitude spectrum; skip DC bin (k=0).
        mags  = np.abs(spectrum[1:]).astype(np.float64)
        freqs = np.fft.rfftfreq(self.FFT_SIZE, d=1.0 / self.sample_rate)[1:]

        total = mags.sum()
        if total > 1e-10:
            self._centroid = float((freqs * mags).sum() / total)
        else:
            self._centroid = 0.0

        self._rms = float(math.sqrt((mags ** 2).mean())) if len(mags) > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# SpectralMaskingManagerPython  (module-level singleton)
# ─────────────────────────────────────────────────────────────────────────────

class _GroupState:
    """Mutable state for one group of paired processors."""
    __slots__ = ("centroid", "smooth_pan")

    def __init__(self) -> None:
        self.centroid   = [500.0, 2000.0]   # centroid per slot
        self.smooth_pan = [0.0,   0.0   ]   # smoothed pan per slot


class SpectralMaskingManagerPython:
    """
    Module-level singleton managing panning state for paired processor groups.

    Thread-safe via threading.Lock (audio render threads may call update()
    concurrently for different groups).
    """

    def __init__(self) -> None:
        self._groups: Dict[int, _GroupState] = {}
        self._lock   = threading.Lock()

    def update(self, group_id: int, slot: int, centroid_hz: float,
               tolerance_hz: float, max_pan: float,
               dt: float, smooth_ms: float) -> None:
        """
        Push a new centroid for one slot, recompute masking, advance LP filter.

        group_id     : integer shared between the two paired plugins.
        slot         : 0 = track A, 1 = track B.
        centroid_hz  : latest spectral centroid from SpectralAnalyzerPython.
        tolerance_hz : masking detection threshold.
        max_pan      : maximum pan deflection [0, 1].
        dt           : current block duration in seconds.
        smooth_ms    : LP filter time constant.
        """
        s = slot & 1

        # One-pole LP coefficient.
        coeff = 0.0
        if smooth_ms > 0.0 and dt > 0.0:
            coeff = math.exp(-dt / (smooth_ms * 0.001))

        with self._lock:
            if group_id not in self._groups:
                self._groups[group_id] = _GroupState()
            gs = self._groups[group_id]

            # Update this slot's centroid.
            gs.centroid[s] = centroid_hz

            # Compute masking detection.
            delta    = gs.centroid[0] - gs.centroid[1]
            absdelta = abs(delta)

            target_pan_0 = 0.0
            target_pan_1 = 0.0
            if absdelta < tolerance_hz:
                strength  = (tolerance_hz - absdelta) / tolerance_hz
                # Track with lower centroid goes left.
                direction = 1.0 if delta <= 0.0 else -1.0
                target_pan_0 = -direction * max_pan * strength
                target_pan_1 = +direction * max_pan * strength

            # Advance the LP filter for this slot only.
            target = target_pan_0 if s == 0 else target_pan_1
            gs.smooth_pan[s] = coeff * gs.smooth_pan[s] + (1.0 - coeff) * target

    def get_pan(self, group_id: int, slot: int) -> float:
        """Return the current smoothed pan for a slot [-1, +1]."""
        with self._lock:
            gs = self._groups.get(group_id)
            if gs is None:
                return 0.0
            return gs.smooth_pan[slot & 1]

    def remove_group(self, group_id: int) -> None:
        """Remove a group's state."""
        with self._lock:
            self._groups.pop(group_id, None)


# Module-level singleton instance shared by all SpectralPanningProcessorPython.
_MASKING_MANAGER = SpectralMaskingManagerPython()


# ─────────────────────────────────────────────────────────────────────────────
# SpectralPanningProcessorPython
# ─────────────────────────────────────────────────────────────────────────────

class SpectralPanningProcessorPython:
    """
    Per-track spectral panning processor (pure-Python fallback).

    Chains SpectralAnalyzerPython → SpectralMaskingManagerPython → equal-power
    pan law applied to the stereo audio block.

    Parameters
    ----------
    sample_rate   : host sample rate in Hz.
    group_id      : integer group ID shared with the sibling track.
    slot          : 0 = track A (pans left under masking), 1 = track B (pans right).
    tolerance_hz  : masking detection threshold in Hz.
    max_pan       : maximum pan deflection [0, 1].
    smooth_ms     : LP filter time constant for pan transitions (ms).
    """

    def __init__(
        self,
        sample_rate:   float = 44100.0,
        group_id:      int   = 0,
        slot:          int   = 0,
        tolerance_hz:  float = 300.0,
        max_pan:       float = 0.5,
        smooth_ms:     float = 100.0,
    ) -> None:
        self.sample_rate  = max(1.0, sample_rate)
        self.group_id     = group_id
        self.slot         = slot & 1
        self.tolerance_hz = tolerance_hz
        self.max_pan      = max_pan
        self.smooth_ms    = smooth_ms

        self._analyzer = SpectralAnalyzerPython(sample_rate)

    def set_params(
        self,
        sample_rate:   Optional[float] = None,
        group_id:      Optional[int]   = None,
        slot:          Optional[int]   = None,
        tolerance_hz:  Optional[float] = None,
        max_pan:       Optional[float] = None,
        smooth_ms:     Optional[float] = None,
    ) -> None:
        """Update one or more parameters."""
        if group_id is not None and group_id != self.group_id:
            _MASKING_MANAGER.remove_group(self.group_id)
        if sample_rate  is not None: self.sample_rate  = max(1.0, sample_rate)
        if group_id     is not None: self.group_id     = group_id
        if slot         is not None: self.slot         = slot & 1
        if tolerance_hz is not None: self.tolerance_hz = tolerance_hz
        if max_pan      is not None: self.max_pan      = max_pan
        if smooth_ms    is not None: self.smooth_ms    = smooth_ms

        if sample_rate is not None:
            self._analyzer.prepare(self.sample_rate)

    def process_block(
        self,
        left:  np.ndarray,
        right: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Analyze + pan one stereo block.

        Returns (out_left, out_right) as new float32 numpy arrays.
        """
        n      = len(left)
        out_l  = np.ascontiguousarray(left,  dtype=np.float32).copy()
        out_r  = np.ascontiguousarray(right, dtype=np.float32).copy()

        if n == 0:
            return out_l, out_r

        # ── Step 1: spectral analysis ─────────────────────────────────────────
        self._analyzer.push_block(out_l, out_r)
        centroid_hz = self._analyzer.get_centroid()

        # ── Step 2: update manager, fetch smoothed pan ─────────────────────────
        dt = n / self.sample_rate
        _MASKING_MANAGER.update(
            self.group_id, self.slot, centroid_hz,
            self.tolerance_hz, self.max_pan, dt, self.smooth_ms
        )
        pan = _MASKING_MANAGER.get_pan(self.group_id, self.slot)

        # ── Step 3: apply equal-power pan law ────────────────────────────────
        theta  = (pan + 1.0) * (math.pi / 4.0)   # maps [−1,+1] → [0, π/2]
        gain_l = math.cos(theta)
        gain_r = math.sin(theta)

        # Down-mix to mono, then redistribute with divergent gains.
        mono   = (out_l + out_r) * 0.5
        out_l  = (mono * gain_l * 2.0).astype(np.float32)
        out_r  = (mono * gain_r * 2.0).astype(np.float32)

        return out_l, out_r

    def reset(self) -> None:
        """Zero all internal state."""
        self._analyzer.reset()
        _MASKING_MANAGER.remove_group(self.group_id)

    @property
    def centroid(self) -> float:
        """Most recent spectral centroid in Hz."""
        return self._analyzer.get_centroid()

    @property
    def current_pan(self) -> float:
        """Current smoothed pan value [-1, +1]."""
        return _MASKING_MANAGER.get_pan(self.group_id, self.slot)


# ─────────────────────────────────────────────────────────────────────────────
# Factory: prefer C++, fall back to Python
# ─────────────────────────────────────────────────────────────────────────────

def get_spectral_panning_processor(
    sample_rate:   float = 44100.0,
    group_id:      int   = 0,
    slot:          int   = 0,
    tolerance_hz:  float = 300.0,
    max_pan:       float = 0.5,
    smooth_ms:     float = 100.0,
) -> object:
    """
    Return a spectral panning processor, preferring the C++ backend.

    The returned object always exposes:
        .process_block(left: ndarray, right: ndarray) -> (ndarray, ndarray)
        .set_params(...)
        .reset()
        .centroid    (property, Hz)
        .current_pan (property, [-1, +1])
    """
    try:
        import daw_processors as dp  # type: ignore[import]

        p = dp.SpectralPanningParams()
        p.group_id     = group_id
        p.slot         = slot
        p.tolerance_hz = tolerance_hz
        p.max_pan      = max_pan
        p.smooth_ms    = smooth_ms

        proc = dp.SpectralPanningProcessor(float(sample_rate))
        proc.set_params(p)

        # Wrap to give a uniform Python API.
        class _CppWrapper:
            def __init__(self, cpp):
                self._p = cpp

            def process_block(self, left, right):
                l32 = np.ascontiguousarray(left,  dtype=np.float32)
                r32 = np.ascontiguousarray(right, dtype=np.float32)
                return self._p.process_block(l32, r32)

            def set_params(self, **kwargs):
                par = self._p.params
                for k, v in kwargs.items():
                    if hasattr(par, k):
                        setattr(par, k, v)
                self._p.set_params(par)

            def reset(self):
                self._p.reset()

            @property
            def centroid(self):
                return self._p.get_centroid()

            @property
            def current_pan(self):
                return self._p.get_current_pan()

        return _CppWrapper(proc)

    except Exception:
        return SpectralPanningProcessorPython(
            sample_rate=sample_rate,
            group_id=group_id,
            slot=slot,
            tolerance_hz=tolerance_hz,
            max_pan=max_pan,
            smooth_ms=smooth_ms,
        )
