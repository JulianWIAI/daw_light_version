/**
 * Flanger.h -- LFO-Modulated Short Delay Flanger
 * ================================================
 * Classic comb-filter flanger using a short delay line (0.1 ms – 10 ms)
 * modulated by an LFO.
 *
 * Controls:
 *   rate_hz      -- LFO frequency (0.01 – 20 Hz)
 *   depth_ms     -- LFO modulation amplitude in ms
 *   center_ms    -- Static center delay time in ms
 *   feedback     -- Feedback amount (−1..+1; negative inverts polarity)
 *   wet          -- Wet/dry mix 0..1
 *   waveform     -- 0 = sine, 1 = triangle, 2 = square
 *   stereo_width -- LFO phase offset between L and R (0 = mono, 1 = full stereo)
 *
 * Stereo width works by offsetting the R-channel LFO phase by
 * (stereo_width × π) radians, giving natural width at 0.5 (90° offset)
 * and full anti-phase (max comb divergence) at 1.0 (180°).
 */

#pragma once
#include <array>
#include <cmath>
#include "DspHelpers.h"

// 10 ms at 96 kHz ≈ 960 samples; 1024 = 2^10 gives headroom and fast masking.
static constexpr int FLANGER_BUF_SIZE = (1 << 10);
static constexpr int FLANGER_BUF_MASK = FLANGER_BUF_SIZE - 1;


class Flanger {
public:
    explicit Flanger(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    // ── Parameter setters ────────────────────────────────────────────────────

    void set_rate(float hz) noexcept;          ///< LFO rate in Hz
    void set_depth(float ms) noexcept;         ///< LFO depth (modulation amplitude) in ms
    void set_center(float ms) noexcept;        ///< Center delay time in ms
    void set_feedback(float f) noexcept;       ///< −1..+1; clamped for stability
    void set_wet(float w) noexcept;            ///< 0..1 wet/dry mix
    void set_waveform(int w) noexcept;         ///< 0=sine, 1=triangle, 2=square
    void set_stereo_width(float w) noexcept;   ///< 0..1 L/R phase offset fraction

    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    float rate_hz_;
    float depth_ms_;
    float center_ms_;
    float feedback_;
    float wet_, dry_;
    int   waveform_;
    float stereo_width_;  // 0..1 → R LFO offset = width * π radians

    // Short circular delay buffers (power-of-2)
    std::array<float, FLANGER_BUF_SIZE> buf_l_, buf_r_;
    int write_pos_;

    float lfo_phase_l_, lfo_phase_r_;  // LFO phase in cycles [0, 1)
    float fb_l_, fb_r_;                // Previous feedback sample (denormal-guarded)

    /** Evaluate the current LFO waveform at a given phase (0..1). Returns −1..+1. */
    float _lfo(float phase) const noexcept;

    /** Linear interpolation read from a power-of-2 circular buffer. */
    float _read_interp(const std::array<float, FLANGER_BUF_SIZE>& buf,
                       float delay_samps) const noexcept;
};
