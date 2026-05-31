#include "MultibandCompressor.h"
#include <cmath>
#include <algorithm>
#include <cstring>

MultibandCompressor::MultibandCompressor(float sample_rate)
    : sample_rate_(sample_rate)
{
    xover_hz_[0] = 200.0f;
    xover_hz_[1] = 2000.0f;
    xover_hz_[2] = 8000.0f;

    for (int b = 0; b < NUM_BANDS; ++b) {
        gain_[b] = 1.0f;
        env_[b]  = 0.0f;
    }

    rebuild_filters();
}

void MultibandCompressor::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    rebuild_filters();
    reset();
}

void MultibandCompressor::reset() noexcept {
    for (int x = 0; x < NUM_XOVERS; ++x) {
        lp_[x].reset();
        hp_[x].reset();
    }
    for (int b = 0; b < NUM_BANDS; ++b) {
        env_[b]  = 0.0f;
        gain_[b] = 1.0f;
    }
}

void MultibandCompressor::set_crossover(int index, float hz) noexcept {
    if (index < 0 || index >= NUM_XOVERS) return;
    xover_hz_[index] = hz;
    rebuild_filters();
}

void MultibandCompressor::set_band(int index, BandConfig cfg) noexcept {
    if (index < 0 || index >= NUM_BANDS) return;
    bands_[index] = cfg;
}

void MultibandCompressor::rebuild_filters() noexcept {
    for (int x = 0; x < NUM_XOVERS; ++x) {
        lp_[x].make_lowpass (xover_hz_[x], sample_rate_);
        hp_[x].make_highpass(xover_hz_[x], sample_rate_);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Per-band gain computer + smoother
// ─────────────────────────────────────────────────────────────────────────────

void MultibandCompressor::compute_band_gain(int band, float env_val, float& gain_out) noexcept {
    const BandConfig& cfg = bands_[band];

    const float thresh_lin  = dsp::db_to_linear(cfg.threshold_db);
    const float attack_coeff  = dsp::time_to_coeff(cfg.attack_ms,  sample_rate_);
    const float release_coeff = dsp::time_to_coeff(cfg.release_ms, sample_rate_);
    const float makeup_lin    = dsp::db_to_linear(cfg.makeup_db);

    // Hard-knee gain computer: above threshold, reduce by ratio.
    float desired_gain = 1.0f;
    if (env_val > thresh_lin && cfg.ratio > 1.0f) {
        const float env_db   = dsp::linear_to_db(env_val);
        const float thr_db   = cfg.threshold_db;
        const float over_db  = env_db - thr_db;
        const float gr_db    = over_db * (1.0f - 1.0f / cfg.ratio);
        desired_gain = dsp::db_to_linear(-gr_db);
    }
    desired_gain *= makeup_lin;

    // Smooth toward desired_gain using attack (going down) / release (going up).
    if (desired_gain < gain_out) {
        gain_out = attack_coeff  * gain_out + (1.0f - attack_coeff)  * desired_gain;
    } else {
        gain_out = release_coeff * gain_out + (1.0f - release_coeff) * desired_gain;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Band splitting using the cascaded LR4 topology.
//
// The signal tree is:
//   input → LP[0]        → band_0
//         → HP[0] → LP[1]  → band_1
//         → HP[0] → HP[1] → LP[2]  → band_2
//         → HP[0] → HP[1] → HP[2]  → band_3
//
// To minimise filter cascade order and memory use we compute it in one pass:
//   tmp_hp0  = hp[0](sample)
//   band_0   = lp[0](sample)
//   tmp_hp1  = hp[1](tmp_hp0)
//   band_1   = lp[1](tmp_hp0)
//   tmp_hp2  = hp[2](tmp_hp1)
//   band_2   = lp[2](tmp_hp1)
//   band_3   = tmp_hp2
// ─────────────────────────────────────────────────────────────────────────────

void MultibandCompressor::process(float* left, float* right, int num_samples) noexcept {
    // Check if any band is soloed — if so, only that band passes.
    bool any_soloed = false;
    for (int b = 0; b < NUM_BANDS; ++b) {
        if (bands_[b].soloed) { any_soloed = true; break; }
    }

    // ── Band splitting ───────────────────────────────────────────────────────
    for (int i = 0; i < num_samples; ++i) {
        const float in_l = left[i];
        const float in_r = right[i];

        // L channel band splitting
        float hp0_l = hp_[0].process_l(in_l);
        float band0_l = lp_[0].process_l(in_l);
        float hp1_l = hp_[1].process_l(hp0_l);
        float band1_l = lp_[1].process_l(hp0_l);
        float hp2_l = hp_[2].process_l(hp1_l);
        float band2_l = lp_[2].process_l(hp1_l);
        float band3_l = hp2_l;

        // R channel band splitting
        float hp0_r = hp_[0].process_r(in_r);
        float band0_r = lp_[0].process_r(in_r);
        float hp1_r = hp_[1].process_r(hp0_r);
        float band1_r = lp_[1].process_r(hp0_r);
        float hp2_r = hp_[2].process_r(hp1_r);
        float band2_r = lp_[2].process_r(hp1_r);
        float band3_r = hp2_r;

        band_l_[0][i] = band0_l;  band_r_[0][i] = band0_r;
        band_l_[1][i] = band1_l;  band_r_[1][i] = band1_r;
        band_l_[2][i] = band2_l;  band_r_[2][i] = band2_r;
        band_l_[3][i] = band3_l;  band_r_[3][i] = band3_r;
    }

    // ── Per-band compression and gain application ────────────────────────────
    for (int b = 0; b < NUM_BANDS; ++b) {
        const float attack_coeff  = dsp::time_to_coeff(bands_[b].attack_ms,  sample_rate_);
        const float release_coeff = dsp::time_to_coeff(bands_[b].release_ms, sample_rate_);
        const float thresh_lin    = dsp::db_to_linear(bands_[b].threshold_db);
        const float makeup_lin    = dsp::db_to_linear(bands_[b].makeup_db);

        for (int i = 0; i < num_samples; ++i) {
            const float bl = band_l_[b][i];
            const float br = band_r_[b][i];

            // Mono-sum peak envelope for gain computation.
            // Using max(|L|,|R|) ensures the louder channel drives the detector
            // and avoids over-compressing on purely mono material.
            const float peak = std::max(std::abs(bl), std::abs(br)) + DENORMAL_GUARD;

            // One-pole peak follower: fast attack, slow release.
            if (peak > env_[b]) {
                env_[b] = attack_coeff  * env_[b] + (1.0f - attack_coeff)  * peak;
            } else {
                env_[b] = release_coeff * env_[b] + (1.0f - release_coeff) * peak;
            }

            // Gain computer.
            float desired_gain = 1.0f;
            if (env_[b] > thresh_lin && bands_[b].ratio > 1.0f) {
                const float env_db  = dsp::linear_to_db(env_[b]);
                const float over_db = env_db - bands_[b].threshold_db;
                const float gr_db   = over_db * (1.0f - 1.0f / bands_[b].ratio);
                desired_gain = dsp::db_to_linear(-gr_db) * makeup_lin;
            } else {
                desired_gain = makeup_lin;
            }

            if (desired_gain < gain_[b]) {
                gain_[b] = attack_coeff  * gain_[b] + (1.0f - attack_coeff)  * desired_gain;
            } else {
                gain_[b] = release_coeff * gain_[b] + (1.0f - release_coeff) * desired_gain;
            }

            band_l_[b][i] *= gain_[b];
            band_r_[b][i] *= gain_[b];
        }
    }

    // ── Reconstruct output ──────────────────────────────────────────────────
    for (int i = 0; i < num_samples; ++i) {
        float out_l = 0.0f, out_r = 0.0f;
        for (int b = 0; b < NUM_BANDS; ++b) {
            bool active = !bands_[b].muted && (!any_soloed || bands_[b].soloed);
            if (active) {
                out_l += band_l_[b][i];
                out_r += band_r_[b][i];
            }
        }
        left[i]  = out_l;
        right[i] = out_r;
    }
}
