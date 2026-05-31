/*
 * AutoFilter.h  --  Resonant multi-mode filter with LFO and envelope follower
 * ============================================================================
 * Algorithm overview
 * ------------------
 * FILTER
 *   A transposed Direct-Form II biquad (TDF-II) is computed per-sample using
 *   the RBJ Audio EQ Cookbook formulas for LP / HP / BP (constant-peak-gain).
 *   The filter coefficients are recomputed whenever the effective cutoff
 *   frequency changes (not every sample — see MODULATION below).
 *
 *   Pre-filter soft-clip drive (tanh) adds harmonic warmth and prevents
 *   self-oscillation from blowing up with high resonance values.
 *
 * MODULATION  (two sources, selectable or combined)
 *
 *   LFO — a phase accumulator oscillator running at lfo_rate_hz_.
 *   Shapes: Sine, Triangle, Square, Saw-up.
 *   Output is in [-1, +1]; multiplied by lfo_depth_ and LFO_OCTAVES to
 *   produce a log-scaled pitch offset in octaves.
 *
 *   Envelope Follower — per-sample peak detector with independent attack
 *   and release time constants (ms).  Output is in [0, +1].
 *   Multiplied by env_depth_ and ENV_OCTAVES.
 *
 *   Both modulators are evaluated every block (before the sample loop) via
 *   per-sample update of the LFO and envelope.  The biquad coefficients are
 *   recomputed every COEFF_UPDATE_INTERVAL samples to reduce CPU load while
 *   keeping modulation smooth enough at normal LFO rates.
 *
 *   Effective cutoff:
 *     mod_oct  = lfo_sample * lfo_depth_ * LFO_OCTAVES   (if LFO source active)
 *              + env_sample * env_depth_ * ENV_OCTAVES    (if ENV source active)
 *     f_cut    = clamp(cutoff_hz_ * 2^mod_oct, 20, sr/2 - 10)
 *
 * DRY / WET blend
 *   The original input is preserved and blended against the filtered output
 *   via `wet_` (0 = dry, 1 = fully filtered).
 *
 * Memory policy
 * -------------
 * No heap allocation inside process().
 */

#pragma once

#include <cmath>
#include <algorithm>

#ifndef MAX_BLOCK_SIZE
#define MAX_BLOCK_SIZE 4096
#endif

/* Available filter topologies. */
enum class FilterMode : int {
    LOWPASS  = 0,
    HIGHPASS = 1,
    BANDPASS = 2,
};

/* Modulation source selection. */
enum class ModSource : int {
    LFO      = 0,   // LFO only
    ENVELOPE = 1,   // Envelope follower only
    BOTH     = 2,   // LFO + Envelope (summed)
};

/* LFO oscillator waveform. */
enum class LfoShape : int {
    SINE     = 0,
    TRIANGLE = 1,
    SQUARE   = 2,
    SAWUP    = 3,
};

class AutoFilter {
public:
    // ─── construction / lifecycle ──────────────────────────────────────────
    explicit AutoFilter(float sample_rate);
    void prepare(float sample_rate);
    void reset();

    // ─── filter parameters ────────────────────────────────────────────────
    /** LP = 0, HP = 1, BP = 2. */
    void set_filter_mode(int mode);

    /** Base cutoff frequency in Hz (20 .. 20000). */
    void set_cutoff_hz(float hz);

    /** Resonance / Q factor (0.5 .. 12.0). High values → self-oscillation. */
    void set_resonance(float q);

    /**
     * Pre-filter drive (0 .. 1).
     * At 0: linear pass-through.  At 1: heavy tanh soft-clip.
     */
    void set_drive(float drive);

    // ─── modulation source ────────────────────────────────────────────────
    /** 0 = LFO, 1 = Envelope, 2 = Both. */
    void set_mod_source(int src);

    // ─── LFO parameters ───────────────────────────────────────────────────
    /** LFO rate in Hz (0.01 .. 20). */
    void set_lfo_rate_hz(float hz);

    /** LFO depth: 0 = no modulation, 1 = full ±LFO_OCTAVES range. */
    void set_lfo_depth(float depth);

    /** 0 = sine, 1 = triangle, 2 = square, 3 = saw-up. */
    void set_lfo_shape(int shape);

    // ─── envelope follower parameters ─────────────────────────────────────
    /** Envelope attack time in ms (1 .. 500). */
    void set_env_attack_ms(float ms);

    /** Envelope release time in ms (10 .. 5000). */
    void set_env_release_ms(float ms);

    /**
     * Envelope depth: 0 = no modulation, 1 = full ENV_OCTAVES upward sweep.
     * Louder signal → cutoff opens upward (wah-wah effect).
     */
    void set_env_depth(float depth);

    // ─── output ───────────────────────────────────────────────────────────
    /** Dry/wet mix: 0 = dry, 1 = fully filtered. */
    void set_wet(float wet);

    // ─── audio processing ─────────────────────────────────────────────────
    void process(float* left, float* right, int num_samples);

private:
    float sample_rate_;

    // ── Biquad state (per channel, TDF-II) ───────────────────────────────
    struct BiquadState {
        float s1 = 0.f, s2 = 0.f;   // TDF-II delay elements
    };
    struct BiquadCoeffs {
        float b0 = 1.f, b1 = 0.f, b2 = 0.f;   // feed-forward
        float a1 = 0.f, a2 = 0.f;              // feed-back (a0 normalised out)
    };

    BiquadState  filt_l_{}, filt_r_{};
    BiquadCoeffs coeffs_{};

    /* Recompute every N samples to reduce coefficient update overhead. */
    static constexpr int COEFF_UPDATE_INTERVAL = 8;
    int coeff_ctr_ = 0;

    // ── LFO state ─────────────────────────────────────────────────────────
    float lfo_phase_    = 0.f;   // 0 .. 1 (normalised)
    float lfo_phase_inc_= 0.f;   // phase increment per sample

    // ── Envelope follower state ───────────────────────────────────────────
    float env_l_    = 0.f;   // smoothed envelope (left channel)
    float env_r_    = 0.f;   // smoothed envelope (right channel)
    float env_att_c_= 0.f;  // attack coefficient  (per sample)
    float env_rel_c_= 0.f;  // release coefficient (per sample)

    // ── Parameters ────────────────────────────────────────────────────────
    FilterMode filter_mode_ = FilterMode::LOWPASS;
    float cutoff_hz_  = 1000.f;
    float resonance_  = 1.f;
    float drive_      = 0.f;

    ModSource mod_src_   = ModSource::LFO;

    float lfo_rate_  = 0.5f;
    float lfo_depth_ = 0.5f;
    LfoShape lfo_shape_ = LfoShape::SINE;

    float env_attack_ms_  = 10.f;
    float env_release_ms_ = 200.f;
    float env_depth_      = 0.5f;

    float wet_ = 1.f;

    /*
     * Maximum modulation range in octaves.
     * LFO spans ±LFO_OCTAVES from the base cutoff (bidirectional).
     * Envelope spans 0..+ENV_OCTAVES (unidirectional, upward).
     */
    static constexpr float LFO_OCTAVES = 4.f;
    static constexpr float ENV_OCTAVES = 4.f;

    // ── Private helpers ───────────────────────────────────────────────────
    /** Recompute biquad coefficients for the given cutoff frequency. */
    void _recompute_filter(float cutoff_hz);

    /** Recompute envelope attack/release coefficients from ms values. */
    void _recompute_env_coeffs();

    /** Recompute lfo_phase_inc_ from lfo_rate_ and sample_rate_. */
    void _recompute_lfo_inc();

    /**
     * Advance LFO by one sample and return the current oscillator value
     * in [-1, +1].
     */
    float _lfo_tick() noexcept;

    /**
     * Process one sample through the biquad (TDF-II, in-place).
     * Returns the filtered output.
     */
    float _biquad_tick(BiquadState& s, float x) const noexcept;

    /**
     * Advance the envelope follower for one sample |x| and return the
     * current smoothed envelope value in [0, 1].
     */
    float _env_tick(float& env, float x) noexcept;
};
