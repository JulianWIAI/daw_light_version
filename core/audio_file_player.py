"""
audio_file_player.py -- Multi-Track Audio File Playback Engine
===============================================================
Manages real-time playback of audio-file clips with per-track DSP effects.

Architecture:
    AudioFilePlayer owns one pygame.mixer.Channel per registered audio track.
    pygame.mixer handles the mixing of all channels to the audio device so that
    multiple audio tracks play simultaneously without interfering with each
    other or with FluidSynth's WASAPI/CoreAudio stream.

    When play_clip() is called (from the MidiLogic playback thread):
        1. A background thread loads the file via pedalboard.io.AudioFile.
        2. The AudioFxChain builds a pedalboard.Pedalboard and processes the
           raw PCM data offline (fast for files up to ~60 s).
        3. Volume and pan are applied via numpy operations.
        4. The result is converted to int16 and fed to pygame via
           pygame.sndarray.make_sound().
        5. The dedicated channel plays the sound -- simultaneous with all
           other channels.

    Solo logic: if any track is soloed, muted tracks AND non-soloed tracks
    are silenced at the pygame channel level without re-rendering the audio.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

from .audio_fx_chain import AudioFxChain

logger = logging.getLogger(__name__)

# Number of pygame mixer channels reserved for audio file tracks.
# Channels 0-3 are left free for other use; tracks occupy 4 onward.
_FIRST_TRACK_CHANNEL = 4
_MAX_TRACK_CHANNELS  = 64


class AudioFilePlayer:
    """
    Multi-track audio file playback engine built on pygame.mixer.

    One instance lives in MainWindow and receives play_clip() calls from the
    MidiLogic playback thread via the audio callback. Thread safety is
    maintained by a lock around the fx_chain and channel registries.
    """

    SAMPLE_RATE = 44100  # Target sample rate; files are resampled if needed.

    def __init__(self) -> None:
        self._fx_chains: Dict[int, AudioFxChain] = {}        # track_id -> chain
        self._channel_map: Dict[int, int] = {}               # track_id -> channel index
        # Stores (path, duration_secs) of the most recently played clip per track
        # so update_fx_chain() can re-render and restart a clip that is currently
        # playing when the user adjusts an effect in the AudioFxPanel.
        self._last_played: Dict[int, tuple] = {}             # track_id -> (path, secs)
        self._next_ch = _FIRST_TRACK_CHANNEL
        self._lock = threading.Lock()
        self._pygame_ok = False
        # MasterBus: set via set_master_bus() after the GUI creates the instance.
        # When set, every rendered clip passes through the C++ MasterBus for
        # master gain, brickwall limiting, and peak metering before reaching pygame.
        self._master_bus = None
        self._init_pygame()

    # -------------------------------------------------------------------------
    # Initialisation
    # -------------------------------------------------------------------------

    def _init_pygame(self) -> None:
        """Ensure pygame.mixer has enough channels for audio tracks."""
        try:
            import pygame
            # Extend channel count without re-initialising the mixer so
            # FluidSynth's audio thread is not disrupted.
            pygame.mixer.set_num_channels(
                max(pygame.mixer.get_num_channels(), _MAX_TRACK_CHANNELS)
            )
            self._pygame_ok = True
            logger.info(
                "AudioFilePlayer ready (%d mixer channels available)",
                pygame.mixer.get_num_channels(),
            )
        except Exception as exc:
            logger.warning("AudioFilePlayer: pygame.mixer unavailable -- %s", exc)

    # -------------------------------------------------------------------------
    # Track registration
    # -------------------------------------------------------------------------

    def register_track(self, track_id: int, fx_chain: Optional[AudioFxChain] = None) -> None:
        """
        Register an audio track so it gets a dedicated mixer channel.

        Calling this multiple times for the same track_id is safe -- it only
        creates a new channel entry on the first call.

        Args:
            track_id : AudioTrack.track_id.
            fx_chain : Initial effect chain; a neutral default is used if None.
        """
        with self._lock:
            if track_id not in self._channel_map:
                if self._next_ch >= _MAX_TRACK_CHANNELS:
                    logger.warning(
                        "AudioFilePlayer: channel limit (%d) reached, "
                        "track %d shares channel %d",
                        _MAX_TRACK_CHANNELS, track_id, _FIRST_TRACK_CHANNEL,
                    )
                    ch = _FIRST_TRACK_CHANNEL
                else:
                    ch = self._next_ch
                    self._next_ch += 1
                self._channel_map[track_id] = ch

            chain = fx_chain if fx_chain is not None else AudioFxChain(track_id=track_id)
            self._fx_chains[track_id] = chain

    def unregister_track(self, track_id: int) -> None:
        """Stop playback and remove a track from the player."""
        self.stop_track(track_id)
        with self._lock:
            self._fx_chains.pop(track_id, None)
            self._channel_map.pop(track_id, None)
            self._last_played.pop(track_id, None)

    def update_fx_chain(self, track_id: int, chain: AudioFxChain) -> None:
        """
        Replace the FX chain for a track and immediately re-render any clip
        that is currently playing on this track's mixer channel.

        Playback position is preserved: pygame.mixer.Channel.get_pos() returns
        the number of milliseconds the channel has been playing, which we
        convert to a sample offset so the re-rendered clip resumes from the
        same point in the file rather than jumping back to time zero.

        Args:
            track_id : Target AudioTrack.track_id.
            chain    : New AudioFxChain (must have matching track_id).
        """
        with self._lock:
            self._fx_chains[track_id] = chain
            ch_idx     = self._channel_map.get(track_id)
            last_entry = self._last_played.get(track_id)
            # Auto-register if the track was not previously registered.
            if track_id not in self._channel_map:
                self._channel_map[track_id] = self._next_ch
                self._next_ch = min(self._next_ch + 1, _MAX_TRACK_CHANNELS - 1)
                ch_idx = self._channel_map[track_id]

        # If the channel is currently playing, capture position BEFORE stopping
        # so the re-render can resume from the same point in the audio file.
        if self._pygame_ok and ch_idx is not None and last_entry is not None:
            try:
                import pygame
                channel = pygame.mixer.Channel(ch_idx)
                if channel.get_busy():
                    # get_pos() returns elapsed milliseconds since play() was called.
                    elapsed_ms = max(0, channel.get_pos())
                    start_offset_secs = elapsed_ms / 1000.0
                    channel.stop()
                    path, duration_secs = last_entry
                    # Re-render from the captured offset so the user hears the new
                    # FX settings without the clip restarting from the beginning.
                    self._play_clip_from_offset(
                        track_id, path, duration_secs, start_offset_secs
                    )
            except Exception:
                pass

    def get_fx_chain(self, track_id: int) -> Optional[AudioFxChain]:
        """Return the current FX chain for a track, or None if not registered."""
        return self._fx_chains.get(track_id)

    def set_master_bus(self, bus) -> None:
        """
        Attach a MasterBus instance (C++ or Python fallback).

        Once set, every rendered clip is routed through the master bus for
        gain scaling, brickwall limiting, and peak-level metering before the
        audio reaches pygame.  The MasterBus methods release the Python GIL,
        so the audio render thread is not blocked by the interpreter.
        """
        self._master_bus = bus

    # -------------------------------------------------------------------------
    # Playback control
    # -------------------------------------------------------------------------

    def play_clip(self, track_id: int, path: str, duration_secs: float) -> None:
        """
        Begin asynchronous playback of an audio clip with effects applied.

        This method returns immediately; loading and DSP happen in a daemon
        thread so the MidiLogic playback loop is not blocked.

        Args:
            track_id     : AudioTrack.track_id (used to look up FX chain and channel).
            path         : Absolute path to the audio file.
            duration_secs: Maximum play time in seconds. 0 = play full file.
        """
        self._play_clip_from_offset(track_id, path, duration_secs, 0.0)

    def _play_clip_from_offset(
        self,
        track_id:          int,
        path:              str,
        duration_secs:     float,
        start_offset_secs: float,
    ) -> None:
        """
        Internal helper: spawn a background render+play thread starting at
        *start_offset_secs* into the audio file.

        Called by play_clip() (offset=0) and by update_fx_chain() (offset =
        elapsed playback time captured just before stopping the channel).
        """
        if not self._pygame_ok:
            return

        # Capture chain and channel while holding the lock.
        with self._lock:
            chain       = self._fx_chains.get(track_id)
            channel_idx = self._channel_map.get(track_id)

        # Auto-register unknown tracks with a neutral chain.
        if channel_idx is None:
            self.register_track(track_id)
            with self._lock:
                chain       = self._fx_chains.get(track_id)
                channel_idx = self._channel_map.get(track_id)

        # Respect solo/mute without loading anything.
        if chain is not None and chain.muted:
            return
        if self._any_soloed() and not (chain and chain.soloed):
            return

        thread = threading.Thread(
            target=self._load_and_play,
            args=(track_id, path, duration_secs, chain, channel_idx,
                  start_offset_secs),
            daemon=True,
            name=f"AudioPlayer-{track_id}",
        )
        thread.start()

    def stop_track(self, track_id: int) -> None:
        """Stop the pygame channel assigned to track_id (instant, no fade)."""
        if not self._pygame_ok:
            return
        try:
            import pygame
            ch = self._channel_map.get(track_id)
            if ch is not None:
                pygame.mixer.Channel(ch).stop()
        except Exception as exc:
            logger.debug("AudioFilePlayer.stop_track(%d): %s", track_id, exc)

    def stop_all(self) -> None:
        """Stop every registered audio track immediately."""
        if not self._pygame_ok:
            return
        try:
            import pygame
            for ch in self._channel_map.values():
                pygame.mixer.Channel(ch).stop()
        except Exception as exc:
            logger.debug("AudioFilePlayer.stop_all: %s", exc)

    def set_mute(self, track_id: int, muted: bool) -> None:
        """
        Mute or unmute a track at the mixer-channel level.

        Adjusts the pygame channel volume instantly without reprocessing audio.
        """
        with self._lock:
            chain = self._fx_chains.get(track_id)
            if chain:
                chain.muted = muted
        self._apply_channel_volume(track_id)

    def set_solo(self, track_id: int, soloed: bool) -> None:
        """
        Solo or un-solo a track.

        When any track is soloed, all non-soloed channels are set to volume 0.
        """
        with self._lock:
            chain = self._fx_chains.get(track_id)
            if chain:
                chain.soloed = soloed
        self._reapply_all_volumes()

    def set_volume(self, track_id: int, volume: float) -> None:
        """Update volume (0.0-2.0) and apply to the pygame channel immediately."""
        with self._lock:
            chain = self._fx_chains.get(track_id)
            if chain:
                chain.volume = volume
        self._apply_channel_volume(track_id)

    def set_pan(self, track_id: int, pan: float) -> None:
        """
        Update stereo pan (-1.0 = full left, 0.0 = centre, 1.0 = full right).

        Pan is baked in during the offline render, so changes take effect
        on the next play_clip() call for this track.
        """
        with self._lock:
            chain = self._fx_chains.get(track_id)
            if chain:
                chain.pan = pan

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _any_soloed(self) -> bool:
        """Return True if at least one registered track has soloed=True."""
        return any(c.soloed for c in self._fx_chains.values())

    def _apply_channel_volume(self, track_id: int) -> None:
        """Set the pygame channel volume based on mute / solo / volume."""
        if not self._pygame_ok:
            return
        try:
            import pygame
            ch = self._channel_map.get(track_id)
            if ch is None:
                return
            chain = self._fx_chains.get(track_id)
            if chain is None:
                return
            vol = 0.0 if chain.muted else min(1.0, chain.volume)
            if self._any_soloed() and not chain.soloed:
                vol = 0.0
            pygame.mixer.Channel(ch).set_volume(vol)
        except Exception:
            pass

    def _reapply_all_volumes(self) -> None:
        """Reapply volume to every channel after a solo state change."""
        for tid in list(self._channel_map):
            self._apply_channel_volume(tid)

    def _load_and_play(
        self,
        track_id:          int,
        path:              str,
        duration_secs:     float,
        chain:             Optional[AudioFxChain],
        channel_idx:       int,
        start_offset_secs: float = 0.0,
    ) -> None:
        """
        Background thread: load audio, apply DSP, hand off to pygame.

        Args:
            start_offset_secs: Skip this many seconds at the start of the file.
                                Used by update_fx_chain() to resume from the
                                playback position that was active before the
                                FX change, so the clip does not jump to t=0.

        Errors are logged but never raised so the playback loop is unaffected.
        """
        try:
            import numpy as np
            import pygame

            # 1. Load the audio file (resampled to mixer rate via pedalboard).
            audio_data, sample_rate = self._load_audio(path)
            if audio_data is None:
                return  # Error already logged inside _load_audio.

            # 2. Skip to the resume offset (preserves position on FX change).
            if start_offset_secs > 0.0:
                skip_samples = int(start_offset_secs * sample_rate)
                audio_data   = audio_data[skip_samples:, :]

            # 3. Trim to requested duration.  When resuming mid-clip we trim
            #    to the REMAINING duration (original minus elapsed), so we do
            #    not play past the original clip boundary.
            effective_duration = duration_secs
            if start_offset_secs > 0.0 and duration_secs > 0.0:
                effective_duration = max(0.0, duration_secs - start_offset_secs)
            if effective_duration > 0.0:
                n_samples  = int(effective_duration * sample_rate)
                audio_data = audio_data[:n_samples, :]

            # Guard against rendering a zero-length buffer.
            if audio_data.shape[0] == 0:
                return

            # 4. Pass audio through the dynamic plugin chain (EQ, reverb, C++ processors, etc.).
            # chain.process() iterates chain.plugins in insertion order, skipping bypassed slots.
            if chain is not None:
                audio_data = chain.process(audio_data, sample_rate)

            # 5. Apply volume and pan.
            n_ch = audio_data.shape[1] if audio_data.ndim == 2 else 1
            if chain is not None:
                audio_data = chain.apply_gain_pan(audio_data, n_ch)

            # 6. Ensure stereo (pygame mixer is initialised as stereo).
            if audio_data.ndim == 1:
                audio_data = np.column_stack([audio_data, audio_data])
            elif audio_data.shape[1] == 1:
                audio_data = np.repeat(audio_data, 2, axis=1)

            # 6.5. Route through the C++ MasterBus: master gain + brickwall
            # limiting + peak metering.  All three MasterBus calls release the
            # GIL, so the audio thread does not block the GUI interpreter.
            # With pygame as the backend each clip is processed individually
            # (true multi-track summing would require a sounddevice callback).
            if self._master_bus is not None:
                try:
                    L = np.ascontiguousarray(audio_data[:, 0], dtype=np.float32)
                    R = np.ascontiguousarray(audio_data[:, 1], dtype=np.float32)
                    n = len(L)
                    self._master_bus.prepare(n, int(sample_rate))
                    self._master_bus.reset()
                    self._master_bus.add_track(L, R)   # GIL released in C++
                    self._master_bus.process()          # gain → limiter → peak
                    audio_data = np.column_stack([
                        np.asarray(self._master_bus.get_L(), dtype=np.float32),
                        np.asarray(self._master_bus.get_R(), dtype=np.float32),
                    ])
                except Exception as exc:
                    logger.debug("MasterBus processing skipped: %s", exc)

            # 7. Convert to int16 for pygame.
            audio_int16 = (np.clip(audio_data, -1.0, 1.0) * 32767).astype(np.int16)
            audio_int16 = np.ascontiguousarray(audio_int16)

            # 8. Play on the dedicated channel and record what was played.
            #    We always store the ORIGINAL (path, duration_secs) so that
            #    a subsequent FX change can re-compute the new offset correctly.
            sound   = pygame.sndarray.make_sound(audio_int16)
            channel = pygame.mixer.Channel(channel_idx)
            channel.play(sound)
            with self._lock:
                self._last_played[track_id] = (path, duration_secs)

        except Exception as exc:
            logger.warning(
                "AudioFilePlayer: playback error track=%d path=%s -- %s",
                track_id, path, exc,
            )

    def _load_audio(self, path: str):
        """
        Load an audio file and resample it to SAMPLE_RATE.

        Returns (float32 ndarray of shape (samples, channels), sample_rate)
        or (None, 0) on failure. Uses pedalboard.io.AudioFile which supports
        WAV, MP3, OGG, FLAC, AIFF, M4A without external libraries.
        """
        try:
            from pedalboard.io import AudioFile
            import numpy as np

            with AudioFile(path).resampled_to(self.SAMPLE_RATE) as f:
                audio = f.read(f.frames)  # shape: (channels, samples), float32

            # Transpose to (samples, channels) for all subsequent operations.
            return audio.T.astype("float32"), self.SAMPLE_RATE

        except Exception as exc:
            logger.warning("AudioFilePlayer._load_audio failed for %s: %s", path, exc)
            return None, 0
