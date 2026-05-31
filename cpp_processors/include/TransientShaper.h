#pragma once
#include "DspHelpers.h"

// TransientShaper
// ─────────────────────────────────────────────────────────────────────────────
// Separates a signal into "transient" (fast attack) and "sustain" (slow body)
// components by running two envelope followers at different time constants and
// using their difference as a weight.
//
// Two envelope followers per channel:
//   FAST — responds quickly; tracks attack fronts.
//   SLOW — responds slowly; tracks the body/sustain of the signal.
//
// The transient weight per sample is:
//   attack_weight = clamp( (fast - slow) / (fast + ε), 0, 1 )
//
// This continuously blends between two independent gain values:
//   attack_gain   — dB of boost/cut applied during transients (fast > slow)
//   sustain_gain  — dB of boost/cut applied during sustain (slow dominates)
//
// The resulting per-sample gain is smoothed with a 1ms coefficient to prevent
// zipper noise on abrupt gain changes.
// ─────────────────────────────────────────────────────────────────────────────

class TransientShaper {
public:
    explicit TransientShaper(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    // Gain range: −24 to +24 dB.
    void set_attack_gain(float db) noexcept;
    void set_sustain_gain(float db) noexcept;

    // Envelope time constants.
    void set_fast_attack(float ms) noexcept;
    void set_fast_release(float ms) noexcept;
    void set_slow_attack(float ms) noexcept;
    void set_slow_release(float ms) noexcept;

    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;

    float attack_linear_;   // linear amplitude for attack gain
    float sustain_linear_;  // linear amplitude for sustain gain

    // Envelope follower state per channel.
    float fast_env_l_, fast_env_r_;
    float slow_env_l_, slow_env_r_;

    // Smoothed output gain per channel (1ms smoother to kill zipper noise).
    float smooth_gain_l_, smooth_gain_r_;

    // IIR one-pole coefficients for the four envelope followers.
    float fast_attack_coeff_, fast_release_coeff_;
    float slow_attack_coeff_, slow_release_coeff_;

    // 1ms gain smoother coefficient (recomputed on prepare()).
    float gain_smooth_coeff_;

    // Stored ms values so prepare() can recompute coefficients at new sample rate.
    float fast_attack_ms_, fast_release_ms_;
    float slow_attack_ms_, slow_release_ms_;

    void recompute_coeffs() noexcept;
};
