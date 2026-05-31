"""
waveform_peaks_python.py  --  Background Waveform Peak Generator
=================================================================
Pure-Python fallback for daw_processors.generate_peaks_from_array when
the C++ extension is unavailable.

Audio files are decoded and peak-reduced on daemon threads so the GUI
paint loop is never stalled by I/O or number crunching.  Results are
queued back to the caller via poll(), which the main refresh timer calls
every 50 ms to schedule a repaint.

Format support (tried in order):
    1. C++ daw_processors.generate_peaks_from_array  (fastest, all formats
       supported by the soundfile backend that was compiled with libsndfile)
    2. soundfile  (WAV, FLAC, OGG, AIFF, MP3 via libsndfile)
    3. scipy.io.wavfile  (PCM WAV only)
    4. stdlib wave  (8 / 16-bit PCM WAV only)

Architecture note:
    One WaveformPeakGenerator instance lives in MainWindow.  TrackArrangeView
    holds a reference set via set_waveform_generator().  The generator is NOT
    a QObject — it owns no Qt signals.  The repaint trigger is handled by
    MainWindow polling in _on_refresh_tick().
"""

from __future__ import annotations

import queue
import struct
import threading
import wave
from typing import Dict, List, Optional, Set

import numpy as np


# ---------------------------------------------------------------------------
# N_PEAKS default
# ---------------------------------------------------------------------------
# 2 000 peaks give sub-millisecond resolution for files up to ~90 s at 44.1 kHz
# and enough visual detail at any zoom level the arrange view can reach.
DEFAULT_N_PEAKS: int = 2000


# ---------------------------------------------------------------------------
# Low-level peak generation helpers (called from background threads)
# ---------------------------------------------------------------------------

def _peaks_from_array(data: np.ndarray, n_peaks: int) -> List[float]:
    """
    Compute n_peaks amplitude values from a float32 (frames × channels) array.

    Algorithm:
        1. Reduce channels → per-frame absolute maximum.
        2. Divide into n_peaks equal-sized chunks.
        3. Return the max value in each chunk, normalised to [0, 1].
    """
    # Reduce to a 1-D mono-absolute array.
    if data.ndim > 1:
        mono = np.abs(data).max(axis=1)
    else:
        mono = np.abs(data)

    n = len(mono)
    if n == 0:
        return []

    # Chunk size: how many frames map to one output peak.
    chunk = max(1, n // n_peaks)

    # Trim the array to a length that divides evenly, then reshape.
    n_use = (n // chunk) * chunk
    if n_use == 0:
        return []

    # shape: (actual_peaks, chunk) — take max of each row.
    chunks = mono[:n_use].reshape(-1, chunk)
    peaks_arr = chunks.max(axis=1)

    # If the reshape produced more peaks than requested, subsample.
    if len(peaks_arr) > n_peaks:
        idx = np.linspace(0, len(peaks_arr) - 1, n_peaks).astype(np.int32)
        peaks_arr = peaks_arr[idx]

    # Normalise so the loudest peak == 1.0.
    max_val = float(peaks_arr.max()) if len(peaks_arr) > 0 else 0.0
    if max_val > 0.0:
        peaks_arr = peaks_arr / max_val

    return peaks_arr.tolist()


def _peaks_from_wave_stdlib(path: str, n_peaks: int) -> Optional[List[float]]:
    """
    Read a PCM WAV file via the stdlib wave module (8 / 16-bit only).

    This is the last-resort fallback when soundfile and scipy are missing.
    Returns None if the file cannot be read.
    """
    try:
        with wave.open(path, "rb") as wf:
            n_ch     = wf.getnchannels()
            sw       = wf.getsampwidth()
            n_frames = wf.getnframes()

            if n_frames == 0 or sw not in (1, 2, 4):
                return None

            # Map sample width → struct format + normalisation scale.
            fmt   = {1: "B", 2: "h", 4: "i"}[sw]
            scale = {1: 128.0, 2: 32768.0, 4: 2147483648.0}[sw]

            chunk = max(1, n_frames // n_peaks)
            peaks: List[float] = []

            for _ in range(n_peaks):
                raw = wf.readframes(chunk)
                if not raw:
                    break
                n_samp = len(raw) // sw
                if n_samp == 0:
                    peaks.append(0.0)
                    continue
                try:
                    vals = struct.unpack_from(f"<{n_samp}{fmt}", raw)
                    # Mix all channels → mono.
                    mono = [
                        sum(vals[i : i + n_ch]) / n_ch
                        for i in range(0, n_samp, n_ch)
                    ]
                    peak = max(abs(s) for s in mono) / scale if mono else 0.0
                    peaks.append(min(1.0, peak))
                except struct.error:
                    peaks.append(0.0)

        if not peaks:
            return None

        # Normalise.
        max_p = max(peaks) or 1.0
        return [p / max_p for p in peaks]

    except Exception:
        return None


def _generate_peaks(path: str, n_peaks: int) -> Optional[List[float]]:
    """
    Generate waveform peaks for the audio file at *path*.

    Tries backends in order from most-capable to least:
        pedalboard → C++ → soundfile → scipy → stdlib wave.

    Returns None if every backend fails (unsupported format, missing file, …).
    """
    # ── Backend 0: pedalboard.io.AudioFile (WAV, MP3, OGG, FLAC, AIFF, M4A) ──
    # This is the same decoder used by AudioFilePlayer, so MP3 and every other
    # format that plays back will also produce a correct waveform thumbnail.
    try:
        from pedalboard.io import AudioFile  # type: ignore[import]
        with AudioFile(path) as f:
            # Read at native rate — resampling is unnecessary for peak detection.
            data = f.read(f.frames)  # shape: (channels, frames), float32
        data = data.T.astype(np.float32)  # → (frames, channels)
        return _peaks_from_array(data, n_peaks)
    except Exception:
        pass

    # ── Backend 1: C++ via daw_processors ────────────────────────────────────
    try:
        import daw_processors as dp  # type: ignore[import]
        if hasattr(dp, "generate_peaks_from_array"):
            import soundfile as sf  # type: ignore[import]
            data, _ = sf.read(path, dtype="float32", always_2d=True)
            flat = data.flatten()
            n_ch = data.shape[1]
            return list(dp.generate_peaks_from_array(flat, n_ch, n_peaks))
    except Exception:
        pass

    # ── Backend 2: soundfile (WAV, FLAC, OGG, AIFF; MP3 only if libsndfile
    #    was compiled with libminimp3 — not guaranteed on Windows) ────────────
    try:
        import soundfile as sf  # type: ignore[import]
        data, _ = sf.read(path, dtype="float32", always_2d=True)
        return _peaks_from_array(data, n_peaks)
    except Exception:
        pass

    # ── Backend 3: scipy (PCM WAV) ────────────────────────────────────────────
    try:
        from scipy.io import wavfile  # type: ignore[import]
        _, data = wavfile.read(path)
        if data.dtype.kind in ("i", "u"):
            # Convert integer PCM to float32 in [-1, 1].
            max_int = float(np.iinfo(data.dtype).max)
            data = data.astype(np.float32) / max_int
        else:
            data = data.astype(np.float32)
        if data.ndim == 1:
            data = data[:, np.newaxis]
        return _peaks_from_array(data, n_peaks)
    except Exception:
        pass

    # ── Backend 4: stdlib wave (8 / 16-bit PCM WAV only) ─────────────────────
    return _peaks_from_wave_stdlib(path, n_peaks)


# ---------------------------------------------------------------------------
# WaveformPeakGenerator
# ---------------------------------------------------------------------------

class WaveformPeakGenerator:
    """
    Background peak loader and cache manager.

    Usage
    -----
    1.  Create one instance in MainWindow.__init__().
    2.  Call set_waveform_generator(gen) on TrackArrangeView.
    3.  In TrackArrangeView._draw_audio_lane() call gen.get_peaks(path).
        - Returns a list[float] immediately when the file is cached.
        - Returns None on first call and starts a background load.
    4.  In MainWindow._on_refresh_tick() call gen.poll().
        - Returns True when at least one new set of peaks became ready.
        - Caller schedules arrange_view.update() in that case.
    """

    def __init__(self) -> None:
        # Peak cache: path → list of floats (or None if unreadable).
        self._cache:   Dict[str, Optional[List[float]]] = {}
        # Paths currently being loaded in background threads.
        self._loading: Set[str]                         = set()
        # Thread-safe lock protecting _cache and _loading.
        self._lock     = threading.Lock()
        # Paths whose background load just finished — drained by poll().
        self._ready_queue: queue.Queue[str]             = queue.Queue()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_peaks(
        self,
        path:    str,
        n_peaks: int = DEFAULT_N_PEAKS,
    ) -> Optional[List[float]]:
        """
        Return cached peaks for *path*, or None while loading.

        On the first call for a given path a daemon thread is spawned to
        load the file.  Subsequent calls return None until the thread
        finishes and poll() has been called at least once.
        """
        with self._lock:
            if path in self._cache:
                return self._cache[path]
            if path not in self._loading:
                # Start a background load for this path.
                self._loading.add(path)
                t = threading.Thread(
                    target=self._load_in_background,
                    args=(path, n_peaks),
                    daemon=True,
                )
                t.start()
        return None

    def poll(self) -> bool:
        """
        Drain the result queue of completed loads.

        Returns True if at least one load finished since the last call.
        The caller should schedule a repaint of the arrange view.
        """
        changed = False
        try:
            while True:
                self._ready_queue.get_nowait()
                changed = True
        except queue.Empty:
            pass
        return changed

    def invalidate(self, path: str) -> None:
        """Remove a cached entry so the file is reloaded on the next request."""
        with self._lock:
            self._cache.pop(path, None)

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_in_background(self, path: str, n_peaks: int) -> None:
        """Worker executed on a daemon thread: generate peaks and queue result."""
        peaks = _generate_peaks(path, n_peaks)
        with self._lock:
            self._cache[path] = peaks
            self._loading.discard(path)
        # Notify the GUI thread via a thread-safe queue.
        self._ready_queue.put(path)
