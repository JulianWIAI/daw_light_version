/*
 * RmsAnalyzer.cpp  --  Implementation of RMS Level Measurement Utility
 * =====================================================================
 * See RmsAnalyzer.h for the full algorithm description.
 */

#include "RmsAnalyzer.h"
#include <cmath>

// ---------------------------------------------------------------------------
// compute_rms
// ---------------------------------------------------------------------------

float RmsAnalyzer::compute_rms(const float* left, const float* right,
                                int n_frames) noexcept
{
    if (n_frames <= 0) return 0.0f;

    // Accumulate sum of squares from both channels and average them together.
    // This gives the same RMS value for a mono signal (L==R) and a stereo
    // signal at the same perceived loudness.
    double sum = 0.0;
    for (int i = 0; i < n_frames; ++i) {
        const double l = left[i];
        const double r = right[i];
        sum += l * l + r * r;
    }

    // Divide by 2 × n_frames (two channels × number of frames) to get the
    // mean square, then take the square root for RMS.
    const double mean_sq = sum / (2.0 * static_cast<double>(n_frames));
    return static_cast<float>(std::sqrt(mean_sq));
}

// ---------------------------------------------------------------------------
// to_dbfs
// ---------------------------------------------------------------------------

float RmsAnalyzer::to_dbfs(float rms) noexcept
{
    // Guard against log(0) or negative amplitudes.
    if (rms <= 1e-7f) return -120.0f;
    return 20.0f * std::log10f(rms);
}

// ---------------------------------------------------------------------------
// from_dbfs
// ---------------------------------------------------------------------------

float RmsAnalyzer::from_dbfs(float db) noexcept
{
    return std::powf(10.0f, db / 20.0f);
}
