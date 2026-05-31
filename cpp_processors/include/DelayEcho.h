/**
 * DelayEcho.h -- BPM-Synced Stereo / Ping-Pong / Tape Delay
 * ==========================================================
 * Three delay modes:
 *   STEREO   -- Independent L/R delay lines, no crossfeed.
 *   PINGPONG -- Feedback signal crosses L→R and R→L for the bouncing effect.
 *   TAPE     -- Stereo delay + sinusoidal pitch-wobble LFO on the read position
 *               + soft-clip saturation on the feedback path.
 *
 * Delay time is derived from a host BPM float (quarter, dotted-eighth, or
 * eighth-note grid) or set manually (pass bpm=0).
 *
 * Feedback path passes through a 2nd-order Butterworth hi-cut and lo-cut
 * filter pair to tame harsh repetitions.
 *
 * Interpolation: Hermite cubic (4-point) for high-quality fractional reads
 * with minimal aliasing even at short delay times.
 *
 * Buffer: power-of-2 size for fast bitwise-AND wrap-around.
 */

#pragma once
#include <array>
#include <cmath>
#include "DspHelpers.h"
#include "BiquadFilter.h"

// ~5.9 s at 44 100 Hz; 2^18 = 262 144
static constexpr int DELAY_BUF_SIZE = (1 << 18);
static constexpr int DELAY_BUF_MASK = DELAY_BUF_SIZE - 1;

/** Stereo topology of the delay. */
enum class DelayMode : int {
    STEREO   = 0,  ///< Standard dual mono delay
    PINGPONG = 1,  ///< Feedback crosses L/R channels
    TAPE     = 2,  ///< Stereo + pitch wobble LFO + saturation
};

/** Beat-division options for BPM-synced delay time. */
enum class DelayDivision : int {
    QUARTER       = 0,  ///< 1/4 note  = 1 beat
    DOTTED_EIGHTH = 1,  ///< Dotted 1/8 = 3/4 beat
    EIGHTH        = 2,  ///< 1/8 note  = 1/2 beat
};


class DelayEcho {
public:
    explicit DelayEcho(float sample_rate);

    /** Re-initialise for a new sample rate without reallocating buffers. */
    void prepare(float sample_rate);

    /** Zero all delay buffers and reset LFO / filter states. */
    void reset() noexcept;

    // ── Parameter setters ────────────────────────────────────────────────────

    /** Host BPM for sync. Pass 0 to use set_delay_ms() instead. */
    void set_bpm(float bpm) noexcept;

    /** Beat division (DelayDivision enum cast to int). */
    void set_division(int div) noexcept;

    /** Manual delay time in ms; ignored when bpm > 0. */
    void set_delay_ms(float ms) noexcept;

    /** Feedback amount 0..0.99. */
    void set_feedback(float f) noexcept;

    /** Wet/dry mix 0..1 (0 = fully dry, 1 = fully wet). */
    void set_wet(float w) noexcept;

    /** High-cut frequency on the feedback path (Hz). */
    void set_hi_cut(float hz) noexcept;

    /** Low-cut frequency on the feedback path (Hz). */
    void set_lo_cut(float hz) noexcept;

    /** Delay mode (DelayMode enum cast to int). */
    void set_mode(int mode) noexcept;

    /** Tape-mode LFO pitch-wobble rate in Hz. */
    void set_tape_rate(float hz) noexcept;

    /** Tape-mode pitch-wobble depth in ms (maps to fractional sample offset). */
    void set_tape_depth(float ms) noexcept;

    // ── Audio processing ─────────────────────────────────────────────────────

    /** Process one block in-place. */
    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    float bpm_;
    int   division_;
    float delay_samples_;   // current delay time in samples (fractional)
    float feedback_;
    float wet_, dry_;
    int   mode_;

    // Circular delay buffers (power-of-2 for & MASK wrap-around)
    std::array<float, DELAY_BUF_SIZE> buf_l_, buf_r_;
    int write_pos_;

    // Feedback hi-cut and lo-cut biquad filters (stereo)
    StereoBiquad hi_cut_, lo_cut_;
    float hi_cut_hz_, lo_cut_hz_;

    // Tape-mode pitch-wobble LFO
    float lfo_phase_;
    float tape_rate_hz_;
    float tape_depth_samps_;

    void _update_delay_from_bpm() noexcept;
    void _update_filters() noexcept;

    /** Hermite cubic interpolation for smooth fractional delay reads. */
    float _read_hermite(const std::array<float, DELAY_BUF_SIZE>& buf,
                        float delay_samps) const noexcept;

    /** Soft-clip (tanh) for tape saturation. */
    static float _saturate(float x) noexcept;
};
