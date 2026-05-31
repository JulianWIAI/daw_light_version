#include "Exciter.h"
#include <algorithm>
#include <cmath>

// ─────────────────────────────────────────────────────────────────────────────
// Construction / preparation
// ─────────────────────────────────────────────────────────────────────────────

Exciter::Exciter(float sample_rate)
    : sample_rate_(sample_rate)
    , crossover_hz_(6000.0f)
    , harmonics_(0.5f)
    , air_linear_(1.0f)  // 0 dB air boost by default
    , wet_(0.5f), dry_(0.5f)
{
    prepare(sample_rate);
}

void Exciter::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    _rebuild_crossover();
    _rebuild_air_shelf();
    reset();
}

void Exciter::reset() noexcept {
    lp_cross_.reset();
    hp_cross_.reset();
    air_shelf_.reset();
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void Exciter::set_crossover_hz(float hz) noexcept {
    crossover_hz_ = dsp::clamp(hz, 3000.0f, 12000.0f);
    _rebuild_crossover();
}

void Exciter::set_harmonics(float amount) noexcept {
    harmonics_ = dsp::clamp(amount, 0.0f, 1.0f);
}

void Exciter::set_air(float db) noexcept {
    // Air is a high-shelf: rebuild the shelf at the new gain.
    // Store as linear for the recombine step, but the shelf is a biquad.
    const float clamped = dsp::clamp(db, 0.0f, 12.0f);
    air_linear_ = dsp::db_to_linear(clamped);  // kept for reference; shelf does the work
    _rebuild_air_shelf();
}

void Exciter::set_wet(float w) noexcept {
    wet_ = dsp::clamp(w, 0.0f, 1.0f);
    dry_ = 1.0f - wet_;
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

void Exciter::_rebuild_crossover() noexcept {
    // LR4 crossover: two cascaded Butterworth biquads at the same frequency.
    lp_cross_.make_lowpass(crossover_hz_, sample_rate_);
    hp_cross_.make_highpass(crossover_hz_, sample_rate_);
}

void Exciter::_rebuild_air_shelf() noexcept {
    // High-shelf at 8 kHz, gain = (set_air value in dB).
    // air_linear_ was set in set_air() — convert back to dB for the shelf.
    const float shelf_db = dsp::linear_to_db(air_linear_);
    air_shelf_.coeffs = biquad::make_high_shelf(8000.0f, shelf_db, sample_rate_);
}

// ─────────────────────────────────────────────────────────────────────────────
// Main processing loop
// ─────────────────────────────────────────────────────────────────────────────

void Exciter::process(float* left, float* right, int num_samples) noexcept {
    for (int i = 0; i < num_samples; ++i) {
        const float dry_l = left[i];
        const float dry_r = right[i];

        // ── LR4 crossover split ───────────────────────────────────────────────
        // Low band: unprocessed, recombined directly.
        const float lf_l = lp_cross_.process_l(dry_l);
        const float lf_r = lp_cross_.process_r(dry_r);

        // High band: sent to the exciter stage.
        float hf_l = hp_cross_.process_l(dry_l);
        float hf_r = hp_cross_.process_r(dry_r);

        // ── Harmonic generation on HF only ────────────────────────────────────
        // _excite() is a normalised tanh shaper; at harmonics_=0 it is linear.
        hf_l = _excite(hf_l, harmonics_);
        hf_r = _excite(hf_r, harmonics_);

        // ── Air shelf boost (acts on the excited HF) ──────────────────────────
        hf_l = air_shelf_.process_l(hf_l);
        hf_r = air_shelf_.process_r(hf_r);

        // ── Recombine LF + excited HF, then blend with dry original ───────────
        const float wet_l = lf_l + hf_l;
        const float wet_r = lf_r + hf_r;

        left[i]  = dry_ * dry_l + wet_ * wet_l;
        right[i] = dry_ * dry_r + wet_ * wet_r;
    }
}
