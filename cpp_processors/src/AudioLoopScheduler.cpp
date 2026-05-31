#include "AudioLoopScheduler.h"
#include <algorithm>
#include <cmath>

// ─────────────────────────────────────────────────────────────────────────────
// Construction / destruction
// ─────────────────────────────────────────────────────────────────────────────

AudioLoopScheduler::AudioLoopScheduler()
    : init_tp_(Clock::now())
{
}

AudioLoopScheduler::~AudioLoopScheduler() {
    stop();
}

// ─────────────────────────────────────────────────────────────────────────────
// Configuration
// ─────────────────────────────────────────────────────────────────────────────

void AudioLoopScheduler::set_bpm(double bpm) {
    std::lock_guard<std::mutex> lock(mutex_);
    bpm_ = std::max(1.0, bpm);
}

void AudioLoopScheduler::set_loop(bool enabled, double start_beat, double end_beat) {
    std::lock_guard<std::mutex> lock(mutex_);
    loop_enabled_ = enabled;
    loop_start_   = std::max(0.0, start_beat);
    loop_end_     = std::max(loop_start_ + 0.01, end_beat);
}

void AudioLoopScheduler::set_clip_fn(ClipStartFn fn) {
    std::lock_guard<std::mutex> lock(mutex_);
    clip_fn_ = std::move(fn);
}

void AudioLoopScheduler::set_stop_fn(StopAllFn fn) {
    std::lock_guard<std::mutex> lock(mutex_);
    stop_fn_ = std::move(fn);
}

// ─────────────────────────────────────────────────────────────────────────────
// Clip list
// ─────────────────────────────────────────────────────────────────────────────

void AudioLoopScheduler::add_clip(int track_id, const std::string& path,
                                   double start_beat, double duration_secs) {
    clips_.push_back({track_id, path, start_beat, duration_secs});
}

void AudioLoopScheduler::clear_clips() {
    clips_.clear();
}

// ─────────────────────────────────────────────────────────────────────────────
// Transport
// ─────────────────────────────────────────────────────────────────────────────

void AudioLoopScheduler::play(double from_beat) {
    stop();  // join any previous worker before starting a new one

    double bpm;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        bpm = bpm_;
    }
    play_bpm_ = bpm;

    // Anchor the beat position to the current wall-clock time.
    double now_wall = std::chrono::duration<double>(Clock::now() - init_tp_).count();
    anchor_beat_.store(from_beat);
    anchor_wall_.store(now_wall);

    playing_.store(true);
    worker_ = std::thread(&AudioLoopScheduler::_run, this);
}

void AudioLoopScheduler::stop() {
    playing_.store(false);
    if (worker_.joinable()) {
        worker_.join();
    }
}

bool AudioLoopScheduler::is_playing() const {
    return playing_.load();
}

double AudioLoopScheduler::current_beat() const {
    if (!playing_.load()) return anchor_beat_.load();
    double a_beat = anchor_beat_.load();
    double a_wall = anchor_wall_.load();
    double now_wall = std::chrono::duration<double>(Clock::now() - init_tp_).count();
    return a_beat + (now_wall - a_wall) * play_bpm_ / 60.0;
}

// ─────────────────────────────────────────────────────────────────────────────
// Internal helpers
// ─────────────────────────────────────────────────────────────────────────────

void AudioLoopScheduler::_sleep_until(TimePoint tp) {
    // Poll stop flag every 5 ms so stop() never has to wait more than 5 ms.
    static const auto TICK = std::chrono::milliseconds(5);
    while (playing_.load()) {
        auto now = Clock::now();
        if (now >= tp) return;
        auto remaining = tp - now;
        if (remaining <= TICK) {
            std::this_thread::sleep_until(tp);
            return;
        }
        std::this_thread::sleep_for(TICK);
    }
}

void AudioLoopScheduler::_fire_clips_for_iter(double iter_start, double iter_end,
                                               TimePoint iter_tp, double secs_per_beat) {
    // Retrieve the callback under lock so it is safe to call unlocked.
    ClipStartFn clip_fn;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        clip_fn = clip_fn_;
    }
    if (!clip_fn) return;

    // Sort clips by start beat so we sleep in chronological order.
    std::vector<const ScheduledClip*> ordered;
    ordered.reserve(clips_.size());
    for (const auto& c : clips_) ordered.push_back(&c);
    std::sort(ordered.begin(), ordered.end(),
              [](const ScheduledClip* a, const ScheduledClip* b) {
                  return a->start_beat < b->start_beat;
              });

    for (const ScheduledClip* c : ordered) {
        if (!playing_.load()) return;

        // End beat of this clip (beats).
        double clip_end = (c->duration_secs > 0.0)
            ? (c->start_beat + c->duration_secs / secs_per_beat)
            : iter_end;

        // Skip clips fully outside the iteration window.
        if (clip_end  <= iter_start) continue;
        if (c->start_beat >= iter_end)   continue;

        double delay_secs;
        double offset_secs;
        double remaining_secs;

        if (c->start_beat >= iter_start) {
            // Clip starts inside this iteration.
            delay_secs     = (c->start_beat - iter_start) * secs_per_beat;
            offset_secs    = 0.0;
            remaining_secs = c->duration_secs;
        } else {
            // Clip started before iter_start (seek-into scenario).
            delay_secs     = 0.0;
            offset_secs    = (iter_start - c->start_beat) * secs_per_beat;
            remaining_secs = (c->duration_secs > 0.0)
                             ? (c->duration_secs - offset_secs)
                             : 0.0;
            if (remaining_secs <= 0.0) continue;
        }

        // Sleep until this clip's fire time, then invoke the callback.
        auto fire_tp = iter_tp + std::chrono::duration_cast<Clock::duration>(
            std::chrono::duration<double>(delay_secs));
        _sleep_until(fire_tp);

        if (!playing_.load()) return;
        clip_fn(c->track_id, c->path, remaining_secs, offset_secs);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Scheduler worker
// ─────────────────────────────────────────────────────────────────────────────

void AudioLoopScheduler::_run() {
    // Snapshot config once at the start of this playback session.
    double bpm, loop_start, loop_end;
    bool   loop_en;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        bpm        = bpm_;
        loop_en    = loop_enabled_;
        loop_start = loop_start_;
        loop_end   = loop_end_;
    }

    double secs_per_beat = 60.0 / bpm;
    double iter_start    = loop_en ? loop_start : anchor_beat_.load();
    double iter_end      = loop_end;
    TimePoint iter_tp    = Clock::now();

    while (playing_.load()) {
        // Update the beat/wall anchors so current_beat() is accurate.
        double now_wall = std::chrono::duration<double>(iter_tp - init_tp_).count();
        anchor_beat_.store(iter_start);
        anchor_wall_.store(now_wall);

        // Fire all clips scheduled within this iteration.
        _fire_clips_for_iter(iter_start, iter_end, iter_tp, secs_per_beat);

        if (!playing_.load()) break;

        // Sleep until the exact iteration boundary (no drift — absolute TimePoint).
        double loop_dur_secs = (iter_end - iter_start) * secs_per_beat;
        TimePoint iter_end_tp = iter_tp
            + std::chrono::duration_cast<Clock::duration>(
                std::chrono::duration<double>(loop_dur_secs));
        _sleep_until(iter_end_tp);

        if (!playing_.load()) break;

        if (!loop_en) {
            // Non-loop: finished — update anchor to end and exit.
            anchor_beat_.store(iter_end);
            playing_.store(false);
            break;
        }

        // ── Loop boundary ─────────────────────────────────────────────────────
        // Stop all currently playing audio EXACTLY at the boundary before
        // firing any clips for the new iteration.  This prevents the old
        // iteration's audio from overlapping the start of the next one.
        StopAllFn stop_fn;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stop_fn = stop_fn_;
        }
        if (stop_fn) stop_fn();

        // Anchor next iteration to the exact boundary TimePoint — no drift.
        iter_tp    = iter_end_tp;
        iter_start = loop_start;
        iter_end   = loop_end;
    }

    playing_.store(false);
}
