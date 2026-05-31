/*
 * SpectralAnalyzer.cpp  --  Real-Time FFT Spectral Centroid Analyzer
 * ===================================================================
 * See SpectralAnalyzer.h for the full algorithm description.
 */

#include "SpectralAnalyzer.h"
#include <cmath>
#include <cstring>
#include <algorithm>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ---------------------------------------------------------------------------
// Construction / prepare
// ---------------------------------------------------------------------------

SpectralAnalyzer::SpectralAnalyzer(float sample_rate) noexcept
    : sample_rate_(sample_rate > 0.0f ? sample_rate : 44100.0f)
    , write_pos_(0)
    , samples_since_fft_(0)
    , centroid_(0.0f)
    , rms_(0.0f)
{
    // Pre-compute the Hann window: w[n] = 0.5 * (1 - cos(2π·n / (N-1)))
    for (int i = 0; i < FFT_SIZE; ++i) {
        hann_[i] = 0.5f * (1.0f - cosf(
            2.0f * float(M_PI) * float(i) / float(FFT_SIZE - 1)
        ));
    }
    // Zero the ring buffer and scratch arrays.
    std::memset(ring_,   0, sizeof(ring_));
    std::memset(fft_re_, 0, sizeof(fft_re_));
    std::memset(fft_im_, 0, sizeof(fft_im_));
}

void SpectralAnalyzer::prepare(float sample_rate) noexcept
{
    sample_rate_ = sample_rate > 0.0f ? sample_rate : 44100.0f;
    reset();
}

void SpectralAnalyzer::reset() noexcept
{
    std::memset(ring_,   0, sizeof(ring_));
    std::memset(fft_re_, 0, sizeof(fft_re_));
    std::memset(fft_im_, 0, sizeof(fft_im_));
    write_pos_        = 0;
    samples_since_fft_ = 0;
    centroid_          = 0.0f;
    rms_               = 0.0f;
}

// ---------------------------------------------------------------------------
// push_block
// ---------------------------------------------------------------------------

bool SpectralAnalyzer::push_block(const float* left, const float* right,
                                   int n) noexcept
{
    bool updated = false;

    for (int i = 0; i < n; ++i) {
        // Down-mix stereo to mono before writing to the ring buffer.
        ring_[write_pos_] = (left[i] + right[i]) * 0.5f;
        write_pos_        = (write_pos_ + 1) % FFT_SIZE;
        ++samples_since_fft_;

        // Run the FFT once per HOP_SIZE new samples.
        if (samples_since_fft_ >= HOP_SIZE) {
            samples_since_fft_ = 0;
            _run_fft();
            updated = true;
        }
    }

    return updated;
}

// ---------------------------------------------------------------------------
// _run_fft
// ---------------------------------------------------------------------------

void SpectralAnalyzer::_run_fft() noexcept
{
    // Copy ring buffer into the scratch real array in chronological order,
    // applying the Hann window to reduce spectral leakage.
    // The oldest sample is at ring_[write_pos_] because write_pos_ points
    // to the next slot to be written, i.e., the slot written FFT_SIZE ago.
    for (int i = 0; i < FFT_SIZE; ++i) {
        int src    = (write_pos_ + i) % FFT_SIZE;
        fft_re_[i] = ring_[src] * hann_[i];
        fft_im_[i] = 0.0f;
    }

    _fft_inplace(fft_re_, fft_im_, FFT_SIZE);

    // Compute spectral centroid from positive-frequency bins (skip DC at k=0).
    // C = sum( f(k) * |X(k)| ) / sum( |X(k)| )
    float centroid_num = 0.0f;
    float centroid_den = 0.0f;
    float rms_sum      = 0.0f;

    for (int k = 1; k < FFT_SIZE / 2; ++k) {
        float freq = float(k) * sample_rate_ / float(FFT_SIZE);
        float mag  = sqrtf(fft_re_[k] * fft_re_[k] + fft_im_[k] * fft_im_[k]);

        centroid_num += freq * mag;
        centroid_den += mag;
        rms_sum      += mag * mag;
    }

    centroid_ = (centroid_den > 1e-10f) ? centroid_num / centroid_den : 0.0f;
    rms_      = sqrtf(rms_sum / float(FFT_SIZE / 2));
}

// ---------------------------------------------------------------------------
// _fft_inplace  --  iterative Cooley-Tukey radix-2 DIT FFT
// ---------------------------------------------------------------------------

void SpectralAnalyzer::_fft_inplace(float* re, float* im, int n) noexcept
{
    // ── Step 1: bit-reversal permutation ─────────────────────────────────────
    for (int i = 1, j = 0; i < n; ++i) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1)
            j ^= bit;
        j ^= bit;

        if (i < j) {
            // Swap both real and imaginary components.
            float tmp = re[i]; re[i] = re[j]; re[j] = tmp;
            /* */ tmp = im[i]; im[i] = im[j]; im[j] = tmp;
        }
    }

    // ── Step 2: butterfly stages ──────────────────────────────────────────────
    // Each stage doubles the DFT length from 2 to n.
    for (int len = 2; len <= n; len <<= 1) {
        // Twiddle factor for this stage: W = exp(-2πi / len)
        float ang = -2.0f * float(M_PI) / float(len);
        float wr  = cosf(ang);
        float wi  = sinf(ang);

        for (int i = 0; i < n; i += len) {
            // Running twiddle factor starts at W^0 = 1.
            float cur_r = 1.0f, cur_i = 0.0f;

            for (int j = 0; j < len / 2; ++j) {
                // Butterfly: u = even, v = twiddle × odd
                float u_r = re[i + j];
                float u_i = im[i + j];
                float v_r = re[i + j + len/2] * cur_r - im[i + j + len/2] * cur_i;
                float v_i = re[i + j + len/2] * cur_i + im[i + j + len/2] * cur_r;

                re[i + j]          = u_r + v_r;
                im[i + j]          = u_i + v_i;
                re[i + j + len/2]  = u_r - v_r;
                im[i + j + len/2]  = u_i - v_i;

                // Advance the running twiddle factor by one position.
                float tmp_r = cur_r * wr - cur_i * wi;
                cur_i       = cur_r * wi + cur_i * wr;
                cur_r       = tmp_r;
            }
        }
    }
}
