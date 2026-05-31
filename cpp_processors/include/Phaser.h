/**
 * Phaser.h -- Multi-Stage All-Pass Filter Phaser
 * ================================================
 * A classic phaser effect built from N cascaded first-order all-pass filters,
 * whose pole frequencies are swept by an LFO between min_freq and max_freq
 * using a logarithmic (musical) frequency mapping.
 *
 * Supported stage counts: 2, 4, 6, 8, 12.
 * Higher stage counts produce more notches and a richer, denser phase-shift.
 *
 * All-pass transfer function:
 *   H(z) = (c + z⁻¹) / (1 + c·z⁻¹)
 *   where c = (tan(π·fc/sr) − 1) / (tan(π·fc/sr) + 1)
 *
 * Feedback (resonance) feeds the all-pass output back to the input, sharpening
 * the notches and adding a resonant quality.
 *
 * Stereo width is implemented by giving the R-channel LFO a phase offset of
 * (stereo_offset × π) cycles relative to the L channel.
 */

#pragma once
#include <cmath>
#include "DspHelpers.h"

static constexpr int PHASER_MAX_STAGES = 12;


class Phaser {
public:
    explicit Phaser(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    // ── Parameter setters ────────────────────────────────────────────────────

    /** Number of all-pass stages. Accepted values: 2, 4, 6, 8, 12. */
    void set_stages(int n) noexcept;

    /** LFO sweep rate in Hz. */
    void set_rate(float hz) noexcept;

    /** LFO modulation depth 0..1 (scales the log-frequency sweep range). */
    void set_depth(float d) noexcept;

    /** Minimum pole frequency in Hz (LFO lower bound). */
    void set_min_freq(float hz) noexcept;

    /** Maximum pole frequency in Hz (LFO upper bound). */
    void set_max_freq(float hz) noexcept;

    /** Feedback / resonance 0..0.98. */
    void set_feedback(float f) noexcept;

    /** Wet/dry mix 0..1. */
    void set_wet(float w) noexcept;

    /** Stereo phase offset as a fraction of one cycle (0..1). */
    void set_stereo_offset(float f) noexcept;

    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    int   num_stages_;
    float rate_hz_;
    float depth_;
    float min_freq_, max_freq_;
    float feedback_;
    float wet_, dry_;
    float stereo_offset_;   // R LFO phase offset in cycles

    float lfo_phase_l_, lfo_phase_r_;

    // All-pass filter state: [stage][channel] where channel 0=L, 1=R
    float ap_x_prev_[PHASER_MAX_STAGES][2];  // x[n-1] for each stage × channel
    float ap_y_prev_[PHASER_MAX_STAGES][2];  // y[n-1] for each stage × channel

    float fb_l_, fb_r_;  // Feedback state (output of last stage fed back to input)

    /** Compute the all-pass pole coefficient for a given frequency. */
    float _ap_coeff(float freq_hz) const noexcept;
};
