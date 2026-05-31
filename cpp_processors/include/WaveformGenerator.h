/*
 * WaveformGenerator.h  --  Fast PCM Peak-Array Generator
 * =======================================================
 * Computes a fixed-length array of normalised amplitude peaks from a
 * contiguous block of interleaved float32 PCM samples.
 *
 * The caller is responsible for loading the audio file and deinterleaving
 * it to float32.  Format-specific decoding (WAV, FLAC, OGG, AIFF, MP3)
 * is handled in Python via soundfile/scipy so this class has zero I/O
 * dependencies and compiles without external audio libraries.
 *
 * Algorithm:
 *   1. Divide the frame buffer into n_peaks equal-sized chunks.
 *   2. For each chunk find the maximum absolute sample across all channels.
 *   3. Normalise the result array so the loudest peak == 1.0.
 *
 * Thread safety:
 *   generate_peaks() is a pure static function — no shared state, fully
 *   re-entrant.  Multiple Python threads may call it simultaneously.
 *
 * Build dependency:
 *   Compiled into daw_processors via cpp_processors/src/bindings.cpp.
 *   No external libraries required beyond the C++ standard library.
 */

#pragma once

#include <vector>
#include <cmath>
#include <algorithm>
#include <stdexcept>

class WaveformGenerator {
public:
    /* -----------------------------------------------------------------------
     * generate_peaks
     * -----------------------------------------------------------------------
     * Parameters
     *   samples       Interleaved float32 PCM samples (L0,R0,L1,R1, ...).
     *                 Values are expected in the range [-1.0, 1.0].
     *   total_frames  Number of audio frames in the buffer
     *                 (each frame contains `channels` samples).
     *   channels      Channel count (1 = mono, 2 = stereo, …).
     *   n_peaks       Number of peak values to return.  Must be > 0.
     *
     * Returns
     *   std::vector<float> of length <= n_peaks.
     *   Each element is the maximum absolute sample value in its chunk,
     *   normalised so the overall maximum equals 1.0 (or 0.0 if silent).
     *
     * Throws
     *   std::invalid_argument if any parameter is out of range.
     * ----------------------------------------------------------------------- */
    static std::vector<float> generate_peaks(
        const float* samples,
        int          total_frames,
        int          channels,
        int          n_peaks
    );
};
