/**
 * MasterBus.h -- Real-time stereo master bus: summing, gain, audition modes,
 *                peak metering, and brickwall limiting.
 * =============================================================================
 * All active AudioFilePlayer track buffers are accumulated here via add_track(),
 * then process() applies master gain, routes through the active AuditionMode,
 * and measures per-channel peak levels for the GUI meter.
 *
 * Audition modes (set from the GUI thread via set_audition_mode()):
 *   BYPASS    -- Normal path: user-configured BrickwallLimiter.
 *   PREVIEW   -- Hardcoded -7  LUFS target (+7 dB pre-gain, -1.0 dBFS ceiling).
 *   STREAMING -- Hardcoded -14 LUFS target ( 0 dB pre-gain, -1.0 dBFS ceiling).
 *
 * Thread safety:
 *   audition_mode_ is a std::atomic<int> so set_audition_mode() is safe to
 *   call from the GUI thread while process() runs on the audio thread.
 *   No mutex is needed; the worst case is one block of latency before the
 *   new mode is heard.
 *
 * Typical call sequence per audio block:
 *   bus.reset()                   -- zero the sum buffers
 *   bus.add_track(L, R, n)        -- accumulate each active track
 *   bus.process()                 -- gain → audition routing → peak metering
 *   peak = bus.peak_L()           -- GUI polls this at ~20 Hz
 */

#pragma once

#include <atomic>
#include <vector>
#include "BrickwallLimiter.h"
#include "AuditionProcessor.h"   // also brings in AuditionMode enum

class MasterBus {
public:
    // Construct with sample rate (forwarded to all embedded processors).
    explicit MasterBus(float sample_rate = 44100.0f);

    // ── Lifecycle ──────────────────────────────────────────────────────────────

    // Allocate internal buffers and reconfigure all processors for n_frames.
    // Must be called before the first add_track() / process() call and whenever
    // the block size or sample rate changes.
    void prepare(int n_frames, float sample_rate);

    // Zero the sum buffers without reallocating. Call once at the start of
    // each audio block before any add_track() calls.
    void reset() noexcept;

    // ── Summing ────────────────────────────────────────────────────────────────

    // Accumulate one stereo track into the internal sum buffer.
    // n must not exceed the n_frames value passed to prepare(); excess samples
    // are silently ignored to prevent buffer overflows.
    void add_track(const float* L, const float* R, int n) noexcept;

    // ── Processing ────────────────────────────────────────────────────────────
    //
    // Full signal chain (called once per audio block):
    //   Stage 1 -- Master gain (scalar multiply, always active).
    //   Stage 2 -- Audition routing (based on audition_mode_):
    //     BYPASS    → user BrickwallLimiter (respects limiter_on_ flag).
    //     PREVIEW   → AuditionProcessor configured for -7  LUFS / -1 dBFS.
    //     STREAMING → AuditionProcessor configured for -14 LUFS / -1 dBFS.
    //   Stage 3 -- Peak measurement (fast-attack, ~200 ms decay hold).
    void process() noexcept;

    // ── Output ────────────────────────────────────────────────────────────────

    // Return a copy of the processed left-channel buffer.
    std::vector<float> get_L() const;
    // Return a copy of the processed right-channel buffer.
    std::vector<float> get_R() const;

    // ── Peak metering ──────────────────────────────────────────────────────────

    // Peak level on the left channel, updated by process().
    // Values 0.0–1.0 represent -∞ to 0 dBFS; values >1.0 = over-ceiling.
    float peak_L() const noexcept;
    // Peak level on the right channel.
    float peak_R() const noexcept;

    // ── Audition mode ─────────────────────────────────────────────────────────

    // Switch the audition mode instantly from any thread.
    // Accepts the integer value of an AuditionMode enum:
    //   0 = BYPASS, 1 = PREVIEW, 2 = STREAMING.
    // Thread-safe via std::atomic; the new mode takes effect within one block.
    void set_audition_mode(int mode) noexcept;
    int  get_audition_mode()   const noexcept;

    // ── User limiter parameters ────────────────────────────────────────────────
    // (These affect the BYPASS path only; audition paths have hardcoded limits.)

    // Master gain applied before the audition routing stage.
    // 0.0 = silence, 1.0 = unity, 2.0 ≈ +6 dB.
    void  set_gain(float gain) noexcept;
    float get_gain()     const noexcept;

    // User limiter true-peak ceiling in dBFS (BYPASS mode only).
    void  set_ceiling(float db) noexcept;
    float get_ceiling()  const noexcept;

    // Enable or disable the user brickwall limiter (BYPASS mode only).
    void set_limiter_enabled(bool enabled) noexcept;
    bool get_limiter_enabled()       const noexcept;

private:
    // Stereo sum / output buffers.
    std::vector<float> buf_L_;
    std::vector<float> buf_R_;

    int   n_frames_    = 0;
    float sample_rate_ = 44100.0f;
    float gain_        = 1.0f;
    float ceiling_db_  = -0.1f;
    bool  limiter_on_  = true;

    // Peak hold with exponential decay (~200 ms release time).
    float peak_L_     = 0.0f;
    float peak_R_     = 0.0f;
    float peak_decay_ = 0.9f;   // recalculated in prepare() from n_frames / sr

    // ── BYPASS path: user-controlled brickwall limiter ─────────────────────────
    BrickwallLimiter limiter_;

    // ── Active audition mode (written by GUI thread, read by audio thread) ─────
    // std::atomic<int> guarantees no torn reads even on non-x86 architectures.
    std::atomic<int> audition_mode_{ static_cast<int>(AuditionMode::BYPASS) };

    // ── PREVIEW path: -7 LUFS target (+7 dB pre-gain, -1.0 dBFS ceiling) ──────
    AuditionProcessor preview_proc_;

    // ── STREAMING path: -14 LUFS target (0 dB pre-gain, -1.0 dBFS ceiling) ────
    AuditionProcessor streaming_proc_;
};
