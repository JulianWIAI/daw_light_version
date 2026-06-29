"""
core/telemetry_noise_gate.py

Chunk-level noise gate for the TelemetryManager input pipeline.

Problem it solves
-----------------
The telemetry analyzer always processes audio, even when the DAW is silent.
Background system noise (ADC noise, computer fans, EMI) produces non-zero
relative band values because the FFT normalises by the global spectral mean —
even -80 dBFS noise spreads energy across all 7 bands.

How it works
------------
The gate operates in two sequential phases:

  Phase 1 — CALIBRATING
    The gate is open and all audio passes through unchanged.  The downstream
    analyzer (TelemetryAnalyzer.cpp or _TelemetryAnalyzerPy) simultaneously
    accumulates a spectral noise-floor baseline from these raw frames.
    Calibration ends after CALIB_SAMPLES worth of "quiet" audio
    (RMS < CALIB_THRESH_DB) have been observed.  Any signal spike resets the
    quiet counter so calibration always happens from genuine silence.

  Phase 2 — GATING  (states: CLOSED / OPENING / OPEN / HOLDING / CLOSING)
    Once calibrated the gate uses a hysteretic RMS threshold to gate signal.
    When CLOSED it returns all-zeros so the downstream analyzer produces
    zeroed frames and the telemetry display decays to a clean baseline.
    On opening it ramps the gain from 0 → 1 over attack_ms to avoid step
    discontinuities; on closing it ramps 1 → 0 over release_ms.

Thresholds (defaults)
---------------------
  open  : -50 dBFS  — gate opens above this RMS level
  close : -60 dBFS  — gate begins closing below this RMS level (10 dB hysteresis)
  calib : -55 dBFS  — "quiet enough" criterion used during calibration

These values put the transition well above typical ADC noise (-70 to -90 dBFS)
but well below the softest intentional DAW content (~-40 dBFS), so soft musical
tails are never cut off.
"""

from __future__ import annotations

import numpy as np


# ── Module-level default threshold constants ───────────────────────────────────

# Gate opens above this level (dBFS).
_OPEN_THRESH_DB: float  = -50.0

# Gate begins closing below this level (dBFS).  Hysteresis = open − close = 10 dB.
_CLOSE_THRESH_DB: float = -60.0

# "Quiet" criterion used only during the CALIBRATING phase.
_CALIB_THRESH_DB: float = -55.0

# Cumulative quiet samples required to finish calibration (≈ 2 s at 44100 Hz).
_CALIB_SAMPLES: int = 88200


class TelemetryNoiseGate:
    """
    Chunk-level noise gate for the telemetry input pipeline.

    Parameters
    ----------
    sample_rate    : int   — audio sample rate in Hz           (default 44 100)
    open_db        : float — RMS gate-open threshold, dBFS     (default  −50)
    close_db       : float — RMS gate-close threshold, dBFS    (default  −60)
    attack_ms      : float — gain ramp-up time in ms           (default   10)
    hold_ms        : float — silence wait before closing, ms   (default  200)
    release_ms     : float — gain ramp-down time in ms         (default  100)
    calib_db       : float — "quiet" dBFS level for calib.     (default  −55)
    calib_samples  : int   — quiet samples to finish calib.    (default 88 200)
    """

    # ── State identifiers ────────────────────────────────────────────────────────
    CALIBRATING: int = 0
    CLOSED:      int = 1
    OPENING:     int = 2
    OPEN:        int = 3
    HOLDING:     int = 4
    CLOSING:     int = 5

    _STATE_NAMES: dict = {
        0: "CALIBRATING",
        1: "CLOSED",
        2: "OPENING",
        3: "OPEN",
        4: "HOLDING",
        5: "CLOSING",
    }

    def __init__(
        self,
        sample_rate:    int   = 44100,
        open_db:        float = _OPEN_THRESH_DB,
        close_db:       float = _CLOSE_THRESH_DB,
        attack_ms:      float = 10.0,
        hold_ms:        float = 200.0,
        release_ms:     float = 100.0,
        calib_db:       float = _CALIB_THRESH_DB,
        calib_samples:  int   = _CALIB_SAMPLES,
    ) -> None:
        self._sr = sample_rate

        # Convert dBFS thresholds to linear amplitude
        self._open_thresh:  float = 10.0 ** (open_db  / 20.0)
        self._close_thresh: float = 10.0 ** (close_db / 20.0)
        self._calib_thresh: float = 10.0 ** (calib_db / 20.0)
        self._calib_target: int   = calib_samples

        # Per-sample gain step magnitudes for attack and release ramps.
        # Divided by sample_rate so gain moves from 0→1 in exactly attack_ms.
        self._attack_rate:  float = 1.0 / max(1, attack_ms  * sample_rate / 1000.0)
        self._release_rate: float = 1.0 / max(1, release_ms * sample_rate / 1000.0)
        self._hold_samples: int   = int(hold_ms * sample_rate / 1000.0)

        # Mutable state
        self._state:        int   = self.CALIBRATING
        self._gain:         float = 1.0   # 1.0 during CALIBRATING, ramped otherwise
        self._hold_count:   int   = 0
        self._quiet_count:  int   = 0     # cumulative quiet samples during calibration
        self._env_level:    float = 0.0   # smoothed peak envelope

    # ── Read-only properties ─────────────────────────────────────────────────────

    @property
    def state(self) -> int:
        """Numeric state constant (CALIBRATING / CLOSED / OPENING / etc.)."""
        return self._state

    @property
    def state_name(self) -> str:
        """Human-readable state label for debugging."""
        return self._STATE_NAMES.get(self._state, "UNKNOWN")

    @property
    def is_calibrating(self) -> bool:
        """True while the initial noise-floor calibration is still running."""
        return self._state == self.CALIBRATING

    @property
    def is_open(self) -> bool:
        """True while audio is passing through (gate open or calibrating)."""
        return self._state in (self.CALIBRATING, self.OPENING, self.OPEN, self.HOLDING)

    @property
    def gain(self) -> float:
        """Current gate gain coefficient in [0.0, 1.0]."""
        return self._gain

    # ── Main processing entry-point ──────────────────────────────────────────────

    def process(self, chunk: np.ndarray) -> np.ndarray:
        """
        Apply gating to one mono audio chunk.

        The returned array has the same shape and dtype as the input.

          • CALIBRATING or OPEN/HOLDING : original samples (possibly gain-ramped)
          • CLOSED                      : all-zeros  →  analyzer decays to baseline
          • OPENING / CLOSING           : samples scaled by the current ramp gain

        Parameters
        ----------
        chunk : float32 ndarray — mono PCM audio, any length ≥ 0

        Returns
        -------
        float32 ndarray — gate-processed, same length as input
        """
        chunk = np.asarray(chunk, dtype=np.float32)
        n = len(chunk)
        if n == 0:
            return chunk

        # ── Envelope follower: instant attack, exponential release ────────────
        rms = float(np.sqrt(np.mean(chunk * chunk) + 1e-12))
        if rms > self._env_level:
            self._env_level = rms                  # instant attack
        else:
            # Decay constant ≈ 95% per chunk; actual rate depends on chunk size.
            self._env_level *= 0.95

        level = self._env_level

        # ── State machine ────────────────────────────────────────────────────

        if self._state == self.CALIBRATING:
            # Pass all audio through so the downstream analyzer can see the real
            # noise floor and build its calibration baseline.
            if level < self._calib_thresh:
                self._quiet_count += n
            else:
                # A signal spike was detected — reset the quiet counter so we
                # only finish calibration during a genuine uninterrupted silence.
                self._quiet_count = 0

            if self._quiet_count >= self._calib_target:
                # Enough consecutive quiet audio: calibration complete.
                # Transition to CLOSED; gate will open only on real signal.
                self._state = self.CLOSED
                self._gain  = 0.0

            # Always pass through during calibration regardless of signal level.
            return chunk

        elif self._state == self.CLOSED:
            if level >= self._open_thresh:
                self._state = self.OPENING
            # Return zeros whether we just transitioned or not; OPENING will be
            # applied on the *next* call once gain begins ramping.
            return np.zeros(n, dtype=np.float32)

        elif self._state == self.OPENING:
            # Ramp gain from 0 → 1 over attack_ms.  Rate is in units of (gain / sample).
            self._gain = min(1.0, self._gain + self._attack_rate * n)
            if self._gain >= 1.0:
                self._gain  = 1.0
                self._state = self.OPEN
            return chunk * np.float32(self._gain)

        elif self._state == self.OPEN:
            if level < self._close_thresh:
                self._hold_count = self._hold_samples
                self._state = self.HOLDING
            return chunk                           # gain = 1.0, no copy

        elif self._state == self.HOLDING:
            self._hold_count -= n
            if level >= self._close_thresh:
                # Signal came back up — stay open, cancel hold.
                self._state = self.OPEN
            elif self._hold_count <= 0:
                self._state = self.CLOSING
            return chunk                           # gain = 1.0 throughout hold

        elif self._state == self.CLOSING:
            # Ramp gain from current value → 0 over release_ms.
            self._gain = max(0.0, self._gain - self._release_rate * n)
            if level >= self._open_thresh:
                # Re-triggered during release — open again.
                self._state = self.OPENING
            elif self._gain <= 0.0:
                self._gain  = 0.0
                self._state = self.CLOSED
            return chunk * np.float32(self._gain)

        # Unreachable fallback — return input unchanged.
        return chunk

    def reset(self) -> None:
        """
        Reset the gate to its initial CALIBRATING state.

        Call this when the audio device changes or the project is reloaded so
        the gate re-calibrates from the new noise environment.
        """
        self._state       = self.CALIBRATING
        self._gain        = 1.0
        self._hold_count  = 0
        self._quiet_count = 0
        self._env_level   = 0.0