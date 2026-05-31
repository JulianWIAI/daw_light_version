/**
 * Saturation.h -- Tape & Tube Harmonic Saturation Processor
 * ==========================================================
 * Two modes:
 *   TUBE -- Asymmetric even-order harmonic waveshaper driven by a DC bias offset.
 *           tanh(drive*(x+bias)) - tanh(drive*bias) produces 2nd and 4th harmonics.
 *           Auto gain compensation so 0 dB drive = unity gain.
 *
 *   TAPE -- Symmetric tanh soft-saturation + drive-dependent HF roll-off LP filter
 *           (more drive = lower LP cutoff, mimicking tape oxide compression of highs).
 *
 * Harmonic meters:
 *   Three bandpass filters at 880, 1320, 1760 Hz (2nd, 3rd, 4th harmonics of 440 Hz)
 *   with envelope followers expose get_harm2/3/4() for the Python UI to poll.
 */

#pragma once
#include <cmath>
#include "BiquadFilter.h"
#include "DspHelpers.h"

// Saturation mode selection.
enum class SatMode : int {
    TUBE = 0,  ///< Asymmetric even-harmonic waveshaper (bias + tanh)
    TAPE = 1,  ///< Symmetric tanh + drive-dependent high-frequency roll-off
};

class Saturation {
public:
    explicit Saturation(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    // ── Parameter setters ────────────────────────────────────────────────────

    /** SatMode cast to int: 0=TUBE, 1=TAPE. */
    void set_mode(int mode) noexcept;

    /** Pre-saturation drive in dB (0..40). Gain compensation is automatic. */
    void set_drive(float db) noexcept;

    /** Output trim in dB (-24..+12). Applied after saturation and compensation. */
    void set_output(float db) noexcept;

    /** Tube-mode asymmetry bias (0..1). 0 = symmetric (only odd harmonics),
     *  larger values introduce increasingly strong 2nd/4th harmonic content. */
    void set_bias(float bias) noexcept;

    // ── Harmonic content meters (read from the GUI thread) ───────────────────

    /** RMS-envelope level in the 880 Hz band (2nd harmonic reference), 0..1. */
    float get_harm2() const noexcept { return harm2_env_; }

    /** RMS-envelope level in the 1320 Hz band (3rd harmonic reference), 0..1. */
    float get_harm3() const noexcept { return harm3_env_; }

    /** RMS-envelope level in the 1760 Hz band (4th harmonic reference), 0..1. */
    float get_harm4() const noexcept { return harm4_env_; }

    // ── Audio processing ─────────────────────────────────────────────────────

    /** Process one block in-place. */
    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    int   mode_;
    float drive_linear_;    // linear multiplier applied before the waveshaper
    float comp_linear_;     // automatic gain compensation (reciprocal of drive gain at x=0)
    float output_linear_;   // output trim
    float bias_;            // tube asymmetry (stored so compensation can be recalculated)
    float drive_db_;        // stored for tape LP cutoff calculation

    // Tape-mode LP filter: cutoff drops with increasing drive.
    StereoBiquad tape_lp_;

    // Three narrow bandpass filters for harmonic analysis.
    StereoBiquad bp2_, bp3_, bp4_;  // 880 Hz, 1320 Hz, 1760 Hz

    // Smoothed envelope follower values (mixed L+R average).
    float harm2_env_, harm3_env_, harm4_env_;

    // One-pole attack/release coefficients for the harmonic envelope followers.
    float env_atk_, env_rel_;

    void _rebuild_filters() noexcept;
    void _recompute_compensation() noexcept;

    // ── Waveshapers ──────────────────────────────────────────────────────────

    /** Tube waveshaper: biased tanh producing even harmonics.
     *  Normalised so that small-signal gain = 1 regardless of drive and bias. */
    inline float _tube_shape(float x) const noexcept {
        return (std::tanh(drive_linear_ * (x + bias_))
                - std::tanh(drive_linear_ * bias_))
               * comp_linear_;
    }

    /** Tape waveshaper: symmetric tanh, normalised so f(1) = 1. */
    static inline float _tape_shape(float x, float drive_linear) noexcept {
        const float th = std::tanh(drive_linear);
        if (th < 1e-9f) return x;
        return std::tanh(drive_linear * x) / th;
    }
};
