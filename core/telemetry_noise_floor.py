"""
core/telemetry_noise_floor.py

Spectral noise-floor calibrator for the Python telemetry DSP fallback
(_TelemetryAnalyzerPy inside telemetry_manager.py).

Problem it solves
-----------------
Even when a DAW sound is playing, the displayed spectral pattern is a
superposition of the actual instrument spectrum AND the ambient system
noise floor.  Because the 7-band and chroma analyses normalise relative to
the global spectral mean, even a small noise contribution skews all values.

How it works
------------
During silence periods (identified by the caller via RMS thresholds) the
analyzer feeds raw FFT magnitude spectra into add_silence_frame().  An
exponential moving average (EMA) builds a per-bin noise baseline over
at least MIN_FRAMES frames (≈ 0.93 s at 44 100 Hz / 2048-sample FFT).

During active signal periods subtract() returns a half-wave-rectified
residual spectrum:

    clean[i] = max(0,  mag[i] − floor[i] × OVERSUB)

The over-subtraction factor (default 1.2) slightly exceeds the measured
noise so the residual is robustly clean rather than showing spectral
valleys from under-subtraction.

Thread safety
-------------
add_silence_frame() and subtract() may be called from different threads
(push() from the audio thread, get_frame() from the GUI timer thread).
Internal state is protected by a threading.Lock.
"""

from __future__ import annotations

import threading

import numpy as np


class TelemetryNoiseFloorCalibrator:
    """
    Incremental spectral noise-floor estimator (pure Python / NumPy).

    Parameters
    ----------
    n_bins          : number of FFT magnitude bins  (= FFT_SIZE // 2 + 1)
    smoothing       : EMA α coefficient — smaller is slower and more stable
    oversubtraction : multiplier on the stored floor before subtraction
    min_frames      : minimum silence frames required before floor is trusted
    """

    # ── Defaults ──────────────────────────────────────────────────────────────
    # These match the C++ TelemetryNoiseFloor constants in TelemetryNoiseFloor.h.

    _DEFAULT_ALPHA      = 0.05   # converges in ≈ 20 frames
    _DEFAULT_OVERSUB    = 1.2    # 20 % over-subtraction for clean residual
    _DEFAULT_MIN_FRAMES = 20     # ≈ 0.93 s at 44 100 Hz / 2048-sample FFT

    def __init__(
        self,
        n_bins:          int   = 1025,
        smoothing:       float = _DEFAULT_ALPHA,
        oversubtraction: float = _DEFAULT_OVERSUB,
        min_frames:      int   = _DEFAULT_MIN_FRAMES,
    ) -> None:
        self._n_bins     = int(n_bins)
        self._alpha      = float(smoothing)
        self._oversub    = float(oversubtraction)
        self._min_frames = int(min_frames)

        self._floor:  np.ndarray = np.zeros(self._n_bins, dtype=np.float32)
        self._frames: int        = 0
        self._lock               = threading.Lock()

    # ── Read-only properties ───────────────────────────────────────────────────

    @property
    def is_calibrated(self) -> bool:
        """True once at least min_frames silence frames have been ingested."""
        return self._frames >= self._min_frames

    @property
    def frame_count(self) -> int:
        """Total silence frames ingested since the last reset()."""
        return self._frames

    # ── Public interface ───────────────────────────────────────────────────────

    def add_silence_frame(self, magnitudes: np.ndarray) -> None:
        """
        Update the noise-floor estimate from one silence-period FFT magnitude
        spectrum.  Silently ignored if the array has the wrong length.

        Parameters
        ----------
        magnitudes : float32 ndarray of shape (n_bins,) — raw FFT magnitudes
        """
        mags = np.asarray(magnitudes, dtype=np.float32)
        if mags.ndim != 1 or len(mags) != self._n_bins:
            return

        with self._lock:
            if self._frames == 0:
                # Bootstrap: copy first frame directly.
                self._floor[:] = mags

            elif self._frames < self._min_frames:
                # Warm-up phase: take per-bin minimum of EMA and current frame.
                # This prevents transient spikes (e.g. a click during silence)
                # from inflating the floor estimate.
                ema = self._alpha * mags + (1.0 - self._alpha) * self._floor
                self._floor[:] = np.minimum(ema, mags)

            else:
                # Steady state: standard EMA — adapts slowly to changing environment.
                self._floor[:] = self._alpha * mags + (1.0 - self._alpha) * self._floor

            self._frames += 1

    def subtract(self, magnitudes: np.ndarray) -> np.ndarray:
        """
        Return a floor-subtracted copy of the given magnitude spectrum.

        Applies oversubtraction and half-wave rectification so no bin goes
        below zero.  Returns an unchanged copy if not yet calibrated.

        Parameters
        ----------
        magnitudes : float32 ndarray of shape (n_bins,) — live FFT magnitudes

        Returns
        -------
        float32 ndarray of shape (n_bins,) — denoised magnitudes, all ≥ 0
        """
        mags = np.asarray(magnitudes, dtype=np.float32)

        if not self.is_calibrated:
            # Not enough silence data yet — pass through unchanged.
            return mags.copy()

        with self._lock:
            # Apply oversubtraction factor outside the lock hold would be a
            # data race on self._floor; multiply while still holding the lock.
            floor = self._floor * np.float32(self._oversub)

        return np.maximum(np.float32(0.0), mags - floor)

    def get_floor(self) -> np.ndarray:
        """Return a thread-safe copy of the current noise-floor spectrum."""
        with self._lock:
            return self._floor.copy()

    def reset(self) -> None:
        """
        Discard all accumulated data and zero the floor.
        Call when the audio environment changes (new device, new project).
        """
        with self._lock:
            self._floor[:] = 0.0
            self._frames   = 0