/*
 * SpectralPanningProcessor.h  --  Per-Track Spectral Panning Insert Processor
 * =============================================================================
 * Combines SpectralAnalyzer (FFT centroid) and SpectralMaskingManager (shared
 * panning state) into a single per-track insert-slot processor.
 *
 * Signal flow per process() call:
 *   1. SpectralAnalyzer::push_block(left, right, n)
 *             ↓ centroid_hz (Hz)
 *   2. SpectralMaskingManager::update(group_id, slot, centroid_hz, …)
 *             ↓ (shared state updated)
 *   3. SpectralMaskingManager::get_pan(group_id, slot)
 *             ↓ pan ∈ [−1, +1]
 *   4. Apply equal-power pan law to left[] and right[]:
 *             gain_L = cos(θ),  gain_R = sin(θ)
 *             where θ = (pan + 1) × π/4  maps [−1,+1] → [0, π/2]
 *
 * The signature void process(float* left, float* right, int n) is compatible
 * with the generic process_block_impl<T> template in bindings.cpp.
 *
 * Params:
 *   group_id     : integer shared between the two paired track processors.
 *   slot         : 0 = track A  (pans left under masking)
 *                  1 = track B  (pans right under masking)
 *   tolerance_hz : masking threshold — centroids closer than this are
 *                  considered to be masking each other.
 *   max_pan      : maximum pan deflection in [0, 1].  0.5 = 50 % deflection.
 *   smooth_ms    : low-pass filter time constant for pan transitions (ms).
 */

#pragma once

#include "SpectralAnalyzer.h"
#include "SpectralMaskingManager.h"
#include <cmath>
#include <algorithm>

// π/4 constant
static constexpr float _SP_QUARTER_PI = 0.785398163f;

class SpectralPanningProcessor {
public:
    struct Params {
        int   group_id     = 0;      // group shared with the sibling track
        int   slot         = 0;      // 0 = A (pans left), 1 = B (pans right)
        float tolerance_hz = 300.0f; // masking detection threshold (Hz)
        float max_pan      = 0.5f;   // maximum pan shift [0, 1]
        float smooth_ms    = 100.0f; // pan transition smoothing (ms)
    };

    explicit SpectralPanningProcessor(float sample_rate = 44100.0f) noexcept;

    // Re-initialise at a new sample rate.
    void prepare(float sample_rate) noexcept;

    // Replace all parameters.
    void set_params(const Params& p) noexcept;

    // Read current parameters.
    const Params& params() const noexcept { return params_; }

    // Process one stereo block in-place.
    // Analyzes → updates manager → applies smoothed pan law.
    void process(float* left, float* right, int n_frames) noexcept;

    // Read the most recent spectral centroid in Hz.
    float get_centroid() const noexcept { return analyzer_.get_centroid(); }

    // Read the current smoothed pan value for this track [-1, +1].
    float get_current_pan() const noexcept;

    // Zero all internal state.
    void reset() noexcept;

private:
    float            sample_rate_;
    Params           params_;
    SpectralAnalyzer analyzer_;

    // Apply equal-power stereo pan law to a stereo block.
    // pan ∈ [−1, +1]: −1 = full left, 0 = centre, +1 = full right.
    static void _apply_pan(float* left, float* right, int n, float pan) noexcept;
};
