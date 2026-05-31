/**
 * Exciter.h -- High-Frequency Harmonic Exciter / Air Enhancer
 * ============================================================
 * Emulates classic psychoacoustic exciters (Aphex, BBE) that add subtle upper
 * harmonic content to make recordings sound more "present" and "airy".
 *
 * Signal path:
 *   1. LR4 (Linkwitz-Riley 4th-order) crossover splits the signal at
 *      crossover_hz_ into a low band (lf) and a high band (hf).
 *
 *   2. The high band is run through a gentle tanh waveshaper to generate
 *      new harmonics.  The drive into the shaper scales with the "harmonics"
 *      parameter, so at 0 the HF signal passes through unchanged.
 *
 *   3. An optional "air" high-shelf EQ (+0..+12 dB at 8 kHz) is applied to
 *      the HF signal after excitation to further brighten the top end.
 *
 *   4. Wet/dry mix: dry = original full-band signal; wet = lf + excited HF.
 *      Blending at wet=0.5 adds the excitement without drowning the original.
 *
 * The low band is always recombined untouched — only HF content is processed,
 * preventing low-mid muddiness and sub-bass phase issues.
 */

#pragma once
#include <cmath>
#include <algorithm>
#include "BiquadFilter.h"
#include "DspHelpers.h"

class Exciter {
public:
    explicit Exciter(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    // ── Parameter setters ────────────────────────────────────────────────────

    /** High-pass crossover frequency in Hz (3000..12 000).
     *  Only content above this frequency is excited. */
    void set_crossover_hz(float hz) noexcept;

    /** Amount of harmonic generation (0..1).
     *  0 = none (HF passes through); 1 = maximum saturation drive. */
    void set_harmonics(float amount) noexcept;

    /** Air shelf boost in dB applied to the excited HF signal (0..12 dB). */
    void set_air(float db) noexcept;

    /** Wet/dry blend: 0 = fully dry (unprocessed), 1 = fully wet (excited). */
    void set_wet(float w) noexcept;

    // ── Audio processing ─────────────────────────────────────────────────────

    /** Process one block in-place. */
    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    float crossover_hz_;
    float harmonics_;    // 0..1 amount of drive into the excite shaper
    float air_linear_;   // linear gain for the air shelf
    float wet_, dry_;

    // LR4 crossover (4th-order Linkwitz-Riley via two cascaded Butterworth biquads).
    Lr4Filter lp_cross_, hp_cross_;

    // High-shelf filter for the "air" boost (fixed at 8 kHz centre).
    StereoBiquad air_shelf_;

    void _rebuild_crossover() noexcept;
    void _rebuild_air_shelf() noexcept;

    /** Gentle tanh excitation shaper.
     *  At drive=0 returns x unchanged; at drive=1 clips moderately.
     *  Max drive into tanh is clamped to 4 to avoid muting at high harmonics. */
    static inline float _excite(float x, float harmonics) noexcept {
        // Scale drive: 1 (unity) at harmonics=0 up to 5 at harmonics=1.
        const float drive = 1.0f + harmonics * 4.0f;
        const float norm  = std::tanh(drive);   // normalise so shaper ≤ 1 at peak
        return std::tanh(drive * x) / norm;
    }
};
