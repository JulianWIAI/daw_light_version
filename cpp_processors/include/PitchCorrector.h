/*
 * PitchCorrector.h  --  Real-time Auto-Tune style pitch correction
 * =================================================================
 * Algorithm overview
 * ------------------
 * 1. PITCH DETECTION  (runs every YIN_HOP samples)
 *    YIN algorithm: compute the difference function d[tau] over a 1024-sample
 *    half-window, then the cumulative mean normalised difference (CMNDF),
 *    and locate the first minimum below a confidence threshold.
 *    Parabolic interpolation gives sub-sample accuracy.
 *
 * 2. SCALE SNAPPING
 *    Convert detected Hz → MIDI float, find the nearest note in the chosen
 *    scale (Major / Minor / Chromatic) relative to root_note_, convert back
 *    to Hz.  The correction amount knob blends between "no correction"
 *    (ratio = 1.0) and "full correction" (ratio = target / detected).
 *
 * 3. PITCH SHIFTING  (circular-buffer OLA)
 *    Input is written at rate 1.0 into a power-of-2 circular buffer.
 *    A fractional read pointer advances at `pitch_ratio` samples/sample
 *    (using linear interpolation).  When the read–write gap becomes too
 *    small or too large (relative to the detected pitch period) the read
 *    pointer is jumped by one period and a XFADE_LEN-sample Hanning
 *    crossfade is applied to avoid clicks.
 *
 * Memory policy
 * -------------
 * All internal buffers are fixed-size stack/member arrays.
 * No heap allocation occurs inside process().
 */

#pragma once

#include <array>
#include <cmath>
#include <algorithm>

#ifndef MAX_BLOCK_SIZE
#define MAX_BLOCK_SIZE 4096
#endif

/* Musical scale types available for pitch snapping. */
enum class ScaleType : int {
    MAJOR      = 0,
    MINOR      = 1,
    CHROMATIC  = 2,
};

class PitchCorrector {
public:
    // ─── construction / lifecycle ──────────────────────────────────────────
    explicit PitchCorrector(float sample_rate);
    void prepare(float sample_rate);   // call when sample rate changes
    void reset();                       // clear all internal state

    // ─── parameter setters ────────────────────────────────────────────────
    /** Scale type: 0 = Major, 1 = Minor, 2 = Chromatic. */
    void set_scale(int scale);

    /** Root note of the scale: 0 = C, 1 = C#/Db … 11 = B. */
    void set_root(int root);

    /**
     * Retune speed — how fast the pitch is corrected.
     * 0.0 = instant snap  (radio:  1/sample, ~0 ms)
     * 1.0 = very slow glide (~300 ms)
     * Maps linearly to a per-sample smoothing coefficient.
     */
    void set_retune_speed(float speed);

    /**
     * Correction amount — mix between dry (0) and fully corrected (1).
     * At 0.5 the detected pitch is nudged halfway toward the target note.
     */
    void set_amount(float amount);

    /** Output trim in dB (-24 .. +12). */
    void set_output_gain(float db);

    // ─── state getters (GUI polling, safe from any thread) ────────────────
    /** Returns the last detected fundamental frequency in Hz (0 if unvoiced). */
    float get_detected_hz() const noexcept { return detected_hz_; }

    /** Returns the last snapped target frequency in Hz. */
    float get_target_hz()   const noexcept { return target_hz_;   }

    // ─── audio processing (called from the real-time audio thread) ────────
    /** In-place stereo processing.  num_samples must be ≤ MAX_BLOCK_SIZE. */
    void process(float* left, float* right, int num_samples);

private:
    float sample_rate_;

    // ── YIN pitch detection ───────────────────────────────────────────────
    static constexpr int   YIN_N   = 2048;    // total analysis ring buffer
    static constexpr int   YIN_W   = 1024;    // YIN half-window (max period)
    static constexpr int   YIN_HOP = 256;     // re-detect every N input samples
    static constexpr float YIN_TH  = 0.15f;   // CMNDF detection threshold

    float abuf_[YIN_N] = {};      // circular mono-mix ring buffer for analysis
    int   awrite_       = 0;      // next write position (mod YIN_N)
    float yin_d_[YIN_W] = {};     // difference function d[tau]
    float yin_cm_[YIN_W] = {};    // cumulative mean normalised difference
    float yin_tmp_[YIN_N] = {};   // linearised copy passed to _yin_detect()
    int   hop_ctr_      = 0;      // samples since last detection run

    float detected_hz_  = 0.f;   // last detected fundamental (0 = unvoiced)
    float target_hz_    = 0.f;   // snapped target frequency
    int   period_samps_ = 512;    // estimated period in samples (for jump size)

    // ── Circular buffer for pitch shifting ───────────────────────────────
    static constexpr int   CIRC      = 8192;  // must be power-of-2
    static constexpr int   CIRC_MASK = CIRC - 1;
    static constexpr int   GRAIN_MIN = 512;   // min gap: jump read back
    static constexpr int   GRAIN_MAX = 3072;  // max gap: jump read forward

    float bufl_[CIRC] = {};   // left  circular buffer
    float bufr_[CIRC] = {};   // right circular buffer
    int   wpos_  = CIRC / 2; // write pointer (starts offset to prime latency)
    float rpos_  = 0.f;       // fractional read pointer

    // ── OLA crossfade ─────────────────────────────────────────────────────
    static constexpr int XFADE = 256;   // crossfade window length

    float  xfl_[XFADE] = {};  // pre-computed fade-out samples (left)
    float  xfr_[XFADE] = {};  // pre-computed fade-out samples (right)
    int    xfp_ = XFADE;      // crossfade position; >= XFADE means inactive

    // ── Smoothed pitch ratio ──────────────────────────────────────────────
    float ratio_    = 1.f;    // currently applied pitch shift ratio (smoothed)
    float smooth_c_ = 0.f;   // per-sample smoothing coefficient (set by speed)

    // ── Parameters ────────────────────────────────────────────────────────
    float retune_speed_ = 0.1f;
    float amount_       = 1.f;
    int   scale_type_   = static_cast<int>(ScaleType::CHROMATIC);
    int   root_note_    = 0;
    float out_gain_     = 1.f;

    // ── Private helpers ───────────────────────────────────────────────────
    /** Run YIN on the last YIN_N samples, return fundamental Hz (0 = unvoiced). */
    float _yin_detect();

    /** Snap a frequency (Hz) to the nearest note in the current scale. */
    float _snap_to_scale(float hz) const;

    /** Linear interpolation read from a CIRC-length circular buffer. */
    float _read_interp(const float* buf, float pos) const noexcept;

    /**
     * Begin an OLA crossfade: save XFADE samples from the CURRENT read
     * position into xfl_/xfr_, then jump rpos_ by jump_samples.
     */
    void _start_crossfade(int jump_samples);

    /** Update smooth_c_ from the current retune_speed_ and sample_rate_. */
    void _update_smooth_coeff();
};
