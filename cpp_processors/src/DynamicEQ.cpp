#include "DynamicEQ.h"
#include <cmath>
#include <algorithm>

DynamicEQ::DynamicEQ(float sample_rate)
    : sample_rate_(sample_rate)
    , num_bands_(4)
{
    // Default four-band setup: sub-bass, mid, presence, air.
    band_params_[0] = { 100.0f,  1.0f, 0.0f, -20.0f, 2.0f, 5.0f, 50.0f, true };
    band_params_[1] = { 500.0f,  1.0f, 0.0f, -20.0f, 2.0f, 5.0f, 50.0f, true };
    band_params_[2] = { 2000.0f, 1.0f, 0.0f, -20.0f, 2.0f, 5.0f, 50.0f, true };
    band_params_[3] = { 8000.0f, 1.0f, 0.0f, -20.0f, 2.0f, 5.0f, 50.0f, true };

    for (int i = 0; i < MAX_BANDS; ++i) {
        rms_l_[i]               = 0.0f;
        rms_r_[i]               = 0.0f;
        smooth_gain_db_[i]      = 0.0f;
        last_applied_gain_db_[i]= 1e6f;  // force initial coefficient calculation
    }

    for (int i = 0; i < num_bands_; ++i) {
        rebuild_sidechain_filter(i);
        rebuild_eq_filter(i, band_params_[i].static_gain_db);
        last_applied_gain_db_[i] = band_params_[i].static_gain_db;
    }
}

void DynamicEQ::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    for (int i = 0; i < num_bands_; ++i) {
        rebuild_sidechain_filter(i);
        rebuild_eq_filter(i, smooth_gain_db_[i]);
    }
    reset();
}

void DynamicEQ::reset() noexcept {
    for (int i = 0; i < MAX_BANDS; ++i) {
        rms_l_[i] = rms_r_[i] = 0.0f;
        smooth_gain_db_[i] = 0.0f;
        last_applied_gain_db_[i] = 1e6f;
        sc_filter_[i].reset();
        eq_filter_[i].reset();
    }
}

void DynamicEQ::set_num_bands(int n) noexcept {
    num_bands_ = dsp::clamp(n, 1, MAX_BANDS);
}

void DynamicEQ::set_band(int i, DynEQBand b) noexcept {
    if (i < 0 || i >= MAX_BANDS) return;
    band_params_[i] = b;
    rebuild_sidechain_filter(i);
    // EQ filter gets rebuilt on the next process() call when gain changes.
    last_applied_gain_db_[i] = 1e6f;  // force recalculate
}

void DynamicEQ::rebuild_sidechain_filter(int i) noexcept {
    const DynEQBand& b = band_params_[i];
    sc_filter_[i].coeffs = biquad::make_bandpass(b.freq_hz, b.q, sample_rate_);
    sc_filter_[i].reset();
}

void DynamicEQ::rebuild_eq_filter(int i, float gain_db) noexcept {
    const DynEQBand& b = band_params_[i];
    eq_filter_[i].coeffs = biquad::make_peak(b.freq_hz, b.q, gain_db, sample_rate_);
}

void DynamicEQ::process(float* left, float* right, int num_samples) noexcept {
    for (int i = 0; i < num_samples; ++i) {
        float in_l = left[i];
        float in_r = right[i];

        for (int b = 0; b < num_bands_; ++b) {
            if (!band_params_[b].enabled) continue;

            const DynEQBand& bp = band_params_[b];

            // ── Sidechain: bandpass RMS detection ────────────────────────
            float sc_l = sc_filter_[b].process_l(in_l);
            float sc_r = sc_filter_[b].process_r(in_r);

            // RMS follower: square, smooth, sqrt.
            // The squaring+smoothing tracks signal power rather than peak,
            // which responds well to tonal energy changes (preferred for EQ).
            const float rms_attack  = dsp::time_to_coeff(bp.attack_ms,  sample_rate_);
            const float rms_release = dsp::time_to_coeff(bp.release_ms, sample_rate_);
            const float sq_l = sc_l * sc_l + DENORMAL_GUARD;
            const float sq_r = sc_r * sc_r + DENORMAL_GUARD;

            // Use max(L,R) squared power to drive a single envelope per band.
            const float sq = std::max(sq_l, sq_r);

            if (sq > rms_l_[b]) {
                rms_l_[b] = rms_attack  * rms_l_[b] + (1.0f - rms_attack)  * sq;
            } else {
                rms_l_[b] = rms_release * rms_l_[b] + (1.0f - rms_release) * sq;
            }
            const float env_lin = std::sqrt(rms_l_[b]);
            const float env_db  = dsp::linear_to_db(env_lin);

            // ── Dynamic gain computation ──────────────────────────────────
            float gain_reduction_db = 0.0f;
            if (env_db > bp.threshold_db && bp.ratio > 1.0f) {
                // (1/ratio - 1) is negative for ratio > 1 → reduction.
                gain_reduction_db = (1.0f / bp.ratio - 1.0f) * (env_db - bp.threshold_db);
            }

            const float target_gain_db = bp.static_gain_db + gain_reduction_db;

            // One-pole smooth the gain in dB domain so the EQ character
            // changes smoothly rather than jumping coefficients abruptly.
            const float atk = dsp::time_to_coeff(bp.attack_ms,  sample_rate_);
            const float rel = dsp::time_to_coeff(bp.release_ms, sample_rate_);

            if (target_gain_db < smooth_gain_db_[b]) {
                smooth_gain_db_[b] = atk * smooth_gain_db_[b] + (1.0f - atk) * target_gain_db;
            } else {
                smooth_gain_db_[b] = rel * smooth_gain_db_[b] + (1.0f - rel) * target_gain_db;
            }

            // ── Coefficient update (only when gain changed significantly) ─
            // Updating biquad coefficients every sample is CPU-intensive and
            // causes small discontinuities.  A threshold of 0.01 dB corresponds
            // to ~0.1% amplitude change — below audibility.
            if (std::abs(smooth_gain_db_[b] - last_applied_gain_db_[b]) > 0.01f) {
                rebuild_eq_filter(b, smooth_gain_db_[b]);
                last_applied_gain_db_[b] = smooth_gain_db_[b];
            }

            // ── Apply peak EQ biquad to the main signal ───────────────────
            in_l = eq_filter_[b].process_l(in_l);
            in_r = eq_filter_[b].process_r(in_r);
        }

        left[i]  = in_l;
        right[i] = in_r;
    }
}
