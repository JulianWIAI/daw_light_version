"""
audio_file_player.py -- Multi-Track Audio File Playback Engine
===============================================================
Plays audio-file clips with per-track DSP effects using a sounddevice
mixing callback.  Replaces the previous pygame.mixer backend.

Architecture:
    One sounddevice.OutputStream runs a real-time mixing callback.
    Each active clip is stored as a float32 (samples, 2) numpy array.
    The callback sums all active clips frame-by-frame into the output
    buffer, advancing each clip's read position on every call.

    When play_clip() is called (from the MidiLogic playback thread):
        1. A background thread loads the file via pedalboard.io.AudioFile
           and resamples it to SAMPLE_RATE automatically.
        2. The AudioFxChain applies EQ/reverb/compression offline.
        3. Volume and pan are applied via numpy.
        4. The result is inserted into the mixer dict under the track_id,
           replacing any clip that was already playing on that track.

Works on macOS (CoreAudio), Windows (WASAPI shared), and Linux (ALSA/PulseAudio).
No pygame dependency.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Tuple

import numpy as np

from .audio_fx_chain import AudioFxChain

logger = logging.getLogger(__name__)

SAMPLE_RATE  = 44100
CHANNELS     = 2
BLOCK_SIZE   = 1024
DTYPE        = "float32"


class AudioFilePlayer:
    """
    Multi-track audio file playback engine built on a sounddevice mixing stream.

    One instance lives in MainWindow and receives play_clip() calls from the
    MidiLogic playback thread via the audio callback.
    """

    SAMPLE_RATE = SAMPLE_RATE

    def __init__(self) -> None:
        self._fx_chains:   Dict[int, AudioFxChain] = {}
        self._channel_map: Dict[int, int]          = {}   # kept for API compat
        self._last_played: Dict[int, Tuple]        = {}

        # Mixer state: track_id -> (audio_array, current_position)
        self._active: Dict[int, Tuple[np.ndarray, int]] = {}
        self._mix_lock = threading.Lock()

        self._master_bus = None
        self._stream     = None
        self._stream_ok  = False
        self._next_ch    = 4   # legacy compat

        # Optional telemetry sink — identical pattern to AudioEngine / SfzRealTimePlayer.
        # Set externally after construction; called from _sd_callback with a mono float32 array.
        self._telemetry_push = None

        self._start_stream()

    # -------------------------------------------------------------------------
    # Stream lifecycle
    # -------------------------------------------------------------------------

    def _start_stream(self) -> None:
        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=BLOCK_SIZE,
                callback=self._sd_callback,
            )
            self._stream.start()
            self._stream_ok = True
            logger.info("AudioFilePlayer: sounddevice mixing stream started at %d Hz.", SAMPLE_RATE)
        except Exception as exc:
            logger.warning("AudioFilePlayer: could not open sounddevice stream — %s", exc)

    def _sd_callback(self, outdata: np.ndarray, frames: int, time, status) -> None:
        """Real-time mixing callback — runs on the audio thread, no Python allocation."""
        mixed = np.zeros((frames, CHANNELS), dtype=np.float32)
        finished = []

        with self._mix_lock:
            for tid, (audio, pos) in self._active.items():
                chain = self._fx_chains.get(tid)
                if chain is not None and chain.muted:
                    finished.append(tid)
                    continue

                end   = min(pos + frames, len(audio))
                chunk = audio[pos:end].copy()
                n     = len(chunk)
                if n > 0:
                    vol = float(chain.volume) if chain is not None else 1.0
                    pan = float(chain.pan)    if chain is not None else 0.0
                    chunk *= vol
                    if pan != 0.0 and chunk.ndim == 2 and chunk.shape[1] == 2:
                        chunk[:, 0] *= max(0.0, min(1.0, 1.0 - pan))
                        chunk[:, 1] *= max(0.0, min(1.0, 1.0 + pan))
                    mixed[:n] += chunk
                if end >= len(audio):
                    finished.append(tid)
                else:
                    self._active[tid] = (audio, end)
            for tid in finished:
                self._active.pop(tid, None)

        np.clip(mixed, -1.0, 1.0, out=mixed)
        outdata[:] = mixed

        # Push the mono mix to the telemetry analyzer so the freq-band and H/P
        # panels reflect the post-EQ audio from audio-file tracks in real time.
        if self._telemetry_push is not None:
            try:
                self._telemetry_push((mixed[:, 0] + mixed[:, 1]) * 0.5)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Track registration (kept for API compatibility with gui_windows)
    # -------------------------------------------------------------------------

    def _init_pygame(self) -> None:
        """No-op: kept so old call sites in gui_windows don't error."""

    def register_track(self, track_id: int, fx_chain: Optional[AudioFxChain] = None) -> None:
        with self._mix_lock:
            if track_id not in self._channel_map:
                self._channel_map[track_id] = self._next_ch
                self._next_ch += 1
        chain = fx_chain if fx_chain is not None else AudioFxChain(track_id=track_id)
        self._fx_chains[track_id] = chain

    def unregister_track(self, track_id: int) -> None:
        self.stop_track(track_id)
        with self._mix_lock:
            self._fx_chains.pop(track_id, None)
            self._channel_map.pop(track_id, None)
            self._last_played.pop(track_id, None)

    def update_fx_chain(self, track_id: int, chain: AudioFxChain) -> None:
        """Replace FX chain and re-render any currently-playing clip."""
        with self._mix_lock:
            self._fx_chains[track_id] = chain
            last = self._last_played.get(track_id)
            active = self._active.get(track_id)

        # DIAGNOSTIC ── probe 4: chain update gate ────────────────────────────
        plugin_names = [
            f"{getattr(p, 'DISPLAY_NAME', '?')}(enabled={getattr(p, 'enabled', '?')})"
            for p in (chain.plugins if chain else []) if p is not None
        ]
        logger.info(
            "[DIAG] update_fx_chain() track=%d | chain_plugins=%s | "
            "is_playing=%s | will_rerender=%s",
            track_id, plugin_names,
            active is not None,
            last is not None and active is not None,
        )
        # ─────────────────────────────────────────────────────────────────────

        if last is not None and active is not None:
            audio, pos = active
            elapsed_secs = pos / SAMPLE_RATE
            path, duration_secs = last
            self._play_clip_from_offset(track_id, path, duration_secs, elapsed_secs)

    def get_fx_chain(self, track_id: int) -> Optional[AudioFxChain]:
        return self._fx_chains.get(track_id)

    def set_master_bus(self, bus) -> None:
        self._master_bus = bus

    # -------------------------------------------------------------------------
    # Playback control
    # -------------------------------------------------------------------------

    def play_clip(self, track_id: int, path: str, duration_secs: float) -> None:
        self._play_clip_from_offset(track_id, path, duration_secs, 0.0)

    def _play_clip_from_offset(
        self,
        track_id:          int,
        path:              str,
        duration_secs:     float,
        start_offset_secs: float,
    ) -> None:
        if not self._stream_ok:
            return

        with self._mix_lock:
            chain = self._fx_chains.get(track_id)

        if chain is not None and chain.muted:
            return
        if self._any_soloed() and not (chain and chain.soloed):
            return

        t = threading.Thread(
            target=self._load_and_play,
            args=(track_id, path, duration_secs, chain, start_offset_secs),
            daemon=True,
            name=f"AudioPlayer-{track_id}",
        )
        t.start()

    def stop_track(self, track_id: int) -> None:
        with self._mix_lock:
            self._active.pop(track_id, None)

    def stop_all(self) -> None:
        with self._mix_lock:
            self._active.clear()

    def set_mute(self, track_id: int, muted: bool) -> None:
        with self._mix_lock:
            chain = self._fx_chains.get(track_id)
            if chain:
                chain.muted = muted
        if muted:
            self.stop_track(track_id)

    def set_solo(self, track_id: int, soloed: bool) -> None:
        with self._mix_lock:
            chain = self._fx_chains.get(track_id)
            if chain:
                chain.soloed = soloed

    def set_volume(self, track_id: int, volume: float) -> None:
        with self._mix_lock:
            chain = self._fx_chains.get(track_id)
            if chain:
                chain.volume = volume

    def set_pan(self, track_id: int, pan: float) -> None:
        with self._mix_lock:
            chain = self._fx_chains.get(track_id)
            if chain:
                chain.pan = pan

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _any_soloed(self) -> bool:
        return any(c.soloed for c in self._fx_chains.values())

    def _load_and_play(
        self,
        track_id:          int,
        path:              str,
        duration_secs:     float,
        chain:             Optional[AudioFxChain],
        start_offset_secs: float = 0.0,
    ) -> None:
        try:
            audio_data, sr = self._load_audio(path)
            if audio_data is None:
                return

            if start_offset_secs > 0.0:
                skip = int(start_offset_secs * sr)
                audio_data = audio_data[skip:, :]

            if duration_secs > 0.0:
                remaining = max(0.0, duration_secs - start_offset_secs) if start_offset_secs > 0.0 else duration_secs
                audio_data = audio_data[:int(remaining * sr), :]

            if audio_data.shape[0] == 0:
                return

            # DIAGNOSTIC ── probe 5: offline render entry ─────────────────────
            logger.info(
                "[DIAG] _load_and_play() track=%d | path=%s | "
                "samples=%d sr=%d | chain=%s plugins=%s",
                track_id, path, audio_data.shape[0], sr,
                "present" if chain is not None else "NONE (pass-through)",
                [
                    f"{getattr(p, 'DISPLAY_NAME', '?')}(enabled={getattr(p, 'enabled', '?')})"
                    for p in (chain.plugins if chain else []) if p is not None
                ],
            )
            # ─────────────────────────────────────────────────────────────────

            if chain is not None:
                audio_data = chain.process(audio_data, sr)

            # Ensure stereo float32 C-contiguous array.
            if audio_data.ndim == 1:
                audio_data = np.column_stack([audio_data, audio_data])
            elif audio_data.shape[1] == 1:
                audio_data = np.repeat(audio_data, 2, axis=1)

            if self._master_bus is not None:
                try:
                    L = np.ascontiguousarray(audio_data[:, 0], dtype=np.float32)
                    R = np.ascontiguousarray(audio_data[:, 1], dtype=np.float32)
                    n = len(L)
                    self._master_bus.prepare(n, sr)
                    self._master_bus.reset()
                    self._master_bus.add_track(L, R)
                    self._master_bus.process()
                    audio_data = np.column_stack([
                        np.asarray(self._master_bus.get_L(), dtype=np.float32),
                        np.asarray(self._master_bus.get_R(), dtype=np.float32),
                    ])
                except Exception as exc:
                    logger.debug("MasterBus processing skipped: %s", exc)

            audio_data = np.ascontiguousarray(
                np.clip(audio_data, -1.0, 1.0), dtype=np.float32
            )

            with self._mix_lock:
                self._active[track_id] = (audio_data, 0)
                self._last_played[track_id] = (path, duration_secs)

        except Exception as exc:
            logger.warning(
                "AudioFilePlayer: playback error track=%d path=%s — %s",
                track_id, path, exc,
            )

    def _load_audio(self, path: str):
        """Load and resample to SAMPLE_RATE. Returns (float32 ndarray (samples,2), sr)."""
        try:
            from pedalboard.io import AudioFile
            with AudioFile(path).resampled_to(SAMPLE_RATE) as f:
                audio = f.read(f.frames)   # (channels, samples)
            return audio.T.astype(np.float32), SAMPLE_RATE
        except Exception as exc:
            logger.warning("AudioFilePlayer._load_audio failed for %s: %s", path, exc)
            return None, 0
