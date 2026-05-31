"""
offline_exporter_python.py  --  Pure-Python Offline Mix Bus
============================================================
Drop-in replacement for the C++ OfflineExporter when daw_processors.pyd
cannot be loaded (e.g. missing MSVC runtime, wrong architecture).

API mirrors the C++ class exactly so ExportWorker can switch between
them with a single try/except around the import.

    exporter = OfflineExporterPython()
    exporter.prepare(sample_rate, total_frames)
    exporter.mix_in(left_f32, right_f32, at_frame, gain)
    ok = exporter.write_wav(path, bit_depth)   # 16 | 24 | 32
    peak_l = exporter.peak_left()
    peak_r = exporter.peak_right()
"""

from __future__ import annotations

import struct
import wave
from typing import Optional

import numpy as np


class OfflineExporterPython:
    """
    Stereo float32 accumulation buffer with WAV export.

    All mixing is done at 32-bit float precision; the final write_wav()
    call quantises to the requested bit depth.
    """

    def __init__(self) -> None:
        self._sr: int = 44100
        self._buf_l: Optional[np.ndarray] = None
        self._buf_r: Optional[np.ndarray] = None
        self._peak_l: float = 0.0
        self._peak_r: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def prepare(self, sample_rate: int, total_frames: int) -> None:
        """Allocate the accumulation buffers and reset peak meters."""
        self._sr = int(sample_rate)
        self._buf_l = np.zeros(int(total_frames), dtype=np.float32)
        self._buf_r = np.zeros(int(total_frames), dtype=np.float32)
        self._peak_l = 0.0
        self._peak_r = 0.0

    # ── Mixing ────────────────────────────────────────────────────────────────

    def mix_in(
        self,
        left:     np.ndarray,
        right:    np.ndarray,
        at_frame: int,
        gain:     float,
    ) -> None:
        """
        Accumulate a stereo buffer into the mix bus starting at at_frame.

        Clips that extend past total_frames are silently truncated.
        """
        if self._buf_l is None:
            raise RuntimeError("Call prepare() before mix_in().")

        n          = min(len(left), len(right))
        buf_frames = len(self._buf_l)
        start      = int(at_frame)
        end        = min(start + n, buf_frames)
        src_n      = end - start

        if src_n <= 0:
            return

        self._buf_l[start:end] += left[:src_n]  * gain
        self._buf_r[start:end] += right[:src_n] * gain

    # ── WAV export ────────────────────────────────────────────────────────────

    def write_wav(self, path: str, bit_depth: int = 24) -> bool:
        """
        Write the accumulated mix to a WAV file.

        bit_depth must be 16, 24, or 32.  Returns True on success.
        """
        if self._buf_l is None or self._buf_r is None:
            return False

        if bit_depth not in (16, 24, 32):
            bit_depth = 24

        # Measure peaks before clipping so write_wav reports true signal level.
        self._peak_l = float(np.max(np.abs(self._buf_l))) if len(self._buf_l) else 0.0
        self._peak_r = float(np.max(np.abs(self._buf_r))) if len(self._buf_r) else 0.0

        try:
            if bit_depth == 32:
                return self._write_wav_float32(path)
            elif bit_depth == 16:
                return self._write_wav_int(path, 16)
            else:
                return self._write_wav_int24(path)
        except OSError:
            return False

    def _write_wav_float32(self, path: str) -> bool:
        """Write IEEE 754 float32 WAV (no clipping)."""
        n_frames = len(self._buf_l)
        # Interleave L/R into a single (n, 2) array.
        stereo = np.empty((n_frames, 2), dtype=np.float32)
        stereo[:, 0] = self._buf_l
        stereo[:, 1] = self._buf_r

        with open(path, "wb") as fh:
            # WAVE format with IEEE_FLOAT subtype requires a fmt chunk
            # with audio format 3 (IEEE float).
            n_bytes = stereo.nbytes
            fh.write(b"RIFF")
            fh.write(struct.pack("<I", 36 + n_bytes))
            fh.write(b"WAVE")
            # fmt chunk: audio format 3 = IEEE float
            fh.write(b"fmt ")
            fh.write(struct.pack("<IHHIIHH",
                                 16,          # chunk size
                                 3,           # IEEE float
                                 2,           # channels
                                 self._sr,
                                 self._sr * 2 * 4,  # byte rate
                                 8,           # block align
                                 32))         # bits per sample
            fh.write(b"data")
            fh.write(struct.pack("<I", n_bytes))
            fh.write(stereo.tobytes())
        return True

    def _write_wav_int(self, path: str, bits: int) -> bool:
        """Write 16-bit PCM WAV (standard, max compatibility)."""
        clip = np.clip(self._buf_l, -1.0, 1.0)
        clip_r = np.clip(self._buf_r, -1.0, 1.0)
        max_val = (1 << (bits - 1)) - 1
        int_l = (clip   * max_val).astype(np.int16)
        int_r = (clip_r * max_val).astype(np.int16)
        stereo = np.empty(len(int_l) * 2, dtype=np.int16)
        stereo[0::2] = int_l
        stereo[1::2] = int_r

        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(self._sr)
            wf.writeframes(stereo.tobytes())
        return True

    def _write_wav_int24(self, path: str) -> bool:
        """Write 24-bit PCM WAV (no stdlib support — pack manually)."""
        n = len(self._buf_l)
        clip_l = np.clip(self._buf_l, -1.0, 1.0)
        clip_r = np.clip(self._buf_r, -1.0, 1.0)

        MAX24 = (1 << 23) - 1
        int_l = (clip_l * MAX24).astype(np.int32)
        int_r = (clip_r * MAX24).astype(np.int32)

        # Each 24-bit sample = 3 bytes little-endian.
        n_bytes = n * 2 * 3  # stereo × 3 bytes

        with open(path, "wb") as fh:
            fh.write(b"RIFF")
            fh.write(struct.pack("<I", 36 + n_bytes))
            fh.write(b"WAVE")
            fh.write(b"fmt ")
            fh.write(struct.pack("<IHHIIHH",
                                 16,              # chunk size
                                 1,               # PCM
                                 2,               # channels
                                 self._sr,
                                 self._sr * 2 * 3,  # byte rate
                                 6,               # block align
                                 24))             # bits per sample
            fh.write(b"data")
            fh.write(struct.pack("<I", n_bytes))

            # Pack samples three bytes at a time.
            buf = bytearray(n_bytes)
            for i in range(n):
                l_val = int(int_l[i]) & 0xFFFFFF
                r_val = int(int_r[i]) & 0xFFFFFF
                offset = i * 6
                buf[offset]     =  l_val        & 0xFF
                buf[offset + 1] = (l_val >> 8)  & 0xFF
                buf[offset + 2] = (l_val >> 16) & 0xFF
                buf[offset + 3] =  r_val        & 0xFF
                buf[offset + 4] = (r_val >> 8)  & 0xFF
                buf[offset + 5] = (r_val >> 16) & 0xFF
            fh.write(buf)
        return True

    # ── Peak meters ───────────────────────────────────────────────────────────

    def peak_left(self) -> float:
        """Return the absolute peak sample value of the left channel."""
        return self._peak_l

    def peak_right(self) -> float:
        """Return the absolute peak sample value of the right channel."""
        return self._peak_r
