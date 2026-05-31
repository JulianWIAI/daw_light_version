/**
 * Bitcrusher.h -- Bit Depth Reduction & Sample Rate Decimation
 * =============================================================
 * Two independent degradation engines that can be used separately or together:
 *
 *   Bit depth reduction:
 *     Quantises float samples to the equivalent of N-bit integer resolution.
 *     Implemented as:  round(x * levels) / levels
 *     where levels = 2^(bits-1) - 1  (so 16-bit gives ±32767 steps).
 *     Optional triangular-PDF dither noise (two LCG noise samples averaged)
 *     is added before quantisation to reduce harmonic distortion artifacts.
 *
 *   Sample rate decimation (sample-and-hold):
 *     A phase accumulator running at resample_hz_ / host_sr_ triggers a new
 *     sample capture; in between, the output repeats the last held value.
 *     This produces the aliased, "lo-fi" texture of retro samplers.
 *
 * Wet/dry:
 *   Parallel mix between the original clean signal and the crushed signal.
 *   wet=1 is fully crushed, wet=0 is fully clean.
 *
 * No dynamic allocation inside process() — all state is stack/member data.
 */

#pragma once
#include <cmath>
#include <cstdint>
#include <algorithm>
#include "DspHelpers.h"

class Bitcrusher {
public:
    explicit Bitcrusher(float sample_rate);

    void prepare(float sample_rate) noexcept;
    void reset() noexcept;

    // ── Parameter setters ────────────────────────────────────────────────────

    /** Bit depth: 1.0 (extreme lo-fi) to 24.0 (near-transparent). */
    void set_bit_depth(float bits) noexcept;

    /** Target sample rate in Hz (500..48 000). Values above host_sr_ are clamped. */
    void set_sample_rate_hz(float hz) noexcept;

    /** Wet/dry mix: 0 = fully dry, 1 = fully crushed. */
    void set_wet(float w) noexcept;

    /** Enable triangular-PDF dither noise before quantisation. */
    void set_dither(bool enabled) noexcept;

    // ── Audio processing ─────────────────────────────────────────────────────

    /** Process one block in-place. */
    void process(float* left, float* right, int num_samples) noexcept;

private:
    float host_sr_;
    float bits_;             // effective bit depth
    float resample_hz_;      // target decimated sample rate
    float wet_, dry_;
    bool  dither_;

    // Sample-and-hold accumulators for L and R channels independently.
    float hold_l_, hold_r_;
    float phase_;      // fractional position within the current hold period
    float phase_inc_;  // = resample_hz_ / host_sr_ — how far phase advances per sample

    // Fast 32-bit LCG (linear congruential generator) for dither noise.
    // The same state is used for both channels; triangular PDF is formed by
    // averaging two consecutive LCG outputs so each channel gets two draws.
    uint32_t rng_;

    // Precomputed quantisation step size: 1.0 / (2^(bits-1) - 1).
    float quant_step_;

    void _update_phase_inc() noexcept;
    void _update_quant_step() noexcept;

    /** Advance LCG and return a float in [−0.5, +0.5]. */
    inline float _next_noise() noexcept {
        rng_ = rng_ * 1664525u + 1013904223u;
        // Map uint32 to [0,1] then shift to [−0.5, +0.5].
        return static_cast<float>(rng_) / static_cast<float>(0xFFFFFFFFu) - 0.5f;
    }

    /** Quantise x to the current bit depth, optionally with dither. */
    inline float _quantise(float x) noexcept {
        if (dither_) {
            // Triangular PDF: two uniform draws summed and scaled to ±0.5 LSB.
            const float dn = (_next_noise() + _next_noise()) * 0.5f;
            x += dn * quant_step_;
        }
        return std::round(x / quant_step_) * quant_step_;
    }
};
