/**
 * StereoImager.h -- M/S Stereo Width Processor with LF Mono Lock
 * ================================================================
 * Encodes the stereo signal into Mid (M) and Side (S) components, scales
 * the Side channel by the user's width factor, then decodes back to L/R.
 *
 *   M = (L + R) / √2
 *   S = (L − R) / √2
 *   S_out = S × width          (width 0 = mono, 1.0 = original, 2.0 = max width)
 *   L_out = (M + S_out) / √2
 *   R_out = (M − S_out) / √2
 *
 * LF Mono Lock:
 *   When enabled, the signal is split at crossover_hz using a Linkwitz-Riley
 *   4th-order crossover (two cascaded Butterworth 2nd-order biquads, same as
 *   the MultibandCompressor).  Only the HF band is processed by M/S; the LF
 *   band is summed to mono (L + R) / 2 to prevent sub-bass smearing and
 *   phase cancellation on mono systems.
 *
 * Phase Correlation Meter:
 *   After each process() call, get_correlation() returns the Pearson
 *   correlation coefficient of the output L and R channels, smoothed with a
 *   leaky integrator.  Range: −1 (fully out of phase) to +1 (identical).
 *   A well-mixed track should sit between +0.3 and +1.
 */

#pragma once
#include <cmath>
#include "DspHelpers.h"
#include "BiquadFilter.h"


class StereoImager {
public:
    explicit StereoImager(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    // ── Parameter setters ────────────────────────────────────────────────────

    /** Stereo width factor. 0 = mono, 1.0 = original, 2.0 = doubled width. */
    void set_width(float w) noexcept;

    /** Enable / disable LF mono lock below crossover_hz. */
    void set_lf_mono_lock(bool enabled) noexcept;

    /** Crossover frequency for the LF mono lock (Hz). Default 200 Hz. */
    void set_crossover_hz(float hz) noexcept;

    void process(float* left, float* right, int num_samples) noexcept;

    /**
     * Returns the smoothed Pearson correlation coefficient of the last
     * processed block.  Range −1 (anti-phase) to +1 (in-phase).
     * Thread-safe for reading from the GUI thread (atomic float write under GIL).
     */
    float get_correlation() const noexcept { return correlation_; }

private:
    float sample_rate_;
    float width_;           // Side-channel scale factor
    bool  lf_mono_lock_;
    float crossover_hz_;

    // LR4 crossover filters: one LP and one HP, each handling both L and R
    // via the shared StereoBiquad / cascaded state arrays inside Lr4Filter.
    Lr4Filter lp_filter_;   // Low-pass  (LF content)
    Lr4Filter hp_filter_;   // High-pass (HF content)

    // Smoothed Pearson phase correlation (-1..+1)
    float correlation_;

    void _rebuild_crossover() noexcept;
};
