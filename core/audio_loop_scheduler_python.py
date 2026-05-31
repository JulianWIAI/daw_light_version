"""
audio_loop_scheduler_python.py -- Python fallback for AudioLoopScheduler
=========================================================================
Provides the same interface as the C++ AudioLoopScheduler (see
cpp_processors/include/AudioLoopScheduler.h) for environments where the
C++ extension cannot be loaded.

The C++ version uses std::chrono::steady_clock + sleep_until for
sub-millisecond precision.  This Python version uses time.perf_counter()
and time.sleep().  On Windows, sleep resolution is ~15 ms, so there can
be a small phase offset at loop boundaries — but each iteration is still
anchored to the PREVIOUS iteration's end time, so no drift accumulates
across multiple loops.

Factory
───────
    get_audio_loop_scheduler() -> AudioLoopSchedulerPython | C++ wrapper
    Returns the best available implementation.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

class _ScheduledClip:
    __slots__ = ("track_id", "path", "start_beat", "duration_secs")

    def __init__(self, track_id: int, path: str,
                 start_beat: float, duration_secs: float) -> None:
        self.track_id     = track_id
        self.path         = path
        self.start_beat   = start_beat
        self.duration_secs = duration_secs


# ─────────────────────────────────────────────────────────────────────────────
# Python fallback implementation
# ─────────────────────────────────────────────────────────────────────────────

class AudioLoopSchedulerPython:
    """
    Python implementation of the AudioLoopScheduler timing logic.

    Fires audio clips at wall-clock-anchored times and anchors every new
    loop iteration to the exact previous iteration end time, preventing
    inter-iteration drift.
    """

    def __init__(self) -> None:
        self._lock         = threading.Lock()
        self._bpm:   float = 120.0
        self._loop_enabled = False
        self._loop_start:  float = 0.0
        self._loop_end:    float = 8.0
        self._clip_fn: Optional[Callable] = None
        self._stop_fn: Optional[Callable] = None

        self._clips: List[_ScheduledClip] = []

        # Playback state — _stop_evt is cleared while playing.
        self._stop_evt    = threading.Event()
        self._stop_evt.set()   # not playing initially

        self._anchor_beat: float = 0.0
        self._anchor_wall: float = 0.0   # time.perf_counter() at anchor
        self._play_bpm:    float = 120.0

        self._worker: Optional[threading.Thread] = None

    # ── Configuration ─────────────────────────────────────────────────────────

    def set_bpm(self, bpm: float) -> None:
        with self._lock:
            self._bpm = max(1.0, bpm)

    def set_loop(self, enabled: bool, start_beat: float, end_beat: float) -> None:
        with self._lock:
            self._loop_enabled = enabled
            self._loop_start   = max(0.0, start_beat)
            self._loop_end     = max(self._loop_start + 0.01, end_beat)

    def set_clip_fn(self, fn: Callable) -> None:
        with self._lock:
            self._clip_fn = fn

    def set_stop_fn(self, fn: Callable) -> None:
        with self._lock:
            self._stop_fn = fn

    # ── Clip list ─────────────────────────────────────────────────────────────

    def add_clip(self, track_id: int, path: str,
                 start_beat: float, duration_secs: float) -> None:
        self._clips.append(_ScheduledClip(track_id, path, start_beat, duration_secs))

    def clear_clips(self) -> None:
        self._clips.clear()

    # ── Transport ─────────────────────────────────────────────────────────────

    def play(self, from_beat: float = 0.0) -> None:
        self.stop()
        with self._lock:
            bpm = self._bpm
        self._play_bpm    = bpm
        self._anchor_beat = from_beat
        self._anchor_wall = time.perf_counter()
        self._stop_evt.clear()   # mark as playing
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="AudioLoopScheduler")
        self._worker.start()

    def stop(self) -> None:
        self._stop_evt.set()   # signal worker to stop
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1.0)
        self._worker = None

    def is_playing(self) -> bool:
        return not self._stop_evt.is_set()

    def current_beat(self) -> float:
        if not self.is_playing():
            return self._anchor_beat
        elapsed = time.perf_counter() - self._anchor_wall
        return self._anchor_beat + elapsed * self._play_bpm / 60.0

    # ── Internal ──────────────────────────────────────────────────────────────

    def _is_running(self) -> bool:
        return not self._stop_evt.is_set()

    def _sleep_until(self, target: float) -> None:
        # Sleep in 5 ms ticks so stop() never waits more than 5 ms.
        while self._is_running():
            remaining = target - time.perf_counter()
            if remaining <= 0.0:
                return
            time.sleep(min(0.005, remaining))

    def _fire_clips_for_iter(
        self,
        iter_start:    float,
        iter_end:      float,
        iter_wall:     float,
        secs_per_beat: float,
    ) -> None:
        with self._lock:
            clip_fn = self._clip_fn
        if not clip_fn:
            return

        # Process clips in ascending start-beat order.
        ordered = sorted(self._clips, key=lambda c: c.start_beat)

        for c in ordered:
            if not self._is_running():
                return

            clip_end = (
                c.start_beat + c.duration_secs / secs_per_beat
                if c.duration_secs > 0.0
                else iter_end
            )
            if clip_end  <= iter_start: continue
            if c.start_beat >= iter_end:   continue

            if c.start_beat >= iter_start:
                delay_secs     = (c.start_beat - iter_start) * secs_per_beat
                offset_secs    = 0.0
                remaining_secs = c.duration_secs
            else:
                delay_secs     = 0.0
                offset_secs    = (iter_start - c.start_beat) * secs_per_beat
                remaining_secs = (
                    c.duration_secs - offset_secs
                    if c.duration_secs > 0.0
                    else 0.0
                )
                if remaining_secs <= 0.0:
                    continue

            self._sleep_until(iter_wall + delay_secs)
            if not self._is_running():
                return
            clip_fn(c.track_id, c.path, remaining_secs, offset_secs)

    def _run(self) -> None:
        with self._lock:
            bpm        = self._bpm
            loop_en    = self._loop_enabled
            loop_start = self._loop_start
            loop_end   = self._loop_end

        secs_per_beat = 60.0 / bpm
        iter_start    = loop_start if loop_en else self._anchor_beat
        iter_end      = loop_end
        iter_wall     = time.perf_counter()

        while self._is_running():
            # Update anchor so current_beat() advances correctly.
            self._anchor_beat = iter_start
            self._anchor_wall = iter_wall

            self._fire_clips_for_iter(iter_start, iter_end, iter_wall, secs_per_beat)

            if not self._is_running():
                break

            # Sleep until the exact iteration boundary.
            loop_dur    = (iter_end - iter_start) * secs_per_beat
            end_wall    = iter_wall + loop_dur
            self._sleep_until(end_wall)

            if not self._is_running():
                break

            if not loop_en:
                # Non-loop: update anchor to end position and finish.
                self._anchor_beat = iter_end
                self._stop_evt.set()
                break

            # ── Loop boundary ─────────────────────────────────────────────────
            # Stop all audio BEFORE starting the next iteration so the old
            # clips don't overlap the new ones.
            with self._lock:
                stop_fn = self._stop_fn
            if stop_fn:
                stop_fn()

            # Anchor next iteration to the precise boundary (no drift).
            iter_wall  = end_wall
            iter_start = loop_start
            iter_end   = loop_end

        self._stop_evt.set()


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def get_audio_loop_scheduler() -> AudioLoopSchedulerPython:
    """
    Return the best available AudioLoopScheduler implementation.

    Tries to instantiate the C++ version first.  Falls back to the Python
    implementation when the C++ extension is unavailable.
    """
    try:
        import daw_processors as dp  # type: ignore[import]
        if hasattr(dp, "AudioLoopScheduler"):
            return dp.AudioLoopScheduler()
    except Exception:
        pass
    return AudioLoopSchedulerPython()
