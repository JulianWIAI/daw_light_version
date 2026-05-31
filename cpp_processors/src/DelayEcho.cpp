#include "DelayEcho.h"
#include <algorithm>
#include <cstring>

// Local π constant — avoids relying on M_PI which is not standard in MSVC.
static constexpr float TWO_PI_F = 6.28318530718f;

// ─────────────────────────────────────────────────────────────────────────────
// Construction / preparation
// ─────────────────────────────────────────────────────────────────────────────

DelayEcho::DelayEcho(float sample_rate)
    : sample_rate_(sample_rate)
    , bpm_(120.0f)
    , division_(0)              // quarter note
    , delay_samples_(0.0f)
    , feedback_(0.40f)
    , wet_(0.50f), dry_(0.50f)
    , mode_(0)                  // STEREO
    , write_pos_(0)
    , hi_cut_hz_(6000.0f)
    , lo_cut_hz_(150.0f)
    , lfo_phase_(0.0f)
    , tape_rate_hz_(0.5f)
    , tape_depth_samps_(0.0f)   // set via set_tape_depth()
{
    buf_l_.fill(0.0f);
    buf_r_.fill(0.0f);
    prepare(sample_rate);
}

void DelayEcho::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    // Recompute derived values (delay time and filter coefficients).
    _update_delay_from_bpm();
    _update_filters();
    reset();
}

void DelayEcho::reset() noexcept {
    buf_l_.fill(0.0f);
    buf_r_.fill(0.0f);
    write_pos_ = 0;
    lfo_phase_ = 0.0f;
    hi_cut_.reset();
    lo_cut_.reset();
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void DelayEcho::set_bpm(float bpm) noexcept {
    bpm_ = bpm;
    _update_delay_from_bpm();
}

void DelayEcho::set_division(int div) noexcept {
    division_ = div;
    _update_delay_from_bpm();
}

void DelayEcho::set_delay_ms(float ms) noexcept {
    // Manual mode: only applied when bpm_ == 0.
    if (bpm_ <= 0.0f) {
        delay_samples_ = ms * sample_rate_ / 1000.0f;
        delay_samples_ = std::max(4.0f, std::min(delay_samples_,
                                                  float(DELAY_BUF_SIZE - 4)));
    }
}

void DelayEcho::set_feedback(float f) noexcept {
    // Hard-clamp below 1 to prevent infinite resonance.
    feedback_ = std::max(0.0f, std::min(f, 0.99f));
}

void DelayEcho::set_wet(float w) noexcept {
    wet_ = std::max(0.0f, std::min(w, 1.0f));
    dry_ = 1.0f - wet_;
}

void DelayEcho::set_hi_cut(float hz) noexcept {
    hi_cut_hz_ = hz;
    _update_filters();
}

void DelayEcho::set_lo_cut(float hz) noexcept {
    lo_cut_hz_ = hz;
    _update_filters();
}

void DelayEcho::set_mode(int mode) noexcept {
    mode_ = mode;
}

void DelayEcho::set_tape_rate(float hz) noexcept {
    tape_rate_hz_ = std::max(0.01f, hz);
}

void DelayEcho::set_tape_depth(float ms) noexcept {
    // Convert ms → samples and clamp to a musically useful range (max 30 ms).
    tape_depth_samps_ = std::min(ms, 30.0f) * sample_rate_ / 1000.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

void DelayEcho::_update_delay_from_bpm() noexcept {
    if (bpm_ <= 0.0f) return;
    const float beat_ms = 60000.0f / bpm_;
    float delay_ms;
    switch (division_) {
        case 0: delay_ms = beat_ms;          break;  // 1/4 note
        case 1: delay_ms = beat_ms * 0.75f;  break;  // dotted 1/8
        case 2: delay_ms = beat_ms * 0.50f;  break;  // 1/8 note
        default: delay_ms = beat_ms; break;
    }
    delay_samples_ = delay_ms * sample_rate_ / 1000.0f;
    // Clamp: must stay at least 4 samples inside the buffer (for Hermite context).
    delay_samples_ = std::max(4.0f, std::min(delay_samples_,
                                              float(DELAY_BUF_SIZE - 4)));
}

void DelayEcho::_update_filters() noexcept {
    // Butterworth low-pass for hi-cut (Q = 0.707 = maximally flat).
    hi_cut_.coeffs = biquad::make_lowpass(hi_cut_hz_, 0.707f, sample_rate_);
    // Butterworth high-pass for lo-cut.
    lo_cut_.coeffs = biquad::make_highpass(lo_cut_hz_, 0.707f, sample_rate_);
}

/**
 * Hermite cubic (Catmull-Rom) interpolation between 4 samples.
 *
 * The four points in the circular buffer are:
 *   y[-1] = sample written (d − 1) steps ago  (older context for curvature)
 *   y[ 0] = sample written  d      steps ago  ← interpolation anchor 0
 *   y[ 1] = sample written (d + 1) steps ago  ← interpolation anchor 1
 *   y[ 2] = sample written (d + 2) steps ago  (older context for curvature)
 *
 * frac = fractional part of delay_samps, 0 → returns y[0], 1 → returns y[1].
 */
float DelayEcho::_read_hermite(const std::array<float, DELAY_BUF_SIZE>& buf,
                                float delay_samps) const noexcept {
    const auto   ui   = static_cast<unsigned>(write_pos_);
    const auto   d    = static_cast<unsigned>(delay_samps);
    const float  frac = delay_samps - static_cast<float>(d);

    const float ym1 = buf[(ui - d + 1u) & DELAY_BUF_MASK];
    const float y0  = buf[(ui - d     ) & DELAY_BUF_MASK];
    const float y1  = buf[(ui - d - 1u) & DELAY_BUF_MASK];
    const float y2  = buf[(ui - d - 2u) & DELAY_BUF_MASK];

    // Catmull-Rom coefficients.
    const float c0 = y0;
    const float c1 = 0.5f * (y1  - ym1);
    const float c2 = ym1 - 2.5f * y0 + 2.0f * y1 - 0.5f * y2;
    const float c3 = 0.5f * (y2  - ym1) + 1.5f * (y0 - y1);
    return ((c3 * frac + c2) * frac + c1) * frac + c0;
}

float DelayEcho::_saturate(float x) noexcept {
    // Scaled tanh soft-clip for tape character: slight drive + normalization.
    return std::tanh(x * 1.4f) / 1.4f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Main processing loop
// ─────────────────────────────────────────────────────────────────────────────

void DelayEcho::process(float* left, float* right, int num_samples) noexcept {
    const bool is_tape     = (mode_ == static_cast<int>(DelayMode::TAPE));
    const bool is_pingpong = (mode_ == static_cast<int>(DelayMode::PINGPONG));

    for (int i = 0; i < num_samples; ++i) {
        // ── Compute instantaneous delay time ─────────────────────────────────
        float cur_delay = delay_samples_;
        if (is_tape) {
            // Sinusoidal pitch wobble: modulate the read position.
            const float lfo_val = std::sin(TWO_PI_F * lfo_phase_);
            cur_delay += tape_depth_samps_ * lfo_val;
            cur_delay  = std::max(4.0f, std::min(cur_delay, float(DELAY_BUF_SIZE - 4)));
            lfo_phase_ += tape_rate_hz_ / sample_rate_;
            if (lfo_phase_ >= 1.0f) lfo_phase_ -= 1.0f;
        }

        // ── Read delayed signal from buffer ───────────────────────────────────
        const float read_l = _read_hermite(buf_l_, cur_delay);
        const float read_r = _read_hermite(buf_r_, cur_delay);

        // ── Apply feedback filters (hi-cut then lo-cut) ───────────────────────
        float fb_l = lo_cut_.process_l(hi_cut_.process_l(read_l));
        float fb_r = lo_cut_.process_r(hi_cut_.process_r(read_r));

        // ── Tape saturation on the feedback path ──────────────────────────────
        if (is_tape) {
            fb_l = _saturate(fb_l);
            fb_r = _saturate(fb_r);
        }

        // Denormal prevention in the feedback network.
        fb_l += DENORMAL_GUARD;
        fb_r += DENORMAL_GUARD;

        // ── Write input + feedback into delay buffer ──────────────────────────
        if (is_pingpong) {
            // Ping-pong: feedback from R feeds L and vice versa.
            buf_l_[write_pos_] = left[i]  + feedback_ * fb_r;
            buf_r_[write_pos_] = right[i] + feedback_ * fb_l;
        } else {
            // Stereo and Tape: normal per-channel feedback.
            buf_l_[write_pos_] = left[i]  + feedback_ * fb_l;
            buf_r_[write_pos_] = right[i] + feedback_ * fb_r;
        }
        write_pos_ = (write_pos_ + 1) & DELAY_BUF_MASK;

        // ── Wet/dry output mix ────────────────────────────────────────────────
        left[i]  = dry_ * left[i]  + wet_ * read_l;
        right[i] = dry_ * right[i] + wet_ * read_r;
    }
}
