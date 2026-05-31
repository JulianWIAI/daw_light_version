#include "BrickwallLimiter.h"
#include <cmath>
#include <algorithm>
#include <cstring>

BrickwallLimiter::BrickwallLimiter(float sample_rate)
    : sample_rate_(sample_rate)
    , ceiling_linear_(dsp::db_to_linear(-0.1f))
    , lookahead_samples_(static_cast<int>(0.005f * sample_rate))
    , gain_(1.0f)
    , write_pos_(0)
    , prev_l_(0.0f), prev_r_(0.0f)
    , attack_ms_(0.5f), release_ms_(150.0f)
{
    delay_l_.fill(0.0f);
    delay_r_.fill(0.0f);
    prepare(sample_rate);
}

void BrickwallLimiter::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    // Clamp look-ahead to the maximum buffer size.
    lookahead_samples_ = std::min(lookahead_samples_,
                                  static_cast<int>(0.020f * sample_rate));
    lookahead_samples_ = std::min(lookahead_samples_, MAX_LOOKAHEAD - 1);
    attack_coeff_  = dsp::time_to_coeff(attack_ms_,  sample_rate_);
    release_coeff_ = dsp::time_to_coeff(release_ms_, sample_rate_);
    reset();
}

void BrickwallLimiter::reset() noexcept {
    delay_l_.fill(0.0f);
    delay_r_.fill(0.0f);
    write_pos_ = 0;
    gain_ = 1.0f;
    prev_l_ = 0.0f;
    prev_r_ = 0.0f;
}

void BrickwallLimiter::set_ceiling(float db) noexcept {
    ceiling_linear_ = dsp::db_to_linear(db);
}

void BrickwallLimiter::set_lookahead(float ms) noexcept {
    const float clamped = std::min(ms, 20.0f);
    lookahead_samples_ = std::min(
        static_cast<int>(clamped * sample_rate_ / 1000.0f),
        MAX_LOOKAHEAD - 1);
}

void BrickwallLimiter::set_attack(float ms) noexcept {
    attack_ms_ = ms;
    attack_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void BrickwallLimiter::set_release(float ms) noexcept {
    release_ms_ = ms;
    release_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

// ─────────────────────────────────────────────────────────────────────────────
// Catmull-Rom peak detection
// ─────────────────────────────────────────────────────────────────────────────

// Returns the maximum absolute interpolated value at t=0.25, 0.5, 0.75
// between samples p1 and p2, using p0 and p3 as context.
// This models the inter-sample waveform that a D/A converter would produce.
float BrickwallLimiter::true_peak(float p0, float p1, float p2, float p3) noexcept {
    float peak = std::max(std::abs(p1), std::abs(p2));
    for (float t : {0.25f, 0.5f, 0.75f}) {
        float interp = catmull_rom(p0, p1, p2, p3, t);
        peak = std::max(peak, std::abs(interp));
    }
    return peak;
}

// ─────────────────────────────────────────────────────────────────────────────
// Main process loop
// ─────────────────────────────────────────────────────────────────────────────

void BrickwallLimiter::process(float* left, float* right, int num_samples) noexcept {
    const int buf_size = MAX_LOOKAHEAD;

    for (int i = 0; i < num_samples; ++i) {
        const float in_l = left[i];
        const float in_r = right[i];

        // ── True-peak detection ───────────────────────────────────────────
        // We need three future samples (p2 = in, p3 = next in buffer) plus
        // previous (p0 = prev).  For simplicity we evaluate the Catmull-Rom
        // interpolation on the *input* sample and its neighbours.
        //
        // We use the previous input (prev) as p0, current input as p1,
        // and the write-ahead value currently in the delay line as p2/p3.
        // This gives a real-time-friendly approximation of inter-sample peaks.
        const int read_ahead_1 = (write_pos_ + 1) % buf_size;
        const int read_ahead_2 = (write_pos_ + 2) % buf_size;

        float p2_l = delay_l_[read_ahead_1];
        float p3_l = delay_l_[read_ahead_2];
        float p2_r = delay_r_[read_ahead_1];
        float p3_r = delay_r_[read_ahead_2];

        float peak_l = true_peak(prev_l_, in_l, p2_l, p3_l);
        float peak_r = true_peak(prev_r_, in_r, p2_r, p3_r);
        float peak   = std::max(peak_l, peak_r);

        // ── Gain computation ──────────────────────────────────────────────
        // Compute the instantaneous gain needed to keep peak below ceiling.
        float desired_gain = (peak > ceiling_linear_) ? ceiling_linear_ / peak : 1.0f;

        // Apply attack / release smoothing.
        // Attack (gain going down) must be faster than release (gain recovering).
        if (desired_gain < gain_) {
            // Signal is too loud — clamp down using attack coefficient.
            gain_ = attack_coeff_ * gain_ + (1.0f - attack_coeff_) * desired_gain;
        } else {
            // Signal has relaxed — let gain recover using release coefficient.
            gain_ = release_coeff_ * gain_ + (1.0f - release_coeff_) * desired_gain;
        }
        gain_ = std::min(gain_, 1.0f);  // never amplify

        // ── Delay line write / read ───────────────────────────────────────
        delay_l_[write_pos_] = in_l;
        delay_r_[write_pos_] = in_r;

        // Read the sample that was written lookahead_samples_ ago.
        const int read_pos = (write_pos_ - lookahead_samples_ + buf_size) % buf_size;
        const float out_l = delay_l_[read_pos] * gain_;
        const float out_r = delay_r_[read_pos] * gain_;

        write_pos_ = (write_pos_ + 1) % buf_size;

        prev_l_ = in_l;
        prev_r_ = in_r;

        left[i]  = out_l;
        right[i] = out_r;
    }
}
