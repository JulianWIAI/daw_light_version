#include "Phaser.h"
#include <algorithm>
#include <cmath>
#include <cstring>

static constexpr float TWO_PI_F = 6.28318530718f;
static constexpr float PI_F     = 3.14159265359f;

// ─────────────────────────────────────────────────────────────────────────────
// Construction / preparation
// ─────────────────────────────────────────────────────────────────────────────

Phaser::Phaser(float sample_rate)
    : sample_rate_(sample_rate)
    , num_stages_(4)
    , rate_hz_(0.5f)
    , depth_(0.8f)
    , min_freq_(200.0f)
    , max_freq_(2000.0f)
    , feedback_(0.70f)
    , wet_(0.5f), dry_(0.5f)
    , stereo_offset_(0.25f)   // 0.25 cycles = 90° → natural stereo imaging
    , lfo_phase_l_(0.0f)
    , lfo_phase_r_(0.25f)     // initialised to stereo_offset_
    , fb_l_(0.0f), fb_r_(0.0f)
{
    std::memset(ap_x_prev_, 0, sizeof(ap_x_prev_));
    std::memset(ap_y_prev_, 0, sizeof(ap_y_prev_));
    prepare(sample_rate);
}

void Phaser::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    reset();
}

void Phaser::reset() noexcept {
    std::memset(ap_x_prev_, 0, sizeof(ap_x_prev_));
    std::memset(ap_y_prev_, 0, sizeof(ap_y_prev_));
    lfo_phase_l_ = 0.0f;
    lfo_phase_r_ = stereo_offset_;
    fb_l_ = fb_r_ = 0.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void Phaser::set_stages(int n) noexcept {
    // Only accept musically meaningful stage counts.
    if (n == 2 || n == 4 || n == 6 || n == 8 || n == 12)
        num_stages_ = n;
}

void Phaser::set_rate(float hz) noexcept {
    rate_hz_ = std::max(0.001f, hz);
}

void Phaser::set_depth(float d) noexcept {
    depth_ = std::max(0.0f, std::min(d, 1.0f));
}

void Phaser::set_min_freq(float hz) noexcept {
    min_freq_ = std::max(10.0f, hz);
}

void Phaser::set_max_freq(float hz) noexcept {
    max_freq_ = std::min(hz, sample_rate_ * 0.49f);
}

void Phaser::set_feedback(float f) noexcept {
    feedback_ = std::max(0.0f, std::min(f, 0.98f));
}

void Phaser::set_wet(float w) noexcept {
    wet_ = std::max(0.0f, std::min(w, 1.0f));
    dry_ = 1.0f - wet_;
}

void Phaser::set_stereo_offset(float f) noexcept {
    stereo_offset_ = std::max(0.0f, std::min(f, 1.0f));
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

/**
 * First-order all-pass coefficient for a given pole frequency:
 *   c = (tan(π·fc/sr) − 1) / (tan(π·fc/sr) + 1)
 *
 * The frequency is clamped to (0, sr/2) to keep the tan() well-behaved.
 */
float Phaser::_ap_coeff(float freq_hz) const noexcept {
    const float fc = std::max(1.0f, std::min(freq_hz, sample_rate_ * 0.499f));
    const float w  = std::tan(PI_F * fc / sample_rate_);
    return (w - 1.0f) / (w + 1.0f);
}

// ─────────────────────────────────────────────────────────────────────────────
// Main processing loop
// ─────────────────────────────────────────────────────────────────────────────

void Phaser::process(float* left, float* right, int num_samples) noexcept {
    const float phase_inc  = rate_hz_ / sample_rate_;
    // Precompute the log-frequency range factor once per block.
    const float log_range  = (min_freq_ > 0.0f && max_freq_ > min_freq_)
                             ? std::log(max_freq_ / min_freq_)
                             : 0.0f;

    for (int i = 0; i < num_samples; ++i) {
        // ── LFO → pole frequency (logarithmic sweep) ─────────────────────────
        // Sine LFO output ∈ [−1, +1], scaled by depth, mapped to [0, 1] range.
        const float lfo_l = std::sin(TWO_PI_F * lfo_phase_l_);
        const float lfo_r = std::sin(TWO_PI_F * lfo_phase_r_);
        const float t_l   = 0.5f * (1.0f + depth_ * lfo_l);  // 0..1
        const float t_r   = 0.5f * (1.0f + depth_ * lfo_r);

        // fc = min_freq * (max_freq/min_freq)^t = min_freq * exp(log_range * t)
        const float fc_l  = min_freq_ * std::exp(log_range * t_l);
        const float fc_r  = min_freq_ * std::exp(log_range * t_r);

        const float coeff_l = _ap_coeff(fc_l);
        const float coeff_r = _ap_coeff(fc_r);

        // ── Apply feedback to inputs ──────────────────────────────────────────
        float in_l = left[i]  + feedback_ * fb_l_ + DENORMAL_GUARD;
        float in_r = right[i] + feedback_ * fb_r_ + DENORMAL_GUARD;

        // ── Cascade all-pass stages (channel 0 = L, channel 1 = R) ───────────
        float ap_l = in_l;
        float ap_r = in_r;

        for (int s = 0; s < num_stages_; ++s) {
            // y[n] = c · (x[n] − y[n−1]) + x[n−1]
            const float new_y_l = coeff_l * (ap_l - ap_y_prev_[s][0]) + ap_x_prev_[s][0];
            const float new_y_r = coeff_r * (ap_r - ap_y_prev_[s][1]) + ap_x_prev_[s][1];

            ap_x_prev_[s][0] = ap_l;
            ap_y_prev_[s][0] = new_y_l;
            ap_x_prev_[s][1] = ap_r;
            ap_y_prev_[s][1] = new_y_r;

            ap_l = new_y_l;
            ap_r = new_y_r;
        }

        // Update feedback state for the next sample.
        fb_l_ = ap_l;
        fb_r_ = ap_r;

        // ── Wet/dry mix ───────────────────────────────────────────────────────
        left[i]  = dry_ * left[i]  + wet_ * ap_l;
        right[i] = dry_ * right[i] + wet_ * ap_r;

        // ── Advance LFO phases ────────────────────────────────────────────────
        lfo_phase_l_ += phase_inc;
        lfo_phase_r_ += phase_inc;
        if (lfo_phase_l_ >= 1.0f) lfo_phase_l_ -= 1.0f;
        if (lfo_phase_r_ >= 1.0f) lfo_phase_r_ -= 1.0f;
    }
}
