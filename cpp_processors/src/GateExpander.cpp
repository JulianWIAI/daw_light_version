#include "GateExpander.h"
#include <cmath>
#include <algorithm>

GateExpander::GateExpander(float sample_rate)
    : sample_rate_(sample_rate)
    , ratio_(1.0f)
    , level_(0.0f)
    , gain_(0.0f)   // start closed
    , state_(GateState::CLOSED)
    , hold_samples_(0)
    , hold_samples_left_(0)
    , threshold_db_(-40.0f)
    , hysteresis_db_(6.0f)
    , attack_ms_(1.0f)
    , release_ms_(100.0f)
    , hold_ms_(50.0f)
{
    range_linear_          = dsp::db_to_linear(-80.0f);
    open_threshold_linear_ = dsp::db_to_linear(threshold_db_);
    recompute_thresholds();
    recompute_coeffs();
}

void GateExpander::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    recompute_thresholds();
    recompute_coeffs();
    reset();
}

void GateExpander::reset() noexcept {
    level_ = 0.0f;
    gain_  = range_linear_;
    state_ = GateState::CLOSED;
    hold_samples_left_ = 0;
}

void GateExpander::set_threshold(float db) noexcept {
    threshold_db_ = db;
    recompute_thresholds();
}

void GateExpander::set_hysteresis(float db) noexcept {
    hysteresis_db_ = db;
    recompute_thresholds();
}

void GateExpander::set_ratio(float r) noexcept {
    ratio_ = r > 1.0f ? r : 1.0f;  // ratio < 1 has no defined meaning for a gate
}

void GateExpander::set_attack(float ms) noexcept {
    attack_ms_ = ms;
    attack_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void GateExpander::set_hold(float ms) noexcept {
    hold_ms_ = ms;
    hold_samples_ = static_cast<int>(ms * sample_rate_ / 1000.0f);
}

void GateExpander::set_release(float ms) noexcept {
    release_ms_ = ms;
    release_coeff_    = dsp::time_to_coeff(ms, sample_rate_);
    det_release_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void GateExpander::set_range(float db) noexcept {
    range_linear_ = dsp::db_to_linear(db);
}

void GateExpander::recompute_thresholds() noexcept {
    open_threshold_linear_  = dsp::db_to_linear(threshold_db_);
    // close threshold is below open threshold by hysteresis_db
    close_threshold_linear_ = dsp::db_to_linear(threshold_db_ - hysteresis_db_);
}

void GateExpander::recompute_coeffs() noexcept {
    attack_coeff_      = dsp::time_to_coeff(attack_ms_,  sample_rate_);
    release_coeff_     = dsp::time_to_coeff(release_ms_, sample_rate_);
    hold_samples_      = static_cast<int>(hold_ms_ * sample_rate_ / 1000.0f);
    // Level detector: very fast attack (0.1ms) so it catches transients instantly.
    det_attack_coeff_  = dsp::time_to_coeff(0.1f,        sample_rate_);
    det_release_coeff_ = dsp::time_to_coeff(release_ms_, sample_rate_);
}

void GateExpander::process(float* left, float* right, int num_samples) noexcept {
    const float epsilon = 0.001f;  // used for "close enough to target" checks

    for (int i = 0; i < num_samples; ++i) {
        const float abs_l = std::abs(left[i]);
        const float abs_r = std::abs(right[i]);
        const float in_peak = std::max(abs_l, abs_r) + DENORMAL_GUARD;

        // ── Level detection (peak follower) ───────────────────────────────
        if (in_peak > level_) {
            level_ = det_attack_coeff_  * level_ + (1.0f - det_attack_coeff_)  * in_peak;
        } else {
            level_ = det_release_coeff_ * level_ + (1.0f - det_release_coeff_) * in_peak;
        }

        // ── State machine ─────────────────────────────────────────────────
        switch (state_) {
            case GateState::CLOSED:
                if (level_ > open_threshold_linear_) {
                    state_ = GateState::OPENING;
                }
                break;

            case GateState::OPENING:
                // Ramp gain upward.
                gain_ = attack_coeff_ * gain_ + (1.0f - attack_coeff_) * 1.0f;
                if (gain_ >= 1.0f - epsilon) {
                    gain_  = 1.0f;
                    state_ = GateState::OPEN;
                }
                break;

            case GateState::OPEN:
                if (level_ < close_threshold_linear_) {
                    hold_samples_left_ = hold_samples_;
                    state_ = GateState::HOLDING;
                }
                break;

            case GateState::HOLDING:
                if (level_ > close_threshold_linear_) {
                    // Signal came back up — return to open without re-triggering attack.
                    // Note: we use close_threshold here (not open) so the signal must
                    // exceed the hysteresis band to go back to OPEN cleanly.
                    state_ = GateState::OPEN;
                } else if (hold_samples_left_ <= 0) {
                    state_ = GateState::CLOSING;
                } else {
                    --hold_samples_left_;
                }
                break;

            case GateState::CLOSING:
                if (level_ > open_threshold_linear_) {
                    // Fast recovery: signal returned above threshold during close ramp.
                    state_ = GateState::OPENING;
                    break;
                }
                // Ramp gain downward toward range_linear_.
                gain_ = release_coeff_ * gain_ + (1.0f - release_coeff_) * range_linear_;
                if (gain_ <= range_linear_ + epsilon) {
                    gain_  = range_linear_;
                    state_ = GateState::CLOSED;
                }
                break;
        }

        // ── Gain value for this sample ─────────────────────────────────────
        float applied_gain = gain_;

        // In OPEN state with ratio > 1 (expander): apply below-threshold expansion.
        // The expansion formula gradually reduces gain below the threshold rather
        // than applying a hard gate, giving a more natural-sounding noise floor.
        if ((state_ == GateState::OPEN || state_ == GateState::OPENING) && ratio_ > 1.0f) {
            if (level_ < open_threshold_linear_ && open_threshold_linear_ > 0.0f) {
                const float norm = level_ / open_threshold_linear_;
                // exponent = (1/ratio - 1) < 0 → below threshold gain decreases.
                float expansion = std::pow(norm + DENORMAL_GUARD, 1.0f / ratio_ - 1.0f);
                expansion = std::max(expansion, range_linear_);
                applied_gain = gain_ * expansion;
            }
        }

        // In CLOSED state, hold the gain at the floor rather than continuing to
        // smooth (avoids a very slow asymptotic approach to range_linear_ that
        // would let tiny amounts of signal bleed through).
        if (state_ == GateState::CLOSED) {
            applied_gain = range_linear_;
        }

        left[i]  *= applied_gain;
        right[i] *= applied_gain;
    }
}
