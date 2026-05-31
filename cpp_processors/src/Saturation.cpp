#include "Saturation.h"
#include <algorithm>
#include <cmath>

// ─────────────────────────────────────────────────────────────────────────────
// Construction / preparation
// ─────────────────────────────────────────────────────────────────────────────

Saturation::Saturation(float sample_rate)
    : sample_rate_(sample_rate)
    , mode_(0)            // TUBE by default
    , drive_linear_(1.0f) // 0 dB drive = no change
    , comp_linear_(1.0f)
    , output_linear_(1.0f)
    , bias_(0.2f)         // mild asymmetry gives gentle 2nd-harmonic colour
    , drive_db_(0.0f)
    , harm2_env_(0.0f), harm3_env_(0.0f), harm4_env_(0.0f)
{
    prepare(sample_rate);
}

void Saturation::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    // Envelope follower: ~5ms attack, ~300ms release.
    env_atk_ = dsp::time_to_coeff(5.0f, sample_rate);
    env_rel_ = dsp::time_to_coeff(300.0f, sample_rate);
    _rebuild_filters();
    _recompute_compensation();
    reset();
}

void Saturation::reset() noexcept {
    tape_lp_.reset();
    bp2_.reset();
    bp3_.reset();
    bp4_.reset();
    harm2_env_ = harm3_env_ = harm4_env_ = 0.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void Saturation::set_mode(int mode) noexcept {
    mode_ = (mode == 1) ? 1 : 0;  // clamp to valid range
}

void Saturation::set_drive(float db) noexcept {
    drive_db_    = dsp::clamp(db, 0.0f, 40.0f);
    drive_linear_ = dsp::db_to_linear(drive_db_);
    // Tape LP cutoff depends on drive: at 0 dB → 20 kHz, at 40 dB → ~2 kHz.
    _rebuild_filters();
    _recompute_compensation();
}

void Saturation::set_output(float db) noexcept {
    output_linear_ = dsp::db_to_linear(dsp::clamp(db, -24.0f, 12.0f));
}

void Saturation::set_bias(float bias) noexcept {
    bias_ = dsp::clamp(bias, 0.0f, 1.0f);
    _recompute_compensation();
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

void Saturation::_rebuild_filters() noexcept {
    // Tape LP cutoff falls with drive: 20000 / (1 + drive_db * 0.45).
    // At 0 dB → 20 kHz (transparent), at 40 dB → ~1.1 kHz (heavy roll-off).
    const float lp_hz = std::max(800.0f, 20000.0f / (1.0f + drive_db_ * 0.45f));
    tape_lp_.coeffs = biquad::make_lowpass(lp_hz, 0.70711f, sample_rate_);

    // Fixed harmonic-analysis bandpass filters (Q=4 for reasonable selectivity).
    bp2_.coeffs = biquad::make_bandpass(880.0f,  4.0f, sample_rate_);
    bp3_.coeffs = biquad::make_bandpass(1320.0f, 4.0f, sample_rate_);
    bp4_.coeffs = biquad::make_bandpass(1760.0f, 4.0f, sample_rate_);
}

void Saturation::_recompute_compensation() noexcept {
    // Tube mode: the small-signal gain of f(x) = tanh(d*(x+b)) - tanh(d*b) is:
    //   f'(0) = d * sech²(d*b) = d * (1 - tanh²(d*b))
    // Compensation = 1 / f'(0), so unity-gain passes small signals unchanged.
    const float th_bias = std::tanh(drive_linear_ * bias_);
    const float sech2   = 1.0f - th_bias * th_bias;   // sech²(d*b)
    const float g       = drive_linear_ * sech2;
    comp_linear_ = (g > 1e-6f) ? (1.0f / g) : 1.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Main processing loop
// ─────────────────────────────────────────────────────────────────────────────

void Saturation::process(float* left, float* right, int num_samples) noexcept {
    for (int i = 0; i < num_samples; ++i) {
        float l = left[i];
        float r = right[i];

        // ── Apply saturation waveshaper ───────────────────────────────────────
        float out_l, out_r;
        if (mode_ == 1) {
            // TAPE: symmetric tanh normalised to f(1)=1, then LP filter.
            out_l = _tape_shape(l, drive_linear_);
            out_r = _tape_shape(r, drive_linear_);
            out_l = tape_lp_.process_l(out_l);
            out_r = tape_lp_.process_r(out_r);
        } else {
            // TUBE: biased tanh with auto gain compensation.
            out_l = _tube_shape(l);
            out_r = _tube_shape(r);
        }

        // ── Output trim ───────────────────────────────────────────────────────
        out_l *= output_linear_;
        out_r *= output_linear_;

        left[i]  = out_l;
        right[i] = out_r;

        // ── Harmonic content metering ─────────────────────────────────────────
        // Run the output through three narrow BPs and apply leaky envelope.
        // We mix L+R into a mono signal for the meter to save filter instances.
        const float mono_out = (out_l + out_r) * 0.5f;

        const float b2 = std::abs(bp2_.process_l(mono_out));
        const float b3 = std::abs(bp3_.process_l(mono_out));
        const float b4 = std::abs(bp4_.process_l(mono_out));

        // Leaky peak-hold: attack_coeff_ when rising, release_coeff_ when falling.
        harm2_env_ = (b2 > harm2_env_) ? (env_atk_ * harm2_env_ + (1.0f - env_atk_) * b2)
                                        : (env_rel_ * harm2_env_);
        harm3_env_ = (b3 > harm3_env_) ? (env_atk_ * harm3_env_ + (1.0f - env_atk_) * b3)
                                        : (env_rel_ * harm3_env_);
        harm4_env_ = (b4 > harm4_env_) ? (env_atk_ * harm4_env_ + (1.0f - env_atk_) * b4)
                                        : (env_rel_ * harm4_env_);
    }
}
