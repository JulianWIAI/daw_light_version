"""
velocity_humanizer_python.py  --  Gaussian MIDI Velocity Humanizer (Python fallback)
======================================================================================
Pure-Python implementation of the VelocityHumanizer class, used when the
C++ daw_processors extension is unavailable.

Algorithm (mirrors the C++ implementation exactly):

    1. Query TimingWeightFunction for the beat-position weight w.
         w > 1.0  on strong beats (downbeat accent)
         w < 1.0  on weak offbeats (de-emphasis)

    2. Compute the timing-weighted mean:
         μ_eff = clamp(base_velocity × w, 1, 127)

    3. Draw one sample from the Gaussian N(μ_eff, σ²) using NumPy's
       default_rng which implements the Ziggurat algorithm — a faster
       alternative to Box-Muller that produces the same distribution.

    4. Round to the nearest integer and clamp to [1, 127].

The probability density satisfies the requirement's formula exactly:
    f(x) = 1/(σ√(2π)) · exp(-½·((x-μ)/σ)²)

Public classes:
    TimingWeightFunctionPython  --  deterministic grid-weight calculator
    GaussianRngPython           --  seeded Gaussian PRNG (NumPy-backed)
    VelocityHumanizerPython     --  combines both; main public API

The module also provides a try/except factory function get_humanizer()
that returns a C++ instance when daw_processors is available and falls
back to VelocityHumanizerPython otherwise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# TimingWeightFunctionPython
# ─────────────────────────────────────────────────────────────────────────────

class TimingWeightFunctionPython:
    """
    Maps an absolute beat position to a deterministic velocity-weight multiplier.

    Classifies every note on a 1/16-note grid and assigns a weight based on
    its structural importance within the bar:

        DOWNBEAT       → 1.0 + downbeat_boost         (beat 1)
        STRONG_BEAT    → 1.0 + downbeat_boost × 0.55  (beat 3 in 4/4)
        QUARTER_BEAT   → 1.0 − offbeat_reduction×0.40 (beats 2, 4)
        EIGHTH_BEAT    → 1.0 − offbeat_reduction×1.00 (eighth upbeats)
        SIXTEENTH_BEAT → 1.0 − offbeat_reduction×1.50 (16th offbeats)

    Notes beyond snap_tolerance beats from any grid point are treated as
    off-grid and receive the maximum offbeat reduction.
    """

    # Size of one 16th note in quarter-note beats.
    _SIXTEENTH = 0.25
    # Floating-point epsilon for "exactly on grid" comparisons.
    _EPS       = 1e-9

    def __init__(
        self,
        time_sig_num:      int   = 4,
        time_sig_denom:    int   = 4,
        downbeat_boost:    float = 0.15,
        offbeat_reduction: float = 0.08,
        snap_tolerance:    float = 0.10,
    ) -> None:
        self.time_sig_num      = max(1, time_sig_num)
        self.time_sig_denom    = max(1, time_sig_denom)
        self.downbeat_boost    = downbeat_boost
        self.offbeat_reduction = offbeat_reduction
        self.snap_tolerance    = snap_tolerance

    def weight(self, beat_position: float) -> float:
        """
        Return the velocity weight multiplier for a note at beat_position.

        beat_position : absolute beat from song start (0.0 = bar 1 beat 1).
                        One unit = one quarter note.

        Returns a positive float; typical range [0.80, 1.20].
        """
        beats_per_bar = float(self.time_sig_num)

        # Fold into within-bar position [0, beats_per_bar).
        bar_pos = math.fmod(beat_position, beats_per_bar)
        if bar_pos < 0.0:
            bar_pos += beats_per_bar

        # Snap to the nearest 1/16-note grid point.
        grid_pos   = round(bar_pos / self._SIXTEENTH) * self._SIXTEENTH
        snap_error = abs(bar_pos - grid_pos)

        # Notes further than snap_tolerance from any grid point are off-grid.
        if snap_error > self.snap_tolerance:
            return 1.0 - self.offbeat_reduction * 1.5

        # Classify the snapped grid position.
        offset = self._classify_offset(grid_pos, beats_per_bar)

        # Scale the offset by how precisely on-grid the note lands.
        # snap_factor = 1.0 when exactly on grid; 0.0 at the tolerance edge.
        snap_factor = 1.0 - snap_error / self.snap_tolerance
        offset     *= max(0.0, snap_factor)

        return 1.0 + offset

    def _classify_offset(self, grid_pos: float, beats_per_bar: float) -> float:
        """Return the raw weight offset for a snapped grid position."""

        # ── Downbeat (beat 1 = position 0) ──────────────────────────────────
        if grid_pos < self._EPS:
            return +self.downbeat_boost

        # ── Quarter-note boundaries ──────────────────────────────────────────
        qn_frac = math.fmod(grid_pos, 1.0)
        if qn_frac < self._EPS:
            # Beat 3 in 4/4 (grid_pos ≈ 2.0) acts as a structural backbeat.
            if beats_per_bar >= 4.0 and abs(grid_pos - 2.0) < self._EPS:
                return +self.downbeat_boost * 0.55
            # Beats 2, 4 — slightly de-emphasised weak beats.
            return -self.offbeat_reduction * 0.40

        # ── Eighth-note upbeats (0.5, 1.5, 2.5, …) ──────────────────────────
        en_frac = math.fmod(grid_pos, 0.5)
        if en_frac < self._EPS:
            return -self.offbeat_reduction * 1.00

        # ── 16th-note offbeats (0.25, 0.75, 1.25, …) ─────────────────────────
        return -self.offbeat_reduction * 1.50


# ─────────────────────────────────────────────────────────────────────────────
# GaussianRngPython
# ─────────────────────────────────────────────────────────────────────────────

class GaussianRngPython:
    """
    Seeded Gaussian pseudo-random number generator backed by NumPy.

    NumPy's default_rng uses the PCG64 algorithm (a 128-bit permuted
    congruential generator) which has better statistical properties than
    Xorshift64 while remaining fast.  The Box-Muller transform is used
    implicitly by NumPy's standard_normal implementation (Ziggurat method),
    which is mathematically equivalent and faster.

    The pdf() static method implements the exact formula from the requirement:
        f(x) = 1/(σ√(2π)) · exp(-½·((x-μ)/σ)²)
    """

    def __init__(self, seed: Optional[int] = None) -> None:
        # Create a seeded NumPy random generator.
        # A seed of None uses OS entropy (non-reproducible).
        self._rng = np.random.default_rng(seed)

    def reseed(self, seed: int) -> None:
        """Replace the RNG state for reproducible offline export."""
        self._rng = np.random.default_rng(seed)

    def sample(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        """Draw one sample from N(mu, sigma²)."""
        return float(self._rng.normal(loc=mu, scale=sigma))

    @staticmethod
    def pdf(x: float, mu: float, sigma: float) -> float:
        """
        Evaluate the Gaussian probability density at x.

        f(x) = 1/(σ√(2π)) · exp(-½·((x-μ)/σ)²)

        This is the formula from the requirement; not used internally.
        """
        z     = (x - mu) / sigma
        coeff = 1.0 / (sigma * math.sqrt(2.0 * math.pi))
        return coeff * math.exp(-0.5 * z * z)


# ─────────────────────────────────────────────────────────────────────────────
# VelocityHumanizerPython
# ─────────────────────────────────────────────────────────────────────────────

class VelocityHumanizerPython:
    """
    Real-time MIDI velocity humanizer (pure-Python fallback).

    Combines GaussianRngPython with TimingWeightFunctionPython to produce
    musically natural velocity variation without simple uniform randomness.

    Parameters
    ----------
    sigma : float
        Gaussian spread in velocity units.  Small values (≤ 5) produce
        subtle variation; large values (≥ 20) produce wild variation.
    downbeat_boost : float
        Fractional velocity boost applied to notes on beat 1 of the bar.
        0.15 → notes on the downbeat average 15 % louder than the base.
    offbeat_reduction : float
        Fractional velocity cut applied to weak offbeats.
        0.08 → eighth upbeats average 8 % quieter than the base.
    time_sig_num : int
        Beats per bar (time-signature numerator; default 4 for 4/4).
    time_sig_denom : int
        Beat value (time-signature denominator; default 4 for quarter note).
    snap_tolerance : float
        Maximum distance in beats from a grid point for a note to be
        considered "on the grid".  Default 0.10 beats.
    seed : int or None
        PRNG seed.  Pass an integer for reproducible offline export;
        leave None for non-reproducible real-time use.
    """

    def __init__(
        self,
        sigma:             float            = 8.0,
        downbeat_boost:    float            = 0.15,
        offbeat_reduction: float            = 0.08,
        time_sig_num:      int              = 4,
        time_sig_denom:    int              = 4,
        snap_tolerance:    float            = 0.10,
        seed:              Optional[int]    = None,
    ) -> None:
        # Store all parameters so they can be read back and updated.
        self.sigma             = sigma
        self.downbeat_boost    = downbeat_boost
        self.offbeat_reduction = offbeat_reduction
        self.time_sig_num      = time_sig_num
        self.time_sig_denom    = time_sig_denom
        self.snap_tolerance    = snap_tolerance

        # Internal sub-components.
        self._rng    = GaussianRngPython(seed=seed)
        self._timing = TimingWeightFunctionPython(
            time_sig_num      = time_sig_num,
            time_sig_denom    = time_sig_denom,
            downbeat_boost    = downbeat_boost,
            offbeat_reduction = offbeat_reduction,
            snap_tolerance    = snap_tolerance,
        )

    def set_params(
        self,
        sigma:             Optional[float] = None,
        downbeat_boost:    Optional[float] = None,
        offbeat_reduction: Optional[float] = None,
        time_sig_num:      Optional[int]   = None,
        time_sig_denom:    Optional[int]   = None,
        snap_tolerance:    Optional[float] = None,
        seed:              Optional[int]   = None,
    ) -> None:
        """Update one or more parameters without touching the others."""
        if sigma             is not None: self.sigma             = sigma
        if downbeat_boost    is not None: self.downbeat_boost    = downbeat_boost
        if offbeat_reduction is not None: self.offbeat_reduction = offbeat_reduction
        if time_sig_num      is not None: self.time_sig_num      = time_sig_num
        if time_sig_denom    is not None: self.time_sig_denom    = time_sig_denom
        if snap_tolerance    is not None: self.snap_tolerance    = snap_tolerance

        # Rebuild the timing function whenever any timing parameter changes.
        self._timing = TimingWeightFunctionPython(
            time_sig_num      = self.time_sig_num,
            time_sig_denom    = self.time_sig_denom,
            downbeat_boost    = self.downbeat_boost,
            offbeat_reduction = self.offbeat_reduction,
            snap_tolerance    = self.snap_tolerance,
        )

        if seed is not None:
            self._rng.reseed(seed)

    def reseed(self, seed: int) -> None:
        """Re-seed the Gaussian RNG for reproducible offline export."""
        self._rng.reseed(seed)

    def humanize(self, base_velocity: int, beat_position: float) -> int:
        """
        Humanize a single MIDI note velocity.

        Parameters
        ----------
        base_velocity : int
            The automation-line target velocity [1, 127].
        beat_position : float
            Absolute beat of the note from the start of the song
            (0.0 = bar 1 beat 1; 4.0 = bar 2 beat 1 in 4/4).

        Returns
        -------
        int
            Humanized velocity in [1, 127].
        """
        # Step 1 — clamp input to valid MIDI velocity range.
        base = max(1.0, min(127.0, float(base_velocity)))

        # Step 2 — apply the deterministic timing weight.
        #   μ_eff = base_velocity × w
        # w > 1 on downbeats (push accent); w < 1 on offbeats (float).
        w      = self._timing.weight(beat_position)
        mu_eff = max(1.0, min(127.0, base * w))

        # Step 3 — draw from N(μ_eff, σ²).
        # Satisfies f(x) = 1/(σ√(2π))·exp(-½·((x-μ_eff)/σ)²).
        sigma = max(0.01, min(64.0, self.sigma))
        raw   = self._rng.sample(mu_eff, sigma)

        # Step 4 — round and clamp to [1, 127].
        return max(1, min(127, round(raw)))


# ─────────────────────────────────────────────────────────────────────────────
# Factory: try C++ first, fall back to Python
# ─────────────────────────────────────────────────────────────────────────────

def get_humanizer(
    sigma:             float         = 8.0,
    downbeat_boost:    float         = 0.15,
    offbeat_reduction: float         = 0.08,
    time_sig_num:      int           = 4,
    time_sig_denom:    int           = 4,
    snap_tolerance:    float         = 0.10,
    seed:              Optional[int] = None,
) -> object:
    """
    Return a velocity humanizer instance, preferring the C++ backend.

    Tries to instantiate daw_processors.VelocityHumanizer first.
    Falls back to VelocityHumanizerPython if the C++ extension is
    unavailable (Win32 error 193, wrong architecture, etc.).

    The returned object always exposes:
        .humanize(base_velocity: int, beat_position: float) -> int
        .reseed(seed: int) -> None
    """
    try:
        import daw_processors as dp  # type: ignore[import]
        params                   = dp.VelocityHumanizerParams()
        params.sigma             = sigma
        params.downbeat_boost    = downbeat_boost
        params.offbeat_reduction = offbeat_reduction
        params.time_sig_num      = time_sig_num
        params.time_sig_denom    = time_sig_denom
        params.snap_tolerance    = snap_tolerance
        params.seed              = seed if seed is not None else 0
        return dp.VelocityHumanizer(params)
    except Exception:
        # C++ extension unavailable — use the pure-Python implementation.
        return VelocityHumanizerPython(
            sigma             = sigma,
            downbeat_boost    = downbeat_boost,
            offbeat_reduction = offbeat_reduction,
            time_sig_num      = time_sig_num,
            time_sig_denom    = time_sig_denom,
            snap_tolerance    = snap_tolerance,
            seed              = seed,
        )
