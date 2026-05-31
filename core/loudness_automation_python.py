"""
loudness_automation_python.py  --  Real-Time Loudness Automation (Python fallback)
====================================================================================
Pure-Python / NumPy implementation of the LoudnessAutomation processor chain,
used when the daw_processors C++ extension is unavailable.

Signal flow (mirrors the C++ implementation exactly):
    block_rms       = RmsAnalyzerPython.compute_rms(left, right)
    smoothed_rms    = EnvelopeFollowerPython.process(block_rms)
    current_db      = RmsAnalyzerPython.to_dbfs(smoothed_rms)
    gain_correction = PidControllerPython.process(current_db, dt)
    target_gain     = 10 ** (gain_correction / 20)    [clamped]
    per-sample gain ramp → multiply left and right channel arrays

Public classes:
    RmsAnalyzerPython      --  static RMS / dBFS helpers
    EnvelopeFollowerPython --  one-pole IIR amplitude smoother
    PidControllerPython    --  discrete-time PID with anti-windup
    LoudnessAutomationPython --  complete processor, process_block() entry point

Factory:
    get_loudness_automation(sample_rate, params_dict)
        Returns a C++ LoudnessAutomation when daw_processors is available,
        otherwise returns LoudnessAutomationPython.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# RmsAnalyzerPython
# ─────────────────────────────────────────────────────────────────────────────

class RmsAnalyzerPython:
    """Static RMS level measurement helpers (no instance state)."""

    @staticmethod
    def compute_rms(left: np.ndarray, right: np.ndarray) -> float:
        """
        Compute the root-mean-square amplitude of a stereo block.

        Averages squared samples from both channels so a mono signal and a
        stereo signal at the same perceived loudness produce equal RMS values.

        left, right : 1-D float32 numpy arrays of equal length.
        Returns     : linear RMS in [0, ∞).
        """
        if len(left) == 0:
            return 0.0
        # Use float64 internally to avoid float32 precision loss in accumulation.
        sum_sq = float(np.sum(left.astype(np.float64) ** 2)
                     + np.sum(right.astype(np.float64) ** 2))
        mean_sq = sum_sq / (2.0 * len(left))
        return math.sqrt(max(0.0, mean_sq))

    @staticmethod
    def to_dbfs(rms: float) -> float:
        """Convert linear RMS amplitude to dBFS.  Returns -120 for near-zero."""
        if rms <= 1e-7:
            return -120.0
        return 20.0 * math.log10(rms)

    @staticmethod
    def from_dbfs(db: float) -> float:
        """Convert dBFS to a linear amplitude multiplier."""
        return 10.0 ** (db / 20.0)


# ─────────────────────────────────────────────────────────────────────────────
# EnvelopeFollowerPython
# ─────────────────────────────────────────────────────────────────────────────

class EnvelopeFollowerPython:
    """
    One-pole IIR amplitude envelope follower.

    Uses separate attack and release coefficients so the envelope tracks
    rising signals quickly (short attack) and decays slowly (long release),
    which matches typical loudness metering behaviour.

    Coefficient formula:
        coeff = exp( -1 / (time_ms × 0.001 × sample_rate) )
    """

    def __init__(
        self,
        sample_rate: float = 44100.0,
        attack_ms:   float = 10.0,
        release_ms:  float = 100.0,
    ) -> None:
        self._env = 0.0
        self._attack_coeff  = 0.0
        self._release_coeff = 0.0
        self.prepare(sample_rate, attack_ms, release_ms)

    def prepare(self, sample_rate: float, attack_ms: float, release_ms: float) -> None:
        """Recompute IIR coefficients for the given sample rate and time constants."""
        sr = max(1.0, sample_rate)
        self._attack_coeff  = self._coeff(attack_ms,  sr)
        self._release_coeff = self._coeff(release_ms, sr)

    def process(self, input_rms: float) -> float:
        """Advance the envelope by one RMS sample; return the smoothed value."""
        coeff = self._attack_coeff if input_rms >= self._env else self._release_coeff
        self._env = coeff * self._env + (1.0 - coeff) * input_rms
        return self._env

    def reset(self) -> None:
        """Zero the internal envelope state."""
        self._env = 0.0

    @property
    def current(self) -> float:
        """Read current envelope level without advancing."""
        return self._env

    @staticmethod
    def _coeff(time_ms: float, sample_rate: float) -> float:
        """Compute a one-pole IIR coefficient from a time constant in ms."""
        if time_ms <= 0.0:
            return 0.0
        t_samples = (time_ms * 0.001) * sample_rate
        return math.exp(-1.0 / t_samples)


# ─────────────────────────────────────────────────────────────────────────────
# PidControllerPython
# ─────────────────────────────────────────────────────────────────────────────

class PidControllerPython:
    """
    Discrete-time PID controller with anti-windup integral clamping.

    Update law (forward-Euler integration):
        e(t)      = setpoint − process_variable
        integral += e(t) × dt          [clamped to ±integral_max]
        deriv     = (e(t) − e(t-1)) / dt
        output    = Kp·e + Ki·∫e + Kd·de/dt
        output    = clamp(output, output_min, output_max)
    """

    def __init__(
        self,
        kp:           float = 1.0,
        ki:           float = 0.1,
        kd:           float = 0.05,
        setpoint:     float = -18.0,
        output_min:   float = -30.0,
        output_max:   float = +12.0,
        integral_max: float = 20.0,
    ) -> None:
        self.kp           = kp
        self.ki           = ki
        self.kd           = kd
        self.setpoint     = setpoint
        self.output_min   = output_min
        self.output_max   = output_max
        self.integral_max = integral_max
        self._integral    = 0.0
        self._prev_error  = 0.0

    def set_params(self, **kwargs) -> None:
        """Update one or more parameters; resets integrator state."""
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self.reset()

    def process(self, process_variable: float, dt: float) -> float:
        """
        Advance by one time step.

        process_variable : current measured level (dBFS)
        dt               : time step in seconds (= block_size / sample_rate)
        Returns          : gain correction in dB
        """
        if dt <= 0.0:
            return 0.0

        error = self.setpoint - process_variable

        # Integral with anti-windup clamp.
        self._integral = max(
            -self.integral_max,
            min(self.integral_max, self._integral + error * dt)
        )

        derivative      = (error - self._prev_error) / dt
        self._prev_error = error

        output = self.kp * error + self.ki * self._integral + self.kd * derivative
        return max(self.output_min, min(self.output_max, output))

    def reset(self) -> None:
        """Zero integral accumulator and previous error."""
        self._integral   = 0.0
        self._prev_error = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# LoudnessAutomationPython
# ─────────────────────────────────────────────────────────────────────────────

class LoudnessAutomationPython:
    """
    Real-time loudness automation processor (pure-Python fallback).

    Chains RmsAnalyzerPython → EnvelopeFollowerPython → PidControllerPython
    and applies per-sample linear gain interpolation to prevent zipper noise.

    Parameters
    ----------
    sample_rate : float
        Host sample rate in Hz.
    target_dbfs : float
        Desired RMS loudness target in dBFS (default -18).
    attack_ms : float
        Envelope follower attack time in ms (default 20).
    release_ms : float
        Envelope follower release time in ms (default 200).
    kp, ki, kd : float
        PID proportional, integral, derivative gains.
    gain_min_db : float
        Minimum allowed gain correction in dB (default -30).
    gain_max_db : float
        Maximum allowed gain correction in dB (default +12).
    """

    def __init__(
        self,
        sample_rate: float = 44100.0,
        target_dbfs: float = -18.0,
        attack_ms:   float = 20.0,
        release_ms:  float = 200.0,
        kp:          float = 1.0,
        ki:          float = 0.1,
        kd:          float = 0.05,
        gain_min_db: float = -30.0,
        gain_max_db: float = +12.0,
    ) -> None:
        self.sample_rate = max(1.0, sample_rate)
        self.target_dbfs = target_dbfs
        self.attack_ms   = attack_ms
        self.release_ms  = release_ms
        self.kp          = kp
        self.ki          = ki
        self.kd          = kd
        self.gain_min_db = gain_min_db
        self.gain_max_db = gain_max_db

        self._current_gain: float = 1.0
        self._target_gain:  float = 1.0

        self._follower = EnvelopeFollowerPython(
            sample_rate, attack_ms, release_ms
        )
        self._pid = PidControllerPython(
            kp=kp, ki=ki, kd=kd,
            setpoint=target_dbfs,
            output_min=gain_min_db,
            output_max=gain_max_db,
        )

    def set_params(
        self,
        sample_rate: Optional[float] = None,
        target_dbfs: Optional[float] = None,
        attack_ms:   Optional[float] = None,
        release_ms:  Optional[float] = None,
        kp:          Optional[float] = None,
        ki:          Optional[float] = None,
        kd:          Optional[float] = None,
        gain_min_db: Optional[float] = None,
        gain_max_db: Optional[float] = None,
    ) -> None:
        """Update one or more parameters and rebuild sub-components."""
        if sample_rate is not None: self.sample_rate = max(1.0, sample_rate)
        if target_dbfs is not None: self.target_dbfs = target_dbfs
        if attack_ms   is not None: self.attack_ms   = attack_ms
        if release_ms  is not None: self.release_ms  = release_ms
        if kp          is not None: self.kp          = kp
        if ki          is not None: self.ki          = ki
        if kd          is not None: self.kd          = kd
        if gain_min_db is not None: self.gain_min_db = gain_min_db
        if gain_max_db is not None: self.gain_max_db = gain_max_db

        self._follower.prepare(self.sample_rate, self.attack_ms, self.release_ms)
        self._pid.set_params(
            kp=self.kp, ki=self.ki, kd=self.kd,
            setpoint=self.target_dbfs,
            output_min=self.gain_min_db,
            output_max=self.gain_max_db,
        )

    def process_block(
        self,
        left:  np.ndarray,
        right: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Process one stereo block and return gain-adjusted copies.

        left, right : 1-D float32 numpy arrays of equal length.
        Returns     : (out_left, out_right) as new float32 arrays.

        Gain changes are interpolated per-sample to prevent zipper noise.
        """
        n = len(left)
        if n == 0:
            return left.copy(), right.copy()

        out_left  = left.astype(np.float32, copy=True)
        out_right = right.astype(np.float32, copy=True)

        # Step 1: instantaneous RMS of this block.
        block_rms = RmsAnalyzerPython.compute_rms(out_left, out_right)

        # Step 2: smooth through the envelope follower.
        smoothed_rms = self._follower.process(block_rms)

        # Step 3: convert to dBFS.
        current_db = RmsAnalyzerPython.to_dbfs(smoothed_rms)

        # Step 4: PID produces a gain correction in dB.
        dt = n / self.sample_rate
        gain_correction_db = self._pid.process(current_db, dt)

        # Step 5: convert to linear gain (clamped first to avoid extreme values).
        clamped_db = max(self.gain_min_db, min(self.gain_max_db, gain_correction_db))
        self._target_gain = 10.0 ** (clamped_db / 20.0)

        # Step 6: per-sample linear gain ramp (zipper-noise prevention).
        gain_step = (self._target_gain - self._current_gain) / n
        gains = (self._current_gain
                 + gain_step * np.arange(1, n + 1, dtype=np.float32))
        out_left  *= gains
        out_right *= gains

        # Snap to the exact target to avoid floating-point drift over many blocks.
        self._current_gain = self._target_gain
        return out_left, out_right

    def reset(self) -> None:
        """Zero all internal controller state."""
        self._follower.reset()
        self._pid.reset()
        self._current_gain = 1.0
        self._target_gain  = 1.0

    @property
    def current_gain(self) -> float:
        """Current instantaneous gain multiplier (linear)."""
        return self._current_gain

    @property
    def current_gain_db(self) -> float:
        """Current gain in dB for metering."""
        return RmsAnalyzerPython.to_dbfs(self._current_gain)


# ─────────────────────────────────────────────────────────────────────────────
# Factory: prefer C++, fall back to Python
# ─────────────────────────────────────────────────────────────────────────────

def get_loudness_automation(
    sample_rate: float = 44100.0,
    target_dbfs: float = -18.0,
    attack_ms:   float = 20.0,
    release_ms:  float = 200.0,
    kp:          float = 1.0,
    ki:          float = 0.1,
    kd:          float = 0.05,
    gain_min_db: float = -30.0,
    gain_max_db: float = +12.0,
) -> object:
    """
    Return a LoudnessAutomation instance, preferring the C++ backend.

    Tries daw_processors.LoudnessAutomation first; falls back to
    LoudnessAutomationPython if the C++ extension is unavailable.

    The returned object always exposes:
        .process_block(left: ndarray, right: ndarray) -> (ndarray, ndarray)
        .set_params(...)
        .reset()
        .current_gain       (property, linear)
        .current_gain_db    (property, dBFS)
    """
    try:
        import daw_processors as dp  # type: ignore[import]
        p = dp.LoudnessAutomationParams()
        p.target_dbfs = target_dbfs
        p.attack_ms   = attack_ms
        p.release_ms  = release_ms
        p.kp          = kp
        p.ki          = ki
        p.kd          = kd
        p.gain_min_db = gain_min_db
        p.gain_max_db = gain_max_db
        proc = dp.LoudnessAutomation(sample_rate)
        proc.set_params(p)

        # Wrap process_block to match the Python API: return numpy arrays.
        class _CppWrapper:
            def __init__(self, cpp_proc):
                self._proc = cpp_proc

            def process_block(self, left: np.ndarray, right: np.ndarray):
                l32 = np.ascontiguousarray(left,  dtype=np.float32)
                r32 = np.ascontiguousarray(right, dtype=np.float32)
                return self._proc.process_block(l32, r32)

            def set_params(self, **kwargs):
                par = self._proc.params
                for k, v in kwargs.items():
                    if hasattr(par, k):
                        setattr(par, k, v)
                self._proc.set_params(par)

            def reset(self):
                self._proc.reset()

            @property
            def current_gain(self):
                return self._proc.current_gain()

            @property
            def current_gain_db(self):
                return self._proc.current_gain_db()

        return _CppWrapper(proc)

    except Exception:
        return LoudnessAutomationPython(
            sample_rate=sample_rate,
            target_dbfs=target_dbfs,
            attack_ms=attack_ms,
            release_ms=release_ms,
            kp=kp, ki=ki, kd=kd,
            gain_min_db=gain_min_db,
            gain_max_db=gain_max_db,
        )
