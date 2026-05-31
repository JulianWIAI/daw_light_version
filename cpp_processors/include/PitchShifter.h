/*
 * PitchShifter.h  --  Manual pitch shifter with optional harmonizer voice
 * ========================================================================
 * Algorithm overview
 * ------------------
 * PITCH SHIFTING
 *   The same OLA circular-buffer technique used by PitchCorrector, but with
 *   a fixed pitch ratio derived from semitones + cents rather than from
 *   automatic pitch detection.
 *
 *   pitch_ratio = 2^((semitones*100 + cents) / 1200)
 *
 *   A read pointer advances at `pitch_ratio` samples/output-sample inside
 *   a power-of-2 circular buffer.  Periodic Hanning crossfades (grain
 *   splices) keep the output seamless when the read–write gap drifts
 *   outside the [GRAIN_MIN, GRAIN_MAX] range.
 *
 * HARMONIZER
 *   A second independent read pointer reads from the SAME circular buffer
 *   at a ratio derived from `harmony_semitones` (relative to the original
 *   signal, NOT to the shifted main voice).  When enabled, the harmony
 *   voice is summed into the output at level `mix` (0 = off, 1 = equal).
 *
 *   Common harmony presets exposed through set_harmony_semitones():
 *     +3  = minor third
 *     +4  = major third
 *     +5  = perfect fourth
 *     +7  = perfect fifth
 *     +12 = octave up
 *
 * Memory policy
 * -------------
 * No heap allocation inside process().
 */

#pragma once

#include <array>
#include <cmath>
#include <algorithm>

#ifndef MAX_BLOCK_SIZE
#define MAX_BLOCK_SIZE 4096
#endif

class PitchShifter {
public:
    // ─── construction / lifecycle ──────────────────────────────────────────
    explicit PitchShifter(float sample_rate);
    void prepare(float sample_rate);
    void reset();

    // ─── parameter setters ────────────────────────────────────────────────
    /** Main pitch shift: semitones in [-12, +12]. */
    void set_semitones(int semitones);

    /** Fine pitch adjustment: cents in [-100, +100]. */
    void set_cents(int cents);

    /** Enable/disable the harmonizer second voice. */
    void set_harmonizer(bool enabled);

    /**
     * Harmony interval in semitones relative to the original (unshifted)
     * pitch.  Range: -24 .. +24.  Default: +7 (perfect fifth).
     */
    void set_harmony_semitones(int semi);

    /**
     * Harmony blend level: 0.0 = harmony silent, 1.0 = harmony equals
     * the main shifted voice in level.
     */
    void set_mix(float mix);

    /** Output trim in dB (-24 .. +12). */
    void set_output_gain(float db);

    // ─── audio processing ─────────────────────────────────────────────────
    void process(float* left, float* right, int num_samples);

private:
    float sample_rate_;

    // ── Shared circular input buffer (both voices read from this) ─────────
    static constexpr int   CIRC      = 8192;
    static constexpr int   CIRC_MASK = CIRC - 1;
    static constexpr int   GRAIN_MIN = 512;
    static constexpr int   GRAIN_MAX = 3072;
    static constexpr int   GRAIN_SZ  = 1024; // fixed jump size (no detection)
    static constexpr int   XFADE     = 256;

    float bufl_[CIRC] = {};   // left  channel circular buffer
    float bufr_[CIRC] = {};   // right channel circular buffer
    int   wpos_  = CIRC / 2;  // write pointer (primed with half-buffer offset)

    // ── Main voice read state ─────────────────────────────────────────────
    float  mrpos_  = 0.f;       // main read pointer (fractional)
    float  mxfl_[XFADE] = {};  // main crossfade fade-out buffer (left)
    float  mxfr_[XFADE] = {};  // main crossfade fade-out buffer (right)
    int    mxfp_   = XFADE;    // main crossfade position (>= XFADE = inactive)
    float  mratio_ = 1.f;      // main voice pitch ratio

    // ── Harmony voice read state ──────────────────────────────────────────
    float  hrpos_  = 0.f;      // harmony read pointer (fractional)
    float  hxfl_[XFADE] = {};  // harmony crossfade fade-out buffer (left)
    float  hxfr_[XFADE] = {};  // harmony crossfade fade-out buffer (right)
    int    hxfp_   = XFADE;   // harmony crossfade position
    float  hratio_ = 1.f;     // harmony voice pitch ratio

    // ── Parameters ────────────────────────────────────────────────────────
    int   semitones_      = 0;
    int   cents_          = 0;
    bool  harmonizer_     = false;
    int   harm_semi_      = 7;   // default: perfect fifth
    float mix_            = 0.5f;
    float out_gain_       = 1.f;

    // ── Private helpers ───────────────────────────────────────────────────
    /** Recompute mratio_ and hratio_ from semitones_, cents_, harm_semi_. */
    void _recompute_ratios();

    /** Linear interpolation read from a CIRC-length circular buffer. */
    float _read_interp(const float* buf, float pos) const noexcept;

    /**
     * Begin OLA crossfade for a single voice:
     * saves XFADE samples from rpos into xfl/xfr, then jumps rpos by jump_samples.
     */
    void _start_crossfade(float& rpos, float ratio,
                          float* xfl, float* xfr, int& xfp,
                          int jump_samples);

    /**
     * Produce one output sample for a single voice (main or harmony).
     * Handles crossfade blending and advances rpos.
     * Also triggers a new crossfade when the gap drifts out of range.
     */
    void _voice_sample(float& rpos, float ratio,
                       float* xfl, float* xfr, int& xfp,
                       float& out_l, float& out_r);
};
