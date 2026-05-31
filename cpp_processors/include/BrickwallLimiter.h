#pragma once
#include <array>
#include "DspHelpers.h"

// BrickwallLimiter
// ─────────────────────────────────────────────────────────────────────────────
// True-peak limiter with look-ahead delay, Catmull-Rom inter-sample peak
// detection, and independent attack / release gain smoothing.
//
// The look-ahead delay ensures the gain-reduction ramp is already applied
// before the peak actually arrives at the output — this eliminates any
// "overshoot" that a feed-forward limiter without delay would exhibit.
//
// True-peak detection evaluates four interpolated sub-samples between each
// pair of consecutive input samples (at t = 0.25, 0.5, 0.75) using
// Catmull-Rom splines.  This catches inter-sample peaks that would clip
// after D/A conversion even though the digital samples themselves are below
// the ceiling.
// ─────────────────────────────────────────────────────────────────────────────

class BrickwallLimiter {
public:
    explicit BrickwallLimiter(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    // Setters — safe to call between blocks.
    void set_ceiling(float db) noexcept;
    void set_lookahead(float ms) noexcept;
    void set_attack(float ms) noexcept;
    void set_release(float ms) noexcept;

    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;

    float ceiling_linear_;
    int   lookahead_samples_;
    float gain_;             // current instantaneous gain (1.0 = no reduction)
    float attack_coeff_;
    float release_coeff_;

    // Circular delay buffers — the audio is held here for look-ahead_samples_
    // so gain reduction computed from future input can be applied in time.
    std::array<float, MAX_LOOKAHEAD> delay_l_;
    std::array<float, MAX_LOOKAHEAD> delay_r_;
    int write_pos_;          // circular buffer write head

    // Previous input sample needed for Catmull-Rom look-back (p0).
    float prev_l_, prev_r_;

    // Recompute smoothing coefficients from stored ms values after prepare().
    float attack_ms_, release_ms_;

    // Catmull-Rom evaluation at fractional position t in [0,1] given four
    // consecutive sample values p0..p3.  Returns the interpolated value.
    static inline float catmull_rom(float p0, float p1, float p2, float p3, float t) noexcept {
        // Standard Catmull-Rom formulation — the 0.5 factor comes from the
        // tension parameter τ = 0.5 used in the centripetal version.
        return 0.5f * ((2.0f * p1)
            + (-p0 + p2) * t
            + (2.0f * p0 - 5.0f * p1 + 4.0f * p2 - p3) * t * t
            + (-p0 + 3.0f * p1 - 3.0f * p2 + p3) * t * t * t);
    }

    // Returns the maximum absolute value across four Catmull-Rom sub-samples
    // inserted between p1 and p2 (needing context samples p0 and p3).
    static float true_peak(float p0, float p1, float p2, float p3) noexcept;
};
