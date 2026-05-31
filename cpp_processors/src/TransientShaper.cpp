#include "TransientShaper.h"
#include <cmath>
#include <algorithm>

TransientShaper::TransientShaper(float sample_rate)
    : sample_rate_(sample_rate)
    , attack_linear_(1.0f)
    , sustain_linear_(1.0f)
    , fast_env_l_(0.0f), fast_env_r_(0.0f)
    , slow_env_l_(0.0f), slow_env_r_(0.0f)
    , smooth_gain_l_(1.0f), smooth_gain_r_(1.0f)
    , fast_attack_ms_(2.0f), fast_release_ms_(20.0f)
    , slow_attack_ms_(20.0f), slow_release_ms_(200.0f)
{
    recompute_coeffs();
}

void TransientShaper::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    recompute_coeffs();
    reset();
}

void TransientShaper::reset() noexcept {
    fast_env_l_ = fast_env_r_ = 0.0f;
    slow_env_l_ = slow_env_r_ = 0.0f;
    smooth_gain_l_ = smooth_gain_r_ = 1.0f;
}

void TransientShaper::set_attack_gain(float db) noexcept {
    attack_linear_ = dsp::db_to_linear(dsp::clamp(db, -24.0f, 24.0f));
}

void TransientShaper::set_sustain_gain(float db) noexcept {
    sustain_linear_ = dsp::db_to_linear(dsp::clamp(db, -24.0f, 24.0f));
}

void TransientShaper::set_fast_attack(float ms) noexcept {
    fast_attack_ms_ = ms;
    fast_attack_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void TransientShaper::set_fast_release(float ms) noexcept {
    fast_release_ms_ = ms;
    fast_release_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void TransientShaper::set_slow_attack(float ms) noexcept {
    slow_attack_ms_ = ms;
    slow_attack_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void TransientShaper::set_slow_release(float ms) noexcept {
    slow_release_ms_ = ms;
    slow_release_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void TransientShaper::recompute_coeffs() noexcept {
    fast_attack_coeff_  = dsp::time_to_coeff(fast_attack_ms_,  sample_rate_);
    fast_release_coeff_ = dsp::time_to_coeff(fast_release_ms_, sample_rate_);
    slow_attack_coeff_  = dsp::time_to_coeff(slow_attack_ms_,  sample_rate_);
    slow_release_coeff_ = dsp::time_to_coeff(slow_release_ms_, sample_rate_);
    gain_smooth_coeff_  = dsp::time_to_coeff(1.0f, sample_rate_);  // 1ms output gain smoother
}

// ─────────────────────────────────────────────────────────────────────────────
// One-pole peak envelope follower helper (shared for both channels)
// ─────────────────────────────────────────────────────────────────────────────

static inline float follow_envelope(float abs_input, float env,
                                    float atk_coeff, float rel_coeff) noexcept {
    if (abs_input > env) {
        return atk_coeff * env + (1.0f - atk_coeff) * abs_input;
    } else {
        return rel_coeff * env + (1.0f - rel_coeff) * abs_input;
    }
}

void TransientShaper::process(float* left, float* right, int num_samples) noexcept {
    const float epsilon = DENORMAL_GUARD;

    for (int i = 0; i < num_samples; ++i) {
        const float abs_l = std::abs(left[i])  + epsilon;
        const float abs_r = std::abs(right[i]) + epsilon;

        // ── Update FAST envelope followers ───────────────────────────────
        fast_env_l_ = follow_envelope(abs_l, fast_env_l_, fast_attack_coeff_, fast_release_coeff_);
        fast_env_r_ = follow_envelope(abs_r, fast_env_r_, fast_attack_coeff_, fast_release_coeff_);

        // ── Update SLOW envelope followers ───────────────────────────────
        slow_env_l_ = follow_envelope(abs_l, slow_env_l_, slow_attack_coeff_, slow_release_coeff_);
        slow_env_r_ = follow_envelope(abs_r, slow_env_r_, slow_attack_coeff_, slow_release_coeff_);

        // ── Per-channel gain blend ────────────────────────────────────────
        // attack_weight measures "how much faster is the fast env than the slow env".
        // When fast > slow, we are in a transient attack phase.
        // When fast ≈ slow, the signal is in a steady sustain or release phase.
        auto compute_gain = [&](float fast_env, float slow_env) -> float {
            const float diff = fast_env - slow_env;
            // Normalise by fast_env so the weight saturates toward 1 during
            // very sharp transients regardless of absolute level.
            const float attack_weight = dsp::clamp(diff / (fast_env + epsilon), 0.0f, 1.0f);

            // Blend between the two gain targets.
            // Using linear blending rather than dB here preserves the
            // transient character better — a hard transient should get
            // a precise linear scaling, not a log-smoothed average.
            return attack_linear_  * attack_weight
                 + sustain_linear_ * (1.0f - attack_weight);
        };

        const float target_l = compute_gain(fast_env_l_, slow_env_l_);
        const float target_r = compute_gain(fast_env_r_, slow_env_r_);

        // ── 1ms gain smoother to kill zipper noise on fast changes ────────
        smooth_gain_l_ = gain_smooth_coeff_ * smooth_gain_l_ + (1.0f - gain_smooth_coeff_) * target_l;
        smooth_gain_r_ = gain_smooth_coeff_ * smooth_gain_r_ + (1.0f - gain_smooth_coeff_) * target_r;

        left[i]  *= smooth_gain_l_;
        right[i] *= smooth_gain_r_;
    }
}
