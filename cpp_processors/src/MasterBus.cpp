/**
 * MasterBus.cpp -- Real-time stereo master bus with audition mode routing.
 * =========================================================================
 * See MasterBus.h for the full signal chain description and thread-safety notes.
 */

#include "MasterBus.h"
#include <algorithm>
#include <cmath>

// ── Construction ──────────────────────────────────────────────────────────────

MasterBus::MasterBus(float sample_rate)
    : sample_rate_(sample_rate)
    , limiter_(sample_rate)
    , preview_proc_(sample_rate)
    , streaming_proc_(sample_rate)
{
    // Apply the default ceiling to the user limiter.
    limiter_.set_ceiling(ceiling_db_);

    // ── Configure the PREVIEW audition path ───────────────────────────────────
    // Target: -7 LUFS.  Assuming the mix sits near -14 LUFS integrated at unity
    // gain, a +7 dB pre-gain shift simulates the louder -7 LUFS master level.
    // Ceiling: -1.0 dBFS true peak (within streaming safe zone despite louder level).
    // Fast release (50 ms) gives the pumping character typical of a commercial master.
    preview_proc_.configure(
        7.0f,   // pre_gain_db  : +7 dB → simulate -7 LUFS loudness
       -1.0f,   // ceiling_db   : -1.0 dBFS true peak
        0.5f,   // attack_ms    : 0.5 ms (fast attack, minimal transient loss)
       50.0f    // release_ms   : 50 ms  (characteristic commercial-master pumping)
    );

    // ── Configure the STREAMING audition path ─────────────────────────────────
    // Target: -14 LUFS / -1.0 dBFS true peak (Spotify / Apple Music standard).
    // No pre-gain: the mix at unity is assumed to represent the -14 LUFS reference.
    // Longer release (150 ms) sounds more transparent for streaming normalization.
    streaming_proc_.configure(
        0.0f,   // pre_gain_db  : 0 dB — reference level unchanged
       -1.0f,   // ceiling_db   : -1.0 dBFS true peak (LUFS / TP spec)
        0.5f,   // attack_ms    : 0.5 ms
      150.0f    // release_ms   : 150 ms — transparent streaming limiter
    );
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────

void MasterBus::prepare(int n_frames, float sample_rate) {
    n_frames_    = n_frames;
    sample_rate_ = sample_rate;

    // Allocate and zero the stereo sum buffers.
    buf_L_.assign(n_frames, 0.0f);
    buf_R_.assign(n_frames, 0.0f);

    // Reconfigure the user limiter.
    limiter_.prepare(sample_rate);
    limiter_.set_ceiling(ceiling_db_);

    // Reconfigure both audition processors for the new sample rate.
    // Their gain / ceiling / timing settings are preserved from construction.
    preview_proc_.prepare(sample_rate);
    streaming_proc_.prepare(sample_rate);

    // Calculate the per-block peak-decay coefficient.
    // coeff = exp(-block_duration / release_time_secs)
    float block_dur = static_cast<float>(n_frames) / sample_rate;
    peak_decay_     = std::exp(-block_dur / 0.2f);

    peak_L_ = 0.0f;
    peak_R_ = 0.0f;
}

void MasterBus::reset() noexcept {
    std::fill(buf_L_.begin(), buf_L_.end(), 0.0f);
    std::fill(buf_R_.begin(), buf_R_.end(), 0.0f);
}

// ── Summing ───────────────────────────────────────────────────────────────────

void MasterBus::add_track(const float* L, const float* R, int n) noexcept {
    int count = std::min(n, n_frames_);
    for (int i = 0; i < count; ++i) {
        buf_L_[i] += L[i];
        buf_R_[i] += R[i];
    }
}

// ── Processing ────────────────────────────────────────────────────────────────

void MasterBus::process() noexcept {
    if (n_frames_ == 0) return;

    // Stage 1: Apply master gain to the summed buffer.
    for (int i = 0; i < n_frames_; ++i) {
        buf_L_[i] *= gain_;
        buf_R_[i] *= gain_;
    }

    // Stage 2: Audition routing — choose the processing path based on the
    // active mode.  The atomic load uses relaxed ordering: the mode change
    // may be visible one block late, which is inaudible at any sample rate.
    const int mode = audition_mode_.load(std::memory_order_relaxed);

    if (mode == static_cast<int>(AuditionMode::PREVIEW)) {
        // ── PREVIEW: -7 LUFS simulation ───────────────────────────────────────
        // Intercepts the normal FX chain and routes through the dedicated
        // preview processor (+7 dB pre-gain + BrickwallLimiter at -1 dBFS).
        preview_proc_.process(buf_L_.data(), buf_R_.data(), n_frames_);

    } else if (mode == static_cast<int>(AuditionMode::STREAMING)) {
        // ── STREAMING: -14 LUFS / -1 dBFS TP simulation ─────────────────────
        // Intercepts the normal FX chain and routes through the streaming
        // processor (0 dB pre-gain + BrickwallLimiter at -1 dBFS).
        streaming_proc_.process(buf_L_.data(), buf_R_.data(), n_frames_);

    } else {
        // ── BYPASS: user-configured FX chain ─────────────────────────────────
        // Normal processing path: apply the user-controlled BrickwallLimiter
        // only when it has been enabled (limiter_on_ flag).
        if (limiter_on_) {
            limiter_.process(buf_L_.data(), buf_R_.data(), n_frames_);
        }
    }

    // Stage 3: Measure the block peak (max absolute value on each channel).
    // This runs after all modes so the meter always reflects the final output.
    float block_peak_L = 0.0f;
    float block_peak_R = 0.0f;
    for (int i = 0; i < n_frames_; ++i) {
        float aL = std::abs(buf_L_[i]);
        float aR = std::abs(buf_R_[i]);
        if (aL > block_peak_L) block_peak_L = aL;
        if (aR > block_peak_R) block_peak_R = aR;
    }

    // Fast attack: replace the hold value if the current block is louder.
    // Slow decay: multiply by the per-block coefficient otherwise.
    peak_L_ = std::max(block_peak_L, peak_L_ * peak_decay_);
    peak_R_ = std::max(block_peak_R, peak_R_ * peak_decay_);
}

// ── Output ────────────────────────────────────────────────────────────────────

std::vector<float> MasterBus::get_L() const { return buf_L_; }
std::vector<float> MasterBus::get_R() const { return buf_R_; }

// ── Peak metering ─────────────────────────────────────────────────────────────

float MasterBus::peak_L() const noexcept { return peak_L_; }
float MasterBus::peak_R() const noexcept { return peak_R_; }

// ── Audition mode ─────────────────────────────────────────────────────────────

void MasterBus::set_audition_mode(int mode) noexcept {
    // Atomically write the new mode; the audio thread picks it up on the next
    // process() call with no lock and no possibility of a torn write.
    audition_mode_.store(mode, std::memory_order_relaxed);
}

int MasterBus::get_audition_mode() const noexcept {
    return audition_mode_.load(std::memory_order_relaxed);
}

// ── User limiter parameters ───────────────────────────────────────────────────

void  MasterBus::set_gain(float gain) noexcept { gain_ = gain; }
float MasterBus::get_gain()    const noexcept  { return gain_; }

void MasterBus::set_ceiling(float db) noexcept {
    ceiling_db_ = db;
    limiter_.set_ceiling(db);
}
float MasterBus::get_ceiling() const noexcept { return ceiling_db_; }

void MasterBus::set_limiter_enabled(bool enabled) noexcept { limiter_on_ = enabled; }
bool MasterBus::get_limiter_enabled()       const noexcept { return limiter_on_; }
