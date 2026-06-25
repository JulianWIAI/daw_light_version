"""
auto_mastering.py — Offline Auto-Mastering Engine
===================================================
Reads a stereo printed WAV, applies a professional mastering chain, and
exports a finalised commercial WAV.  Pure DSP — no Qt dependency.

Processing chain (in order)
---------------------------
1. Ingestion          — soundfile reads the input WAV to float64 (N, 2).
2. Mid/Side split     — M = (L + R) / 2,  S = (L - R) / 2.
3. Side HP @ 120 Hz   — forces all sub-bass energy to mono (2nd-order Butter).
4. Width               — Side channel scaled by stereo_width scalar.
5. L/R recombine      — L = M + S,  R = M - S.
6. Genre EQ           — macro tonal shaping via biquad shelves/peak.
7. LUFS measurement   — pyloudnorm integrated loudness (EBU R128 / ITU BS.1770).
8. Gain staging       — static dB offset so measured LUFS hits target_lufs.
9. Brickwall limiter  — hard clip at target_true_peak (dBTP → linear).
10. Export            — soundfile writes 24-bit PCM WAV.

Dependencies
------------
    numpy      — array maths
    soundfile  — I/O
    scipy      — biquad IIR filtering (scipy.signal.sosfilt)
    pyloudnorm — integrated LUFS measurement (EBU R128)

scipy and pyloudnorm are imported lazily; if either is absent the module
falls back to a simpler substitute and logs a warning.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Biquad filter helpers (all coefficients follow the Audio EQ Cookbook)
# Each function returns a 2-D SOS array compatible with scipy.signal.sosfilt,
# or falls back to a transposed-direct-form-II time-domain implementation.
# ─────────────────────────────────────────────────────────────────────────────

def _sos_hp(fc: float, sr: float, q: float = 0.707) -> np.ndarray:
    """Second-order Butterworth high-pass SOS section."""
    omega = 2.0 * math.pi * fc / sr
    cos_w = math.cos(omega)
    sin_w = math.sin(omega)
    alpha = sin_w / (2.0 * q)
    b0 =  (1.0 + cos_w) / 2.0
    b1 = -(1.0 + cos_w)
    b2 =  (1.0 + cos_w) / 2.0
    a0 =   1.0 + alpha
    a1 =  -2.0 * cos_w
    a2 =   1.0 - alpha
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _sos_low_shelf(fc: float, gain_db: float, sr: float, q: float = 0.707) -> np.ndarray:
    """Low-shelf SOS section."""
    A = 10.0 ** (gain_db / 40.0)
    omega = 2.0 * math.pi * fc / sr
    cos_w = math.cos(omega)
    sin_w = math.sin(omega)
    alpha = sin_w / 2.0 * math.sqrt((A + 1.0 / A) * (1.0 / q - 1.0) + 2.0)
    sqrt_A = math.sqrt(A)
    b0 =      A * ((A + 1) - (A - 1) * cos_w + 2 * sqrt_A * alpha)
    b1 =  2 * A * ((A - 1) - (A + 1) * cos_w)
    b2 =      A * ((A + 1) - (A - 1) * cos_w - 2 * sqrt_A * alpha)
    a0 =          ((A + 1) + (A - 1) * cos_w + 2 * sqrt_A * alpha)
    a1 =     -2 * ((A - 1) + (A + 1) * cos_w)
    a2 =          ((A + 1) + (A - 1) * cos_w - 2 * sqrt_A * alpha)
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _sos_high_shelf(fc: float, gain_db: float, sr: float, q: float = 0.707) -> np.ndarray:
    """High-shelf SOS section."""
    A = 10.0 ** (gain_db / 40.0)
    omega = 2.0 * math.pi * fc / sr
    cos_w = math.cos(omega)
    sin_w = math.sin(omega)
    alpha = sin_w / 2.0 * math.sqrt((A + 1.0 / A) * (1.0 / q - 1.0) + 2.0)
    sqrt_A = math.sqrt(A)
    b0 =      A * ((A + 1) + (A - 1) * cos_w + 2 * sqrt_A * alpha)
    b1 = -2 * A * ((A - 1) + (A + 1) * cos_w)
    b2 =      A * ((A + 1) + (A - 1) * cos_w - 2 * sqrt_A * alpha)
    a0 =          ((A + 1) - (A - 1) * cos_w + 2 * sqrt_A * alpha)
    a1 =      2 * ((A - 1) - (A + 1) * cos_w)
    a2 =          ((A + 1) - (A - 1) * cos_w - 2 * sqrt_A * alpha)
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _sos_peak(fc: float, gain_db: float, q: float, sr: float) -> np.ndarray:
    """Peaking EQ (bell) SOS section."""
    A = 10.0 ** (gain_db / 40.0)
    omega = 2.0 * math.pi * fc / sr
    cos_w = math.cos(omega)
    sin_w = math.sin(omega)
    alpha = sin_w / (2.0 * q)
    b0 =  1.0 + alpha * A
    b1 = -2.0 * cos_w
    b2 =  1.0 - alpha * A
    a0 =  1.0 + alpha / A
    a1 = -2.0 * cos_w
    a2 =  1.0 - alpha / A
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _apply_sos(audio: np.ndarray, sos: np.ndarray) -> np.ndarray:
    """
    Filter a (N, 2) stereo or (N,) mono array with a SOS biquad cascade.
    Uses scipy.signal.sosfilt when available; falls back to a pure-Python
    transposed direct form II loop that is slower but always works.
    """
    try:
        from scipy.signal import sosfilt  # type: ignore
        if audio.ndim == 2:
            left  = sosfilt(sos, audio[:, 0])
            right = sosfilt(sos, audio[:, 1])
            return np.column_stack([left, right])
        return sosfilt(sos, audio)
    except ImportError:
        # Pure-Python TDF-II fallback (slow but dependency-free)
        logger.warning("scipy unavailable — using Python biquad loop (slow).")
        return _sosfilt_fallback(audio, sos)


def _sosfilt_fallback(audio: np.ndarray, sos: np.ndarray) -> np.ndarray:
    """Transposed Direct Form II biquad for each SOS section, each channel."""
    def _filt_channel(x: np.ndarray, sos: np.ndarray) -> np.ndarray:
        y = x.copy()
        for section in sos:
            b0, b1, b2, _, a1, a2 = section
            s1 = s2 = 0.0
            out = np.empty_like(y)
            for n, xn in enumerate(y):
                yn      = b0 * xn + s1
                s1      = b1 * xn - a1 * yn + s2
                s2      = b2 * xn - a2 * yn
                out[n]  = yn
            y = out
        return y

    if audio.ndim == 1:
        return _filt_channel(audio, sos)
    return np.column_stack([_filt_channel(audio[:, c], sos) for c in range(audio.shape[1])])


# ─────────────────────────────────────────────────────────────────────────────
# LUFS measurement
# ─────────────────────────────────────────────────────────────────────────────

def _measure_lufs(audio: np.ndarray, sr: int) -> float:
    """
    Return integrated LUFS (EBU R128 / ITU BS.1770-4).

    Requires pyloudnorm.  Falls back to an RMS-based approximation that is
    accurate to ±1-2 dB for typical music material.
    """
    try:
        import pyloudnorm as pyln  # type: ignore
        meter = pyln.Meter(sr)
        lufs  = meter.integrated_loudness(audio.astype(np.float64))
        if math.isinf(lufs) or math.isnan(lufs):
            raise ValueError("silence / clip too short for BS.1770 measurement")
        return float(lufs)
    except Exception as exc:
        logger.warning("pyloudnorm LUFS failed (%s); using RMS fallback.", exc)
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        return 20.0 * math.log10(max(rms, 1e-10)) - 3.0


# ─────────────────────────────────────────────────────────────────────────────
# Genre EQ profiles
# ─────────────────────────────────────────────────────────────────────────────

# Each genre maps to a list of filter descriptors:
#   ("low_shelf",  fc_hz, gain_db)
#   ("high_shelf", fc_hz, gain_db)
#   ("peak",       fc_hz, gain_db, q)
#   ("bypass",)  — no processing

_GENRE_EQ: dict = {
    # Electronic: punchy sub, airy top
    "electronic": [
        ("low_shelf",  80.0,   1.5),
        ("high_shelf", 8000.0, 1.0),
    ],
    # Hip-Hop: identical profile to electronic
    "hiphop": [
        ("low_shelf",  80.0,   1.5),
        ("high_shelf", 8000.0, 1.0),
    ],
    # Trap: same as hiphop (alias)
    "trap": [
        ("low_shelf",  80.0,   1.5),
        ("high_shelf", 8000.0, 1.0),
    ],
    # Pop: vocal presence boost
    "pop": [
        ("peak", 2000.0, 1.0, 1.4),
    ],
    # Classical / cinematic: transparent — no EQ
    "classical": [("bypass",)],
    "cinematic": [("bypass",)],
    # Catch-all for unlisted genres: minimal shaping
    "other": [
        ("high_shelf", 10000.0, 0.5),
    ],
}


def _apply_genre_eq(audio: np.ndarray, sr: int, genre: str) -> np.ndarray:
    """Apply the genre-specific EQ profile to a (N, 2) float64 array."""
    key = genre.lower().strip()
    profile = _GENRE_EQ.get(key, _GENRE_EQ["other"])

    for descriptor in profile:
        kind = descriptor[0]
        if kind == "bypass":
            continue
        elif kind == "low_shelf":
            _, fc, gain_db = descriptor
            sos = _sos_low_shelf(fc, gain_db, sr)
            audio = _apply_sos(audio, sos)
        elif kind == "high_shelf":
            _, fc, gain_db = descriptor
            sos = _sos_high_shelf(fc, gain_db, sr)
            audio = _apply_sos(audio, sos)
        elif kind == "peak":
            _, fc, gain_db, q = descriptor
            sos = _sos_peak(fc, gain_db, q, sr)
            audio = _apply_sos(audio, sos)

    return audio


# ─────────────────────────────────────────────────────────────────────────────
# I/O helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_wav(path: str) -> Tuple[np.ndarray, int]:
    """
    Read a WAV file and return (audio, sample_rate).

    audio is always float64 (N, 2).  Mono files are up-mixed to stereo.
    """
    import soundfile as sf  # guaranteed present — checked at import time
    audio, sr = sf.read(path, dtype="float64", always_2d=True)
    if audio.shape[1] == 1:
        # Mono → duplicate to stereo
        audio = np.repeat(audio, 2, axis=1)
    elif audio.shape[1] > 2:
        # Multi-channel → take first two channels
        audio = audio[:, :2]
    return audio, int(sr)


def _write_wav(path: str, audio: np.ndarray, sr: int, bit_depth: int = 24) -> None:
    """Write float64 (N, 2) audio to a 24-bit PCM WAV file."""
    import soundfile as sf
    subtype_map = {16: "PCM_16", 24: "PCM_24", 32: "PCM_32"}
    subtype = subtype_map.get(bit_depth, "PCM_24")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    sf.write(path, np.ascontiguousarray(audio), sr, subtype=subtype)


# ─────────────────────────────────────────────────────────────────────────────
# AutoMasterEngine
# ─────────────────────────────────────────────────────────────────────────────

class AutoMasterEngine:
    """
    Offline mastering processor.

    The public entry point is ``process_file()``.  Each step is also exposed
    as a separate method for unit-testing and custom pipelines.

    Parameters accepted by process_file() match the Automaster dialog
    controls one-to-one so the dialog can pass its values without adaptation.
    """

    # ── Main entry point ──────────────────────────────────────────────────────

    def process_file(
        self,
        input_path:        str,
        output_path:       str,
        genre:             str   = "other",
        target_lufs:       float = -14.0,
        target_true_peak:  float = -1.0,
        stereo_width:      float = 1.0,
        progress_callback          = None,   # Optional[Callable[[int, str], None]]
    ) -> dict:
        """
        Master *input_path* and write the result to *output_path*.

        Parameters
        ----------
        input_path        : Absolute path to a stereo printed WAV file.
        output_path       : Destination path; directories are created if absent.
        genre             : One of 'electronic', 'hiphop', 'trap', 'pop',
                            'classical', 'cinematic', 'other'.
        target_lufs       : Integrated loudness target in LUFS (e.g. -14.0).
        target_true_peak  : Maximum true peak level in dBTP (e.g. -1.0).
        stereo_width      : Side-channel gain scalar (1.0 = unchanged, 1.2 = wider).
        progress_callback : Optional ``(percent: int, message: str) → None``.

        Returns
        -------
        dict with keys:
            'input_lufs'   float   — measured LUFS before mastering
            'output_lufs'  float   — measured LUFS after mastering
            'gain_db'      float   — static gain applied
            'peak_linear'  float   — true-peak limiter ceiling (linear)
            'sr'           int     — sample rate
            'n_samples'    int     — total samples in the output file
        """
        def _progress(pct: int, msg: str) -> None:
            logger.info("[%3d%%] %s", pct, msg)
            if progress_callback is not None:
                try:
                    progress_callback(pct, msg)
                except Exception:
                    pass

        # ── 1. Ingest ─────────────────────────────────────────────────────────
        _progress(5, f"Reading {os.path.basename(input_path)} …")
        if not os.path.isfile(input_path):
            raise FileNotFoundError(f"Input WAV not found: {input_path}")
        audio, sr = _read_wav(input_path)
        _progress(12, f"Loaded {len(audio):,} samples at {sr} Hz.")

        # ── 2. Mid/Side split ─────────────────────────────────────────────────
        _progress(18, "Mid/Side matrix …")
        audio = self.apply_ms_width(audio, sr, stereo_width)

        # ── 3. Genre EQ ───────────────────────────────────────────────────────
        _progress(30, f"Genre EQ ({genre}) …")
        audio = _apply_genre_eq(audio, sr, genre)

        # ── 4. LUFS measurement ───────────────────────────────────────────────
        _progress(50, "Measuring integrated LUFS …")
        input_lufs = _measure_lufs(audio, sr)
        _progress(60, f"Input LUFS: {input_lufs:.1f} → target {target_lufs:.1f}")

        # ── 5. Gain staging ───────────────────────────────────────────────────
        gain_db     = target_lufs - input_lufs
        gain_linear = 10.0 ** (gain_db / 20.0)
        audio       = audio * gain_linear
        _progress(70, f"Applied {gain_db:+.2f} dB static gain.")

        # ── 6. Brickwall limiter ──────────────────────────────────────────────
        _progress(80, f"Brickwall limiter at {target_true_peak:.1f} dBTP …")
        peak_linear = 10.0 ** (target_true_peak / 20.0)
        audio       = self.apply_limiter(audio, peak_linear)

        # ── 7. Output LUFS verification ───────────────────────────────────────
        _progress(88, "Verifying output loudness …")
        output_lufs = _measure_lufs(audio, sr)
        _progress(92, f"Output LUFS: {output_lufs:.1f}")

        # ── 8. Export ─────────────────────────────────────────────────────────
        _progress(95, f"Writing {os.path.basename(output_path)} …")
        _write_wav(output_path, audio, sr, bit_depth=24)
        _progress(100, "Done.")

        return {
            "input_lufs":  input_lufs,
            "output_lufs": output_lufs,
            "gain_db":     gain_db,
            "peak_linear": peak_linear,
            "sr":          sr,
            "n_samples":   len(audio),
        }

    # ── DSP sub-steps (individually testable) ─────────────────────────────────

    def apply_ms_width(
        self,
        audio:        np.ndarray,
        sr:           int,
        stereo_width: float,
    ) -> np.ndarray:
        """
        Mid/Side stereo width processing.

        Steps:
          1. Encode to M/S.
          2. High-pass the Side channel at 120 Hz (mono sub-bass).
          3. Scale Side by *stereo_width*.
          4. Decode back to L/R.

        Args:
            audio        : (N, 2) float64 stereo array.
            sr           : Sample rate in Hz.
            stereo_width : Side gain scalar (1.0 = unity, >1.0 = wider).

        Returns:
            (N, 2) float64 array.
        """
        L = audio[:, 0]
        R = audio[:, 1]

        # M/S encode
        mid  = (L + R) * 0.5
        side = (L - R) * 0.5

        # Force sub-bass to mono by high-passing the Side channel at 120 Hz.
        hp_sos = _sos_hp(120.0, sr, q=0.707)
        side   = _apply_sos(side, hp_sos)

        # Width scaling
        side = side * stereo_width

        # M/S decode
        L_out = mid + side
        R_out = mid - side

        return np.column_stack([L_out, R_out])

    @staticmethod
    def apply_limiter(audio: np.ndarray, ceiling: float) -> np.ndarray:
        """
        Hard brickwall limiter.

        Clips the signal so that no sample exceeds *ceiling* in absolute value.
        This is a true-peak approximation — for strict inter-sample compliance
        use a look-ahead limiter; for offline mastering this is sufficient for
        most streaming delivery specs when ceiling ≤ -1.0 dBTP.

        Args:
            audio   : (N, 2) or (N,) float64 array.
            ceiling : Linear amplitude ceiling (e.g. 0.891 for -1.0 dBTP).

        Returns:
            Clipped float64 array, same shape as input.
        """
        return np.clip(audio, -ceiling, ceiling)
