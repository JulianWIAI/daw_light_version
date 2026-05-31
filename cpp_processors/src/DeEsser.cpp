#include "DeEsser.h"
#include <cmath>
#include <algorithm>

DeEsser::DeEsser(float sample_rate)
    : sample_rate_(sample_rate)
    , freq_hz_(7000.0f)
    , threshold_linear_(dsp::db_to_linear(-10.0f))
    , ratio_(4.0f)
    , gain_l_(1.0f), gain_r_(1.0f)
    , mode_(Mode::WIDEBAND)
    , attack_ms_(1.0f), release_ms_(80.0f)
{
    rebuild_filters();
    prepare(sample_rate);
}

void DeEsser::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    attack_coeff_  = dsp::time_to_coeff(attack_ms_,  sample_rate_);
    release_coeff_ = dsp::time_to_coeff(release_ms_, sample_rate_);
    rebuild_filters();
    reset();
}

void DeEsser::reset() noexcept {
    sc_l_[0].reset(); sc_l_[1].reset();
    sc_r_[0].reset(); sc_r_[1].reset();
    split_lp_l_.reset(); split_lp_r_.reset();
    split_hp_l_.reset(); split_hp_r_.reset();
    gain_l_ = gain_r_ = 1.0f;
}

void DeEsser::set_frequency(float hz) noexcept {
    freq_hz_ = hz;
    rebuild_filters();
}

void DeEsser::set_threshold(float db) noexcept {
    threshold_linear_ = dsp::db_to_linear(db);
}

void DeEsser::set_ratio(float ratio) noexcept {
    ratio_ = ratio;
}

void DeEsser::set_attack(float ms) noexcept {
    attack_ms_ = ms;
    attack_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void DeEsser::set_release(float ms) noexcept {
    release_ms_ = ms;
    release_coeff_ = dsp::time_to_coeff(ms, sample_rate_);
}

void DeEsser::set_split_mode(bool split) noexcept {
    mode_ = split ? Mode::SPLIT : Mode::WIDEBAND;
}

void DeEsser::rebuild_filters() noexcept {
    // The target bandwidth is ±2000 Hz around freq_hz_.
    // For a bandpass biquad, Q = centre_freq / bandwidth.
    // bandwidth = 2 * 2000 = 4000 Hz → Q = freq_hz_ / 4000.
    // Cascading two identical BPF stages narrows the bandwidth by roughly
    // sqrt(2) but also raises the Q effect, giving steeper skirts.
    const float q = freq_hz_ / 4000.0f;
    const BiquadCoeffs bp = biquad::make_bandpass(freq_hz_, q > 0.5f ? q : 0.5f, sample_rate_);
    sc_bp_coeffs_[0] = sc_bp_coeffs_[1] = bp;

    // Split-mode LP and HP at the centre frequency with Q=0.707 (Butterworth).
    split_lp_coeffs_ = biquad::make_lowpass (freq_hz_, BUTTERWORTH_Q, sample_rate_);
    split_hp_coeffs_ = biquad::make_highpass(freq_hz_, BUTTERWORTH_Q, sample_rate_);
}

void DeEsser::process(float* left, float* right, int num_samples) noexcept {
    for (int i = 0; i < num_samples; ++i) {
        const float in_l = left[i];
        const float in_r = right[i];

        // ── Sidechain: two cascaded bandpass filters ─────────────────────
        // Two stages improve selectivity around the sibilance band so that
        // broad-spectrum transients (e.g., consonant 't') do not trigger
        // excessive reduction.
        float sc_l = biquad_process(in_l, sc_bp_coeffs_[0], sc_l_[0]);
        sc_l = biquad_process(sc_l, sc_bp_coeffs_[1], sc_l_[1]);

        float sc_r = biquad_process(in_r, sc_bp_coeffs_[0], sc_r_[0]);
        sc_r = biquad_process(sc_r, sc_bp_coeffs_[1], sc_r_[1]);

        const float peak_l = std::abs(sc_l) + DENORMAL_GUARD;
        const float peak_r = std::abs(sc_r) + DENORMAL_GUARD;

        // ── Gain computation (per channel independent detection) ──────────
        // target_gain = ceiling / peak if peak > ceiling, else 1.0
        // With ratio: effective_ceiling raised so that full gain reduction
        // only happens when peak is ratio times the threshold.
        auto compute_gain = [&](float peak, float& gain_state) {
            float target = 1.0f;
            if (peak > threshold_linear_) {
                // Ratio-based computation: at threshold, gain = 1;
                // above threshold, gain decreases toward ceiling/peak.
                const float over = peak / threshold_linear_;  // > 1
                const float compressed_over = std::pow(over, 1.0f / ratio_);
                target = 1.0f / compressed_over;
            }
            // Attack: gain going down; release: gain recovering.
            if (target < gain_state) {
                gain_state = attack_coeff_  * gain_state + (1.0f - attack_coeff_)  * target;
            } else {
                gain_state = release_coeff_ * gain_state + (1.0f - release_coeff_) * target;
            }
        };

        compute_gain(peak_l, gain_l_);
        compute_gain(peak_r, gain_r_);

        float out_l, out_r;

        if (mode_ == Mode::WIDEBAND) {
            // Apply gain uniformly to the entire signal.
            out_l = in_l * gain_l_;
            out_r = in_r * gain_r_;
        } else {
            // SPLIT mode: only attenuate the high-frequency component.
            // The low-frequency part is preserved, avoiding "pumping" on
            // the full signal when there is heavy mid-frequency content.
            float lo_l = biquad_process(in_l, split_lp_coeffs_, split_lp_l_);
            float lo_r = biquad_process(in_r, split_lp_coeffs_, split_lp_r_);
            float hi_l = biquad_process(in_l, split_hp_coeffs_, split_hp_l_);
            float hi_r = biquad_process(in_r, split_hp_coeffs_, split_hp_r_);

            out_l = lo_l + hi_l * gain_l_;
            out_r = lo_r + hi_r * gain_r_;
        }

        left[i]  = out_l;
        right[i] = out_r;
    }
}
