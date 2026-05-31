/*
 * LoudnessAutomation.h  --  Real-Time Loudness Automation Processor
 * ==================================================================
 * Chains RmsAnalyzer → EnvelopeFollower → PidController to compute a
 * target gain multiplier, then applies per-sample linear interpolation
 * of that multiplier to absolutely prevent zipper noise.
 *
 * Signal flow per process() call:
 *
 *   1. RmsAnalyzer::compute_rms(left, right, n)
 *              ↓ linear RMS
 *   2. EnvelopeFollower::process(rms)
 *              ↓ smoothed RMS (linear)
 *   3. RmsAnalyzer::to_dbfs(smoothed_rms)
 *              ↓ dBFS
 *   4. PidController::process(current_db, dt)
 *              ↓ gain_correction_db  (Kp·e + Ki·∫e + Kd·de/dt)
 *   5. target_gain = pow(10, gain_correction_db / 20)  [clamped]
 *
 *   6. Per-sample interpolation (zipper-noise prevention):
 *        gain_step = (target_gain - current_gain) / n_frames
 *        For each sample i:
 *          current_gain += gain_step
 *          left[i]  *= current_gain
 *          right[i] *= current_gain
 *
 * The process() signature (float* l, float* r, int n) matches the
 * generic process_block_impl<T> template in bindings.cpp so the class
 * can be bound with the shared pybind11 wrapper.
 */

#pragma once

#include "RmsAnalyzer.h"
#include "EnvelopeFollower.h"
#include "PidController.h"
#include <cmath>
#include <algorithm>

class LoudnessAutomation {
public:
    struct Params {
        float target_dbfs  = -18.0f;  // desired RMS loudness target (dBFS)
        float attack_ms    = 20.0f;   // envelope follower attack time
        float release_ms   = 200.0f;  // envelope follower release time
        float kp           = 1.0f;    // PID proportional gain
        float ki           = 0.1f;    // PID integral gain
        float kd           = 0.05f;   // PID derivative gain
        float gain_min_db  = -30.0f;  // minimum allowed gain correction
        float gain_max_db  = +12.0f;  // maximum allowed gain correction
    };

    // Construct with default 44.1 kHz and default Params.
    explicit LoudnessAutomation(float sample_rate = 44100.0f) noexcept;

    // Re-initialise at a new sample rate (recomputes envelope coefficients).
    void prepare(float sample_rate) noexcept;

    // Replace all parameters.  Rebuilds internal EnvelopeFollower + PID.
    void set_params(const Params& p) noexcept;

    // Read current parameters.
    const Params& params() const noexcept { return params_; }

    // Process one block in-place.  Applies per-sample gain interpolation.
    // Signature matches process_block_impl<T>: proc.process(l, r, n).
    void process(float* left, float* right, int n_frames) noexcept;

    // Zero all controller state without changing parameters.
    void reset() noexcept;

    // Read the current instantaneous gain multiplier (linear, not dB).
    float current_gain()    const noexcept { return current_gain_; }

    // Read the current gain in dBFS for metering.
    float current_gain_db() const noexcept;

private:
    float           sample_rate_;
    Params          params_;
    EnvelopeFollower follower_;
    PidController    pid_;
    float           current_gain_;  // smoothly-interpolated gain (linear)
    float           target_gain_;   // requested gain after PID update (linear)

    // Rebuild sub-components after parameter or sample-rate changes.
    void _rebuild() noexcept;

    // Safe powf: avoids NaN for negative arguments via clamping.
    static float _db_to_linear(float db) noexcept {
        return std::powf(10.0f, db * 0.05f);  // 0.05 = 1/20
    }
    static float _clamp(float v, float lo, float hi) noexcept {
        return v < lo ? lo : (v > hi ? hi : v);
    }
};
