/*
 * SpectralAnalyzer.h  --  Real-Time FFT Spectral Centroid Analyzer
 * =================================================================
 * Accumulates incoming mono-mixed audio samples into a ring buffer,
 * then applies a Hann window and runs a Cooley-Tukey radix-2 FFT
 * every HOP_SIZE new samples to compute the spectral centroid:
 *
 *   C = sum( f(k) * |X(k)| ) / sum( |X(k)| )   for k = 1 .. N/2-1
 *
 * where f(k) = k * sample_rate / FFT_SIZE  is the centre frequency of bin k
 * and   |X(k)|  is the magnitude of the k-th FFT bin.
 *
 * The result is updated approximately every HOP_SIZE / sample_rate seconds
 * (HOP_SIZE = FFT_SIZE / 4 = 512 by default → ~12 ms at 44.1 kHz).
 *
 * Public interface:
 *   push_block(left, right, n)  -- add n stereo samples; returns true when
 *                                   a new centroid has been computed.
 *   get_centroid()              -- most recent centroid in Hz.
 *   get_rms()                   -- approximate spectral RMS of last frame.
 */

#pragma once
#include <cmath>
#include <cstring>
#include <algorithm>

class SpectralAnalyzer {
public:
    // FFT frame length (must be a power of two).
    static constexpr int FFT_SIZE = 2048;
    // Number of new samples between successive FFT computations.
    static constexpr int HOP_SIZE = FFT_SIZE / 4;   // 512

    explicit SpectralAnalyzer(float sample_rate = 44100.0f) noexcept;

    // Re-initialise at a new sample rate (clears ring buffer + results).
    void prepare(float sample_rate) noexcept;

    // Feed one stereo block.  Left and right channels are mixed to mono
    // before being inserted into the ring buffer.
    // Returns true if the ring buffer contained enough new samples to run
    // an FFT and update the centroid.
    bool push_block(const float* left, const float* right, int n) noexcept;

    // Most recent spectral centroid in Hz (0 if not yet computed).
    float get_centroid() const noexcept { return centroid_; }

    // Approximate spectral energy of the last FFT frame (not true RMS).
    float get_rms() const noexcept { return rms_; }

    // Zero all internal state.
    void reset() noexcept;

private:
    float sample_rate_;

    // Pre-computed Hann window coefficients.
    float hann_[FFT_SIZE];

    // Ring buffer storing the last FFT_SIZE mono samples.
    float ring_[FFT_SIZE];

    // Scratch arrays for in-place FFT (re-used every computation).
    float fft_re_[FFT_SIZE];
    float fft_im_[FFT_SIZE];

    // Write position in the ring buffer (points at the NEXT slot to write).
    int write_pos_;

    // Number of new samples added since the last FFT run.
    int samples_since_fft_;

    // Last computed results.
    float centroid_;
    float rms_;

    // Run the FFT on the current ring-buffer content and update centroid_.
    void _run_fft() noexcept;

    // In-place iterative Cooley-Tukey radix-2 DIT FFT.
    // n must be a power of two.  Overwrites re[] and im[] with the spectrum.
    static void _fft_inplace(float* re, float* im, int n) noexcept;
};
