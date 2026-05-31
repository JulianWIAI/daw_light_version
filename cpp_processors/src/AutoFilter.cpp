/*
 * AutoFilter.cpp  --  Resonant multi-mode filter + LFO + envelope follower
 * =========================================================================
 * See AutoFilter.h for the full algorithm description.
 *
 * Biquad formula source: "Cookbook formulae for audio equalizer biquad
 * filter coefficients" by Robert Bristow-Johnson (RBJ cookbook).
 */

#include "AutoFilter.h"
#include <cstring>
#include <cmath>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ─────────────────────────────────────────────────────────────────────────────
// Construction & lifecycle
// ─────────────────────────────────────────────────────────────────────────────

AutoFilter::AutoFilter(float sample_rate) {
    prepare(sample_rate);
}

void AutoFilter::prepare(float sample_rate) {
    sample_rate_ = (sample_rate > 0.f) ? sample_rate : 44100.f;
    _recompute_env_coeffs();
    _recompute_lfo_inc();
    _recompute_filter(cutoff_hz_);
    reset();
}

void AutoFilter::reset() {
    filt_l_ = {};
    filt_r_ = {};
    lfo_phase_ = 0.f;
    env_l_ = 0.f;
    env_r_ = 0.f;
    coeff_ctr_ = 0;
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void AutoFilter::set_filter_mode(int mode) {
    filter_mode_ = static_cast<FilterMode>(mode);
    _recompute_filter(cutoff_hz_);
}

void AutoFilter::set_cutoff_hz(float hz) {
    cutoff_hz_ = std::max(20.f, std::min(hz, sample_rate_ * 0.49f));
    _recompute_filter(cutoff_hz_);
}

void AutoFilter::set_resonance(float q) {
    resonance_ = std::max(0.5f, std::min(12.f, q));
    _recompute_filter(cutoff_hz_);
}

void AutoFilter::set_drive(float drive) {
    drive_ = std::max(0.f, std::min(1.f, drive));
}

void AutoFilter::set_mod_source(int src) {
    mod_src_ = static_cast<ModSource>(src);
}

void AutoFilter::set_lfo_rate_hz(float hz) {
    lfo_rate_ = std::max(0.01f, std::min(20.f, hz));
    _recompute_lfo_inc();
}

void AutoFilter::set_lfo_depth(float depth) {
    lfo_depth_ = std::max(0.f, std::min(1.f, depth));
}

void AutoFilter::set_lfo_shape(int shape) {
    lfo_shape_ = static_cast<LfoShape>(shape);
}

void AutoFilter::set_env_attack_ms(float ms) {
    env_attack_ms_ = std::max(0.1f, std::min(500.f, ms));
    _recompute_env_coeffs();
}

void AutoFilter::set_env_release_ms(float ms) {
    env_release_ms_ = std::max(1.f, std::min(5000.f, ms));
    _recompute_env_coeffs();
}

void AutoFilter::set_env_depth(float depth) {
    env_depth_ = std::max(0.f, std::min(1.f, depth));
}

void AutoFilter::set_wet(float wet) {
    wet_ = std::max(0.f, std::min(1.f, wet));
}

// ─────────────────────────────────────────────────────────────────────────────
// Private: recompute biquad coefficients (RBJ cookbook)
// ─────────────────────────────────────────────────────────────────────────────

void AutoFilter::_recompute_filter(float cutoff_hz) {
    /* Clamp cutoff to a safe range. */
    float f  = std::max(20.f, std::min(cutoff_hz, sample_rate_ * 0.49f));
    float w0 = 2.f * (float)M_PI * f / sample_rate_;
    float cw = std::cosf(w0);
    float sw = std::sinf(w0);
    float Q  = resonance_;
    float alpha = sw / (2.f * Q);

    float b0, b1, b2, a0, a1, a2;

    switch (filter_mode_) {
        case FilterMode::HIGHPASS:
            /* RBJ HPF */
            b0 =  (1.f + cw) * 0.5f;
            b1 = -(1.f + cw);
            b2 =  (1.f + cw) * 0.5f;
            a0 =   1.f + alpha;
            a1 =  -2.f * cw;
            a2 =   1.f - alpha;
            break;

        case FilterMode::BANDPASS:
            /* RBJ BPF (constant 0 dB peak gain) */
            b0 =  alpha;
            b1 =  0.f;
            b2 = -alpha;
            a0 =  1.f + alpha;
            a1 = -2.f * cw;
            a2 =  1.f - alpha;
            break;

        default: /* LOWPASS */
            /* RBJ LPF */
            b0 = (1.f - cw) * 0.5f;
            b1 =  1.f - cw;
            b2 = (1.f - cw) * 0.5f;
            a0 =  1.f + alpha;
            a1 = -2.f * cw;
            a2 =  1.f - alpha;
            break;
    }

    /* Normalise by a0 and store. */
    float inv_a0 = 1.f / a0;
    coeffs_.b0 = b0 * inv_a0;
    coeffs_.b1 = b1 * inv_a0;
    coeffs_.b2 = b2 * inv_a0;
    coeffs_.a1 = a1 * inv_a0;
    coeffs_.a2 = a2 * inv_a0;
}

// ─────────────────────────────────────────────────────────────────────────────
// Private: recompute envelope time constants
// ─────────────────────────────────────────────────────────────────────────────

void AutoFilter::_recompute_env_coeffs() {
    /*
     * First-order IIR coefficients:  c = 1 - exp(-1 / (tc * sr))
     * Larger tc → slower, smaller coefficient.
     */
    auto tc_to_coeff = [&](float ms) {
        float tc = ms * 0.001f * sample_rate_;
        return 1.f - std::expf(-1.f / std::max(tc, 1.f));
    };
    env_att_c_ = tc_to_coeff(env_attack_ms_);
    env_rel_c_ = tc_to_coeff(env_release_ms_);
}

// ─────────────────────────────────────────────────────────────────────────────
// Private: recompute LFO phase increment
// ─────────────────────────────────────────────────────────────────────────────

void AutoFilter::_recompute_lfo_inc() {
    lfo_phase_inc_ = lfo_rate_ / sample_rate_;
}

// ─────────────────────────────────────────────────────────────────────────────
// Private: advance LFO one sample
// ─────────────────────────────────────────────────────────────────────────────

float AutoFilter::_lfo_tick() noexcept {
    float phase = lfo_phase_;

    float value = 0.f;
    switch (lfo_shape_) {
        case LfoShape::SINE:
            value = std::sinf(2.f * (float)M_PI * phase);
            break;
        case LfoShape::TRIANGLE:
            /* Triangle: 0→1 (first half), 1→-1 (second half). */
            value = (phase < 0.5f) ? (4.f * phase - 1.f)
                                   : (3.f - 4.f * phase);
            break;
        case LfoShape::SQUARE:
            value = (phase < 0.5f) ? 1.f : -1.f;
            break;
        case LfoShape::SAWUP:
            /* Saw-up: -1 → +1 per cycle. */
            value = 2.f * phase - 1.f;
            break;
    }

    /* Advance phase and wrap. */
    lfo_phase_ += lfo_phase_inc_;
    if (lfo_phase_ >= 1.f) lfo_phase_ -= 1.f;

    return value;   // in [-1, +1]
}

// ─────────────────────────────────────────────────────────────────────────────
// Private: TDF-II biquad one-sample tick
// ─────────────────────────────────────────────────────────────────────────────

inline float AutoFilter::_biquad_tick(BiquadState& s, float x) const noexcept {
    /*
     * Transposed Direct Form II:
     *   y   = b0*x + s1
     *   s1' = b1*x - a1*y + s2
     *   s2' = b2*x - a2*y
     */
    float y  = coeffs_.b0 * x + s.s1;
    s.s1 = coeffs_.b1 * x - coeffs_.a1 * y + s.s2;
    s.s2 = coeffs_.b2 * x - coeffs_.a2 * y;
    return y;
}

// ─────────────────────────────────────────────────────────────────────────────
// Private: envelope follower one-sample tick
// ─────────────────────────────────────────────────────────────────────────────

inline float AutoFilter::_env_tick(float& env, float x) noexcept {
    float mag = std::abs(x);
    /* Attack when signal rises; release when it falls. */
    float coeff = (mag > env) ? env_att_c_ : env_rel_c_;
    env += coeff * (mag - env);
    return env;   // in [0, 1] for normalised input
}

// ─────────────────────────────────────────────────────────────────────────────
// Main process loop
// ─────────────────────────────────────────────────────────────────────────────

void AutoFilter::process(float* left, float* right, int num_samples) {

    for (int i = 0; i < num_samples; ++i) {

        /* ── 1. Get modulation values ── */
        float lfo_val = 0.f;
        float env_val = 0.f;

        bool use_lfo = (mod_src_ == ModSource::LFO  || mod_src_ == ModSource::BOTH);
        bool use_env = (mod_src_ == ModSource::ENVELOPE || mod_src_ == ModSource::BOTH);

        if (use_lfo)
            lfo_val = _lfo_tick();       // advances phase
        else
            _lfo_tick();                 // still advance LFO even if unused

        if (use_env) {
            float mono = (left[i] + right[i]) * 0.5f;
            env_val = std::max(_env_tick(env_l_, left[i]),
                                _env_tick(env_r_, right[i]));
        }

        /* ── 2. Compute effective cutoff (log-scaled modulation) ── */
        /* mod_oct = signed octave offset from base cutoff. */
        float mod_oct = 0.f;
        if (use_lfo)
            mod_oct += lfo_val * lfo_depth_ * LFO_OCTAVES;
        if (use_env)
            mod_oct += env_val * env_depth_ * ENV_OCTAVES;

        /* ── 3. Recompute filter coefficients every COEFF_UPDATE_INTERVAL ── */
        if (++coeff_ctr_ >= COEFF_UPDATE_INTERVAL) {
            coeff_ctr_ = 0;
            float mod_cutoff = cutoff_hz_ * std::pow(2.f, mod_oct);
            mod_cutoff = std::max(20.f, std::min(mod_cutoff, sample_rate_ * 0.49f));
            _recompute_filter(mod_cutoff);
        }

        /* ── 4. Pre-filter soft-clip drive (tanh saturation) ── */
        float xl = left[i];
        float xr = right[i];
        if (drive_ > 0.f) {
            float d = 1.f + drive_ * 9.f;   // drive range: 1..10
            xl = std::tanhf(xl * d) / std::tanhf(d);
            xr = std::tanhf(xr * d) / std::tanhf(d);
        }

        /* ── 5. Apply biquad filter ── */
        float yl = _biquad_tick(filt_l_, xl);
        float yr = _biquad_tick(filt_r_, xr);

        /* ── 6. Dry / wet blend and write output ── */
        left[i]  = left[i]  + wet_ * (yl - left[i]);
        right[i] = right[i] + wet_ * (yr - right[i]);
    }
}
