#include "StereoImager.h"
#include <algorithm>
#include <cmath>

// √2 reciprocal for the M/S encode / decode normalisation.
static constexpr float INV_SQRT2 = 0.70710678118f;

// ─────────────────────────────────────────────────────────────────────────────
// Construction / preparation
// ─────────────────────────────────────────────────────────────────────────────

StereoImager::StereoImager(float sample_rate)
    : sample_rate_(sample_rate)
    , width_(1.0f)          // 1.0 = unity (no change)
    , lf_mono_lock_(true)   // enabled by default to protect sub-bass
    , crossover_hz_(200.0f)
    , correlation_(0.0f)
{
    prepare(sample_rate);
}

void StereoImager::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    _rebuild_crossover();
    reset();
}

void StereoImager::reset() noexcept {
    lp_filter_.reset();
    hp_filter_.reset();
    correlation_ = 0.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void StereoImager::set_width(float w) noexcept {
    width_ = std::max(0.0f, std::min(w, 2.0f));
}

void StereoImager::set_lf_mono_lock(bool enabled) noexcept {
    lf_mono_lock_ = enabled;
}

void StereoImager::set_crossover_hz(float hz) noexcept {
    crossover_hz_ = std::max(20.0f, std::min(hz, sample_rate_ * 0.4f));
    _rebuild_crossover();
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

void StereoImager::_rebuild_crossover() noexcept {
    lp_filter_.make_lowpass(crossover_hz_, sample_rate_);
    hp_filter_.make_highpass(crossover_hz_, sample_rate_);
}

// ─────────────────────────────────────────────────────────────────────────────
// Main processing loop
// ─────────────────────────────────────────────────────────────────────────────

void StereoImager::process(float* left, float* right, int num_samples) noexcept {
    // Accumulators for the Pearson correlation computed on the OUTPUT signal.
    float sum_lr = 0.0f, sum_ll = 0.0f, sum_rr = 0.0f;

    for (int i = 0; i < num_samples; ++i) {
        float l = left[i];
        float r = right[i];

        float hf_l, hf_r;
        float lf_l = 0.0f, lf_r = 0.0f;  // only used when LF mono lock is active

        if (lf_mono_lock_) {
            // ── Split into LF and HF via LR4 crossover ────────────────────────
            lf_l = lp_filter_.process_l(l);
            lf_r = lp_filter_.process_r(r);
            hf_l = hp_filter_.process_l(l);
            hf_r = hp_filter_.process_r(r);

            // Sum LF to mono to prevent sub-bass phase cancellation.
            const float mono_lf = (lf_l + lf_r) * 0.5f;
            lf_l = lf_r = mono_lf;
        } else {
            // No LF mono lock: apply width to the full-band signal.
            hf_l = l;
            hf_r = r;
        }

        // ── M/S width processing on HF band (or full signal) ─────────────────
        // Encode to Mid/Side.
        const float m = (hf_l + hf_r) * INV_SQRT2;
        const float s = (hf_l - hf_r) * INV_SQRT2;

        // Scale Side channel by width factor.
        const float s_out = s * width_;

        // Decode back to L/R.
        hf_l = (m + s_out) * INV_SQRT2;
        hf_r = (m - s_out) * INV_SQRT2;

        // ── Recombine LF + HF ─────────────────────────────────────────────────
        left[i]  = lf_l + hf_l;
        right[i] = lf_r + hf_r;

        // ── Accumulate correlation statistics ─────────────────────────────────
        sum_lr += left[i] * right[i];
        sum_ll += left[i] * left[i];
        sum_rr += right[i] * right[i];
    }

    // ── Pearson phase correlation for this block ──────────────────────────────
    // corr = Σ(L·R) / √(Σ(L²) · Σ(R²))
    // Range: −1 (anti-phase) to +1 (identical).
    const float denom = std::sqrt(sum_ll * sum_rr);
    const float new_corr = (denom > 1e-12f) ? (sum_lr / denom) : 0.0f;

    // Smooth with a leaky integrator (~100 ms time constant at 44100 Hz, 512 block).
    // Coefficient computed once would be more accurate, but 0.9 is a good default.
    correlation_ = 0.90f * correlation_ + 0.10f * new_corr;
}
