/*
 * EnvelopeFollower.h  --  One-Pole IIR Amplitude Envelope Follower
 * =================================================================
 * Smooths a sequence of instantaneous RMS values using two separate
 * first-order IIR (low-pass) coefficients: a fast attack and a slow release.
 *
 * Algorithm:
 *   If input >= env :  env = attack_coeff  * env + (1 - attack_coeff)  * input
 *   If input <  env :  env = release_coeff * env + (1 - release_coeff) * input
 *
 * Coefficient computation (one-pole bilinear mapping):
 *   coeff = exp( -1.0 / (time_ms * 0.001 * sample_rate) )
 *
 * coeff → 1.0 means extremely slow (infinite time constant).
 * coeff → 0.0 means instantaneous tracking (zero time constant).
 */

#pragma once
#include <cmath>

class EnvelopeFollower {
public:
    // Construct with default 44.1 kHz, 10 ms attack, 100 ms release.
    explicit EnvelopeFollower(float sample_rate = 44100.0f,
                               float attack_ms  = 10.0f,
                               float release_ms = 100.0f) noexcept;

    // Rebuild IIR coefficients after a sample-rate or time-constant change.
    void prepare(float sample_rate, float attack_ms, float release_ms) noexcept;

    // Feed one RMS sample; returns the smoothed envelope level (linear).
    float process(float input_rms) noexcept;

    // Zero the internal envelope state.
    void reset() noexcept;

    // Read current envelope level without advancing it.
    float current() const noexcept { return env_; }

private:
    float attack_coeff_;   // IIR coefficient for the rising edge
    float release_coeff_;  // IIR coefficient for the falling edge
    float env_;            // current smoothed envelope value

    // Compute a one-pole IIR coefficient from a time constant.
    static float _coeff(float time_ms, float sample_rate) noexcept;
};
