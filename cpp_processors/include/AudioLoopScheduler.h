#pragma once
/*
 * AudioLoopScheduler.h -- Precision audio-loop boundary scheduler
 * ================================================================
 * Uses std::chrono::steady_clock + sleep_until for sub-millisecond loop
 * boundary precision.  This eliminates the ~15 ms drift of Python
 * time.sleep() on Windows which caused audio clips to restart before the
 * previous iteration had finished playing.
 *
 * Architecture
 * ────────────
 * A single daemon worker thread runs the scheduling loop.  For each loop
 * iteration it:
 *   1. Fires each scheduled clip at the exact wall-clock time derived from
 *      the iteration start TimePoint + per-clip beat offset.
 *   2. Sleeps with 5 ms cancellation ticks (stop() is always responsive).
 *   3. At the loop boundary: calls stop_fn_() to halt all playing audio,
 *      then anchors the next iteration to the SAME TimePoint so no drift
 *      can accumulate across iterations.
 *
 * In non-loop mode the scheduler plays clips once, waits until loop_end,
 * then stops without calling stop_fn_().
 *
 * Thread safety
 * ─────────────
 * Config setters and clip-list mutators must be called BEFORE play().
 * current_beat() is lock-free (atomic reads) — safe from any thread.
 * Callbacks are invoked from the worker thread; pybind11 wrappers must
 * acquire the GIL before calling into the Python interpreter.
 */

#include <atomic>
#include <chrono>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// Clip-start callback: (track_id, path, remaining_secs, start_offset_secs)
using ClipStartFn = std::function<void(int, const std::string&, double, double)>;

// Stop-all callback: invoked at every loop boundary to halt playing audio.
using StopAllFn = std::function<void()>;


struct ScheduledClip {
    int         track_id;
    std::string path;
    double      start_beat;     // absolute beat on the project timeline
    double      duration_secs;  // total file duration (0 = unknown / unlimited)
};


class AudioLoopScheduler {
public:
    AudioLoopScheduler();
    ~AudioLoopScheduler();

    // ── Configuration ─────────────────────────────────────────────────────────
    // Call all setters before play().  Thread-safe via internal mutex.

    void set_bpm(double bpm);
    void set_loop(bool enabled, double start_beat, double end_beat);
    void set_clip_fn(ClipStartFn fn);
    void set_stop_fn(StopAllFn fn);

    // ── Clip list ─────────────────────────────────────────────────────────────
    // Rebuild before each play() call.  Not safe to call while playing.

    void add_clip(int track_id, const std::string& path,
                  double start_beat, double duration_secs);
    void clear_clips();

    // ── Transport ─────────────────────────────────────────────────────────────

    void play(double from_beat = 0.0);
    void stop();

    bool   is_playing()    const;
    double current_beat()  const;  // lock-free interpolated position

private:
    using Clock     = std::chrono::steady_clock;
    using TimePoint = Clock::time_point;

    void _run();

    // Fire clips in [iter_start, iter_end) from iter_tp as wall-clock anchor.
    void _fire_clips_for_iter(double iter_start, double iter_end,
                               TimePoint iter_tp, double secs_per_beat);

    // Precision sleep with 5 ms cancellation ticks.
    void _sleep_until(TimePoint tp);

    // ── Config (protected by mutex_) ──────────────────────────────────────────
    mutable std::mutex mutex_;
    double      bpm_          = 120.0;
    bool        loop_enabled_ = false;
    double      loop_start_   = 0.0;
    double      loop_end_     = 8.0;
    ClipStartFn clip_fn_;
    StopAllFn   stop_fn_;

    // ── Clip list (set before play, not modified during playback) ─────────────
    std::vector<ScheduledClip> clips_;

    // ── Playback state ────────────────────────────────────────────────────────
    std::atomic<bool>   playing_      {false};

    // Beat anchor updated at the start of every loop iteration.
    // anchor_beat_ + elapsed * bpm_ / 60 = current beat.
    std::atomic<double> anchor_beat_  {0.0};
    std::atomic<double> anchor_wall_  {0.0};  // seconds from init_tp_

    double    play_bpm_  = 120.0;  // BPM captured at play(); constant during run
    TimePoint init_tp_;            // set once at construction

    std::thread worker_;
};
