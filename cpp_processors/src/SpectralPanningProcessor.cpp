/*
 * SpectralPanningProcessor.cpp  --  Per-Track Spectral Panning Processor
 * ========================================================================
 * See SpectralPanningProcessor.h for the full algorithm description.
 */

#include "SpectralPanningProcessor.h"
#include <cmath>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// Construction / prepare / set_params
// ---------------------------------------------------------------------------

SpectralPanningProcessor::SpectralPanningProcessor(float sample_rate) noexcept
    : sample_rate_(sample_rate > 0.0f ? sample_rate : 44100.0f)
    , params_()
    , analyzer_(sample_rate_)
{}

void SpectralPanningProcessor::prepare(float sample_rate) noexcept
{
    sample_rate_ = sample_rate > 0.0f ? sample_rate : 44100.0f;
    analyzer_.prepare(sample_rate_);
    // Remove stale manager state so the new sample rate takes effect cleanly.
    SpectralMaskingManager::instance().remove_group(params_.group_id);
}

void SpectralPanningProcessor::set_params(const Params& p) noexcept
{
    // If the group ID changes, clean up the old group entry first.
    if (p.group_id != params_.group_id) {
        SpectralMaskingManager::instance().remove_group(params_.group_id);
    }
    params_ = p;
}

void SpectralPanningProcessor::reset() noexcept
{
    analyzer_.reset();
    SpectralMaskingManager::instance().remove_group(params_.group_id);
}

// ---------------------------------------------------------------------------
// get_current_pan
// ---------------------------------------------------------------------------

float SpectralPanningProcessor::get_current_pan() const noexcept
{
    return SpectralMaskingManager::instance().get_pan(
        params_.group_id, params_.slot
    );
}

// ---------------------------------------------------------------------------
// process()
// ---------------------------------------------------------------------------

void SpectralPanningProcessor::process(float* left, float* right,
                                        int n_frames) noexcept
{
    if (n_frames <= 0) return;

    // ── Step 1: spectral analysis (runs FFT every HOP_SIZE samples) ──────────
    analyzer_.push_block(left, right, n_frames);

    // ── Step 2: push centroid to shared manager, get back smoothed pan ────────
    const float centroid_hz = analyzer_.get_centroid();
    const float dt          = static_cast<float>(n_frames) / sample_rate_;

    SpectralMaskingManager::instance().update(
        params_.group_id,
        params_.slot,
        centroid_hz,
        params_.tolerance_hz,
        params_.max_pan,
        dt,
        params_.smooth_ms
    );

    const float pan = SpectralMaskingManager::instance().get_pan(
        params_.group_id, params_.slot
    );

    // ── Step 3: apply equal-power pan law to the stereo block ─────────────────
    _apply_pan(left, right, n_frames, pan);
}

// ---------------------------------------------------------------------------
// _apply_pan  --  equal-power stereo pan law
// ---------------------------------------------------------------------------

void SpectralPanningProcessor::_apply_pan(float* left, float* right,
                                           int n, float pan) noexcept
{
    // Map pan ∈ [−1, +1] to angle θ ∈ [0, π/2].
    // pan = −1 → θ = 0   → gain_L = cos(0)     = 1,   gain_R = sin(0)     = 0  (full left)
    // pan =  0 → θ = π/4 → gain_L = cos(π/4)   = √2/2 (centre, equal power)
    // pan = +1 → θ = π/2 → gain_L = cos(π/2)   = 0,   gain_R = sin(π/2)   = 1  (full right)
    const float theta   = (pan + 1.0f) * _SP_QUARTER_PI;
    const float gain_l  = cosf(theta);
    const float gain_r  = sinf(theta);

    for (int i = 0; i < n; ++i) {
        // Compute the mono sum and apply the divergent gains.
        // A pure pan (no M/S split) preserves the stereo signal's
        // directionality while shifting its apparent centre.
        const float mono = (left[i] + right[i]) * 0.5f;
        left[i]  = mono * gain_l * 2.0f;   // ×2 to compensate for the 0.5 sum
        right[i] = mono * gain_r * 2.0f;
    }
}
