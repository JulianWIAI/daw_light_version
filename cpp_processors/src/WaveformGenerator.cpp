/*
 * WaveformGenerator.cpp  --  Implementation of the PCM Peak-Array Generator
 * ==========================================================================
 * See WaveformGenerator.h for the full interface contract and algorithm notes.
 */

#include "WaveformGenerator.h"

// ---------------------------------------------------------------------------
// generate_peaks
// ---------------------------------------------------------------------------
// Iterates over n_peaks equal-sized chunks of the input sample buffer and
// records the loudest absolute sample in each chunk.  The resulting array
// is normalised by its own maximum so the display always uses the full
// vertical range of the waveform widget.
// ---------------------------------------------------------------------------

std::vector<float> WaveformGenerator::generate_peaks(
    const float* samples,
    int          total_frames,
    int          channels,
    int          n_peaks)
{
    // Validate parameters.
    if (n_peaks <= 0)
        throw std::invalid_argument("n_peaks must be > 0");
    if (total_frames <= 0 || channels <= 0)
        return {};  // Empty or invalid buffer — return empty peak list.

    std::vector<float> peaks;
    peaks.reserve(n_peaks);

    // Number of frames per output peak bucket.
    const int chunk = std::max(1, total_frames / n_peaks);

    float global_max = 0.0f;

    for (int p = 0; p < n_peaks; ++p) {
        const int frame_start = p * chunk;
        const int frame_end   = std::min(frame_start + chunk, total_frames);

        // No more frames — stop early (last bucket may be smaller).
        if (frame_start >= total_frames) break;

        // Find the peak absolute sample value across all channels in this chunk.
        float bucket_peak = 0.0f;
        for (int f = frame_start; f < frame_end; ++f) {
            for (int c = 0; c < channels; ++c) {
                // Interleaved layout: sample index = frame * channels + channel.
                const float s = std::fabs(samples[f * channels + c]);
                if (s > bucket_peak) bucket_peak = s;
            }
        }

        peaks.push_back(bucket_peak);

        // Track overall maximum for normalisation.
        if (bucket_peak > global_max) global_max = bucket_peak;
    }

    // Normalise so the loudest peak == 1.0.
    // Silent buffers (global_max == 0) produce an all-zero array.
    if (global_max > 0.0f) {
        for (float& v : peaks) v /= global_max;
    }

    return peaks;
}
