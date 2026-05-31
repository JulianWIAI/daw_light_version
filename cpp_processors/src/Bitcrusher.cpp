#include "Bitcrusher.h"
#include <cmath>
#include <algorithm>

// ─────────────────────────────────────────────────────────────────────────────
// Construction / preparation
// ─────────────────────────────────────────────────────────────────────────────

Bitcrusher::Bitcrusher(float sample_rate)
    : host_sr_(sample_rate)
    , bits_(16.0f)
    , resample_hz_(sample_rate)  // at host rate by default = no decimation
    , wet_(1.0f), dry_(0.0f)
    , dither_(false)
    , hold_l_(0.0f), hold_r_(0.0f)
    , phase_(0.0f)
    , rng_(0xDEADBEEFu)
    , quant_step_(1.0f)
{
    prepare(sample_rate);
}

void Bitcrusher::prepare(float sample_rate) noexcept {
    host_sr_     = sample_rate;
    resample_hz_ = std::min(resample_hz_, sample_rate);
    _update_phase_inc();
    _update_quant_step();
    reset();
}

void Bitcrusher::reset() noexcept {
    hold_l_ = hold_r_ = 0.0f;
    phase_  = 0.0f;
    rng_    = 0xDEADBEEFu;
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void Bitcrusher::set_bit_depth(float bits) noexcept {
    bits_ = dsp::clamp(bits, 1.0f, 24.0f);
    _update_quant_step();
}

void Bitcrusher::set_sample_rate_hz(float hz) noexcept {
    resample_hz_ = dsp::clamp(hz, 500.0f, host_sr_);
    _update_phase_inc();
}

void Bitcrusher::set_wet(float w) noexcept {
    wet_ = dsp::clamp(w, 0.0f, 1.0f);
    dry_ = 1.0f - wet_;
}

void Bitcrusher::set_dither(bool enabled) noexcept {
    dither_ = enabled;
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

void Bitcrusher::_update_phase_inc() noexcept {
    // Each host sample advances the phase by this fraction.
    // When phase >= 1.0 we latch a new sample and subtract 1.0.
    phase_inc_ = (host_sr_ > 0.0f) ? (resample_hz_ / host_sr_) : 1.0f;
}

void Bitcrusher::_update_quant_step() noexcept {
    // levels = 2^(bits-1) - 1 (signed integer max, e.g. 16-bit → 32767).
    // quant_step = 1 / levels — the size of one quantisation step.
    const float levels = std::pow(2.0f, bits_ - 1.0f) - 1.0f;
    quant_step_ = (levels > 0.0f) ? (1.0f / levels) : 1.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Main processing loop
// ─────────────────────────────────────────────────────────────────────────────

void Bitcrusher::process(float* left, float* right, int num_samples) noexcept {
    for (int i = 0; i < num_samples; ++i) {
        const float dry_l = left[i];
        const float dry_r = right[i];

        // ── Sample-rate decimation (sample-and-hold) ──────────────────────────
        // Advance the phase accumulator.  When it overflows 1.0, latch new input.
        phase_ += phase_inc_;
        if (phase_ >= 1.0f) {
            phase_ -= 1.0f;
            hold_l_ = dry_l;
            hold_r_ = dry_r;
        }
        // Output is the held (decimated) sample.

        // ── Bit-depth quantisation ────────────────────────────────────────────
        const float q_l = _quantise(hold_l_);
        const float q_r = _quantise(hold_r_);

        // ── Wet / dry mix ─────────────────────────────────────────────────────
        left[i]  = dry_ * dry_l + wet_ * q_l;
        right[i] = dry_ * dry_r + wet_ * q_r;
    }
}
