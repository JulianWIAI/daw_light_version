/*
 * RmsAnalyzer.h  --  Stateless RMS Level Measurement Utility
 * ===========================================================
 * Provides static helper methods to compute per-block RMS loudness, convert
 * between linear amplitude and dBFS, and derive target amplitudes from dBFS
 * target values.  Used by LoudnessAutomation to feed the EnvelopeFollower.
 *
 * All methods are pure functions with no internal state, so the class acts as
 * a namespace.  There is no constructor.
 *
 * Algorithm:
 *   RMS = sqrt( mean( x_i^2 ) )   averaged over both L and R channels.
 *   dBFS = 20 * log10(RMS),  clamped to -120 dBFS for near-silent signals.
 */

#pragma once
#include <cmath>

class RmsAnalyzer {
public:

    // ------------------------------------------------------------------
    // compute_rms()
    //
    // Compute the root-mean-square amplitude of a stereo block.
    // Averages the squared samples from both left and right channels so
    // that a mono signal at full scale and a stereo signal at full scale
    // produce the same RMS value.
    //
    // left, right : arrays of n_frames float samples (may be equal for mono).
    // n_frames    : number of samples per channel.
    // Returns     : RMS in linear amplitude [0, ∞).  Returns 0 if n_frames==0.
    // ------------------------------------------------------------------
    static float compute_rms(const float* left, const float* right, int n_frames) noexcept;

    // ------------------------------------------------------------------
    // to_dbfs()
    //
    // Convert linear RMS amplitude to dBFS.
    // Returns -120.0f for near-zero inputs to avoid log(0).
    // ------------------------------------------------------------------
    static float to_dbfs(float rms) noexcept;

    // ------------------------------------------------------------------
    // from_dbfs()
    //
    // Convert a dBFS value to a linear amplitude multiplier.
    // ------------------------------------------------------------------
    static float from_dbfs(float db) noexcept;

private:
    // Utility class — no instances.
    RmsAnalyzer() = delete;
};
