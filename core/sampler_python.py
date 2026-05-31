"""
sampler_python.py  --  Pure-Python Polyphonic Sampler Fallback
==============================================================
Provides PythonSampler, a drop-in replacement for daw_processors.Sampler
that is used when the C++ extension cannot be loaded (e.g. missing MSVC
runtime on Windows or unsupported platform).

All voice rendering uses vectorised NumPy operations — no per-sample
Python loops — so it is fast enough for real-time use at typical block
sizes (512–2048 frames).

Interface matches daw_processors.Sampler exactly:
    __init__(sample_rate)
    load_sample(flat, source_sr, channels)
    note_on(midi_note, velocity)
    note_off(midi_note)
    process_block(left, right) -> (left, right)
    sample_loaded() -> bool
    active_voice_count() -> int
    set_root_note(n)
    set_attack_ms(ms)
    set_decay_ms(ms)
    set_sustain(lvl)
    set_release_ms(ms)
"""

from __future__ import annotations

import threading

import numpy as np

MAX_VOICES = 8


class _Voice:
    """Single sampler voice with numpy-vectorized rendering."""

    def __init__(self, note, velocity, root_note, sample_l, sample_r,
                 source_sr, target_sr, attack_ms, decay_ms, sustain_lvl, release_ms):
        self.note = note
        self.vel  = max(0.0, min(1.0, velocity))
        pitch_ratio    = 2.0 ** ((note - root_note) / 12.0)
        self.advance   = pitch_ratio * source_sr / target_sr
        self.sample_l  = sample_l   # (N,) float32
        self.sample_r  = sample_r
        self.n_frames  = len(sample_l)
        self.pos       = 0.0        # current read position (fractional)
        # ADSR in frames
        sr = target_sr
        self.att_f  = max(1, int(attack_ms  * sr / 1000))
        self.dec_f  = max(1, int(decay_ms   * sr / 1000))
        self.sus    = float(sustain_lvl)
        self.rel_f  = max(1, int(release_ms * sr / 1000))
        # Envelope state
        self.state     = 'attack'   # attack | decay | sustain | release | done
        self.env_frame = 0          # frame counter within current state
        self.env_level = 0.0        # current envelope level
        self.rel_start = 0.0

    def note_off(self):
        if self.state not in ('release', 'done'):
            self.rel_start = self.env_level
            self.state     = 'release'
            self.env_frame = 0

    @property
    def done(self) -> bool:
        return self.state == 'done'

    def render(self, n: int) -> tuple:
        """Render up to n frames. Returns (l, r) float32 arrays of length n."""
        if self.done:
            return np.zeros(n, np.float32), np.zeros(n, np.float32)

        # --- build envelope curve for this block ---
        env = np.empty(n, dtype=np.float32)
        remaining = n
        written   = 0
        while remaining > 0 and self.state != 'done':
            if self.state == 'attack':
                avail = self.att_f - self.env_frame
                take  = min(avail, remaining)
                t     = np.arange(self.env_frame, self.env_frame + take, dtype=np.float32)
                env[written:written+take] = t / self.att_f
                self.env_frame += take
                if self.env_frame >= self.att_f:
                    self.env_level = 1.0
                    self.state     = 'decay'
                    self.env_frame = 0
            elif self.state == 'decay':
                avail = self.dec_f - self.env_frame
                take  = min(avail, remaining)
                t     = np.arange(self.env_frame, self.env_frame + take, dtype=np.float32)
                env[written:written+take] = 1.0 - (1.0 - self.sus) * t / self.dec_f
                self.env_frame += take
                if self.env_frame >= self.dec_f:
                    self.env_level = self.sus
                    self.state     = 'sustain'
            elif self.state == 'sustain':
                env[written:written+remaining] = self.sus
                written   += remaining
                remaining  = 0
                break
            elif self.state == 'release':
                avail = self.rel_f - self.env_frame
                take  = min(avail, remaining)
                t     = np.arange(self.env_frame, self.env_frame + take, dtype=np.float32)
                env[written:written+take] = self.rel_start * (1.0 - t / self.rel_f)
                self.env_frame += take
                if self.env_frame >= self.rel_f:
                    self.state = 'done'
            written   += take
            remaining -= take

        # Zero-fill if we hit 'done' before filling the block
        if written < n:
            env[written:] = 0.0

        # --- build read positions ---
        positions = np.arange(n, dtype=np.float64) * self.advance + self.pos
        end_idx   = positions < (self.n_frames - 1)
        # Clamp positions so np.interp doesn't extrapolate
        positions_c = np.clip(positions, 0.0, self.n_frames - 1.0001)
        src_idx = np.arange(self.n_frames, dtype=np.float32)
        l = np.interp(positions_c, src_idx, self.sample_l).astype(np.float32)
        r = np.interp(positions_c, src_idx, self.sample_r).astype(np.float32)

        l *= env * self.vel
        r *= env * self.vel

        # Zero out frames past end of sample
        if not np.all(end_idx):
            l[~end_idx] = 0.0
            r[~end_idx] = 0.0
            self.state = 'done'

        self.pos = float(positions[-1]) + self.advance if not self.done else self.pos
        return l, r


class PythonSampler:
    """Pure-Python polyphonic sampler — same interface as daw_processors.Sampler."""

    def __init__(self, sample_rate: float = 44100.0) -> None:
        self._sr         = float(sample_rate)
        self._root       = 60
        self._attack_ms  = 5.0
        self._decay_ms   = 100.0
        self._sustain    = 0.8
        self._release_ms = 300.0
        self._sample_l   = None   # (N,) float32
        self._sample_r   = None
        self._source_sr  = 44100.0
        self._voices     = []     # list[_Voice]
        self._lock       = threading.Lock()

    def load_sample(self, flat: np.ndarray, source_sr: float, channels: int) -> None:
        flat = np.asarray(flat, dtype=np.float32)
        if channels == 2:
            l = flat[0::2]
            r = flat[1::2]
        else:
            l = r = flat
        with self._lock:
            self._sample_l  = l.copy()
            self._sample_r  = r.copy()
            self._source_sr = float(source_sr)
            self._voices.clear()

    def sample_loaded(self) -> bool:
        return self._sample_l is not None

    def note_on(self, midi_note: int, velocity: float) -> None:
        if not self.sample_loaded():
            return
        with self._lock:
            # Kill same note already playing (retrigger)
            self._voices = [v for v in self._voices if v.note != midi_note]
            if len(self._voices) >= MAX_VOICES:
                self._voices.pop(0)   # steal oldest
            v = _Voice(midi_note, velocity, self._root,
                       self._sample_l, self._sample_r,
                       self._source_sr, self._sr,
                       self._attack_ms, self._decay_ms,
                       self._sustain, self._release_ms)
            self._voices.append(v)

    def note_off(self, midi_note: int) -> None:
        with self._lock:
            for v in self._voices:
                if v.note == midi_note:
                    v.note_off()

    def process_block(self, left: np.ndarray, right: np.ndarray):
        n = len(left)
        out_l = left.copy()
        out_r = right.copy()
        with self._lock:
            alive = []
            for v in self._voices:
                vl, vr = v.render(n)
                out_l += vl
                out_r += vr
                if not v.done:
                    alive.append(v)
            self._voices = alive
        return out_l, out_r

    def active_voice_count(self) -> int:
        return len(self._voices)

    def set_root_note(self, n: int) -> None:    self._root       = int(n)
    def set_attack_ms(self, ms: float) -> None: self._attack_ms  = float(ms)
    def set_decay_ms(self, ms: float) -> None:  self._decay_ms   = float(ms)
    def set_sustain(self, lvl: float) -> None:  self._sustain    = float(lvl)
    def set_release_ms(self, ms: float) -> None: self._release_ms = float(ms)
