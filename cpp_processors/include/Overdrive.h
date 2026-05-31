/**
 * Overdrive.h -- Distortion / Overdrive / Fuzz Processor
 * =======================================================
 * Three classic drive modes activated by a DriveMode enum:
 *
 *   OVERDRIVE  -- Asymmetric soft-clip: positive half uses a smooth x/(1+|x|)
 *                 saturation while the negative half clips harder, mimicking a
 *                 single diode in the feedback path of a guitar pedal.
 *
 *   DISTORTION -- Hard symmetric clipping at ±1 after pre-gain.
 *                 High pre-gain + hard clip produces dense upper harmonics.
 *
 *   FUZZ       -- Full-wave rectification (fold negative half up) followed by
 *                 hard clipping.  Creates a wall of even harmonics and a
 *                 "broken speaker" texture characteristic of Fuzz Face-style units.
 *
 * Signal path:  input → pre-gain → tone filter → waveshaper → output trim
 *
 * Tone filter modes (set_tone_type):
 *   0 = Low-pass  (warm/dark, cuts highs before clipping → smoother distortion)
 *   1 = High-pass (bright/aggressive, cuts lows before clipping → tighter feel)
 *   2 = High-shelf +6 dB (tilt EQ: pushes presence and air into the distortion)
 */

#pragma once
#include <cmath>
#include <algorithm>
#include "BiquadFilter.h"
#include "DspHelpers.h"

// Distortion topology selection.
enum class DriveMode : int {
    OVERDRIVE  = 0,  ///< Soft asymmetric clipping (diode-pair character)
    DISTORTION = 1,  ///< Hard symmetric clipping (Marshall-stack character)
    FUZZ       = 2,  ///< Full-wave rectification (Fuzz Face character)
};

class Overdrive {
public:
    explicit Overdrive(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    // ── Parameter setters ────────────────────────────────────────────────────

    /** DriveMode cast to int: 0=OVERDRIVE, 1=DISTORTION, 2=FUZZ. */
    void set_mode(int mode) noexcept;

    /** Pre-gain applied before the waveshaper, in dB (0..60). */
    void set_pregain(float db) noexcept;

    /** Tone filter centre / cutoff frequency in Hz (200..8000). */
    void set_tone(float hz) noexcept;

    /** Tone filter type: 0=low-pass, 1=high-pass, 2=high-shelf +6 dB. */
    void set_tone_type(int type) noexcept;

    /** Output level after the waveshaper, in dB (-24..+6). */
    void set_output(float db) noexcept;

    // ── Audio processing ─────────────────────────────────────────────────────

    /** Process one block in-place. */
    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    int   mode_;
    float pregain_linear_;
    float output_linear_;
    int   tone_type_;
    float tone_hz_;

    // Single biquad tone filter (shared coefficients, separate L/R state).
    StereoBiquad tone_filter_;

    void _rebuild_tone() noexcept;

    // ── Waveshapers (stateless, called per-sample) ───────────────────────────

    /** Asymmetric overdrive: smooth positive, harder negative half. */
    static inline float _overdrive(float x) noexcept {
        if (x >= 0.0f) {
            // Positive: diode soft-clip, range [0, 1)
            return x / (1.0f + x);
        } else {
            // Negative: slightly harder (1.5× compression ratio vs positive)
            const float ax = -x;
            return -(ax / (1.0f + ax * 1.5f));
        }
    }

    /** Hard symmetric clip at ±1. Pre-gain provides all the character. */
    static inline float _distortion(float x) noexcept {
        return dsp::clamp(x, -1.0f, 1.0f);
    }

    /** Full-wave rectification + hard clip.  Creates dense even harmonics. */
    static inline float _fuzz(float x) noexcept {
        // Fold: abs gives all-positive, shift back to −1..+1 centre,
        // then hard-clip the top to keep peaks controlled.
        const float rect = std::abs(x);          // 0..+∞
        const float centred = rect * 1.8f - 0.9f; // map to approx −0.9..+∞
        return dsp::clamp(centred, -1.0f, 1.0f);
    }
};
