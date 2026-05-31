#pragma once
#include <cmath>
#include "DspHelpers.h"

// MSVC does not define M_PI unless _USE_MATH_DEFINES is set before <cmath>.
// A file-scoped constexpr avoids that dependency and is always portable.
// #pragma once guarantees this header is included at most once per translation unit.
static constexpr double DSP_PI = 3.14159265358979323846;

// All biquad coefficient formulas from Robert Bristow-Johnson's "Audio EQ Cookbook".
// Transposed Direct Form II is used throughout: it has better numerical stability
// than Direct Form I because internal node values stay near zero when gain is low.
//
// Process equation (Transposed DF2):
//   y    = b0*x + s1
//   s1   = b1*x - a1*y + s2
//   s2   = b2*x - a2*y
//
// Note: a0 is always normalised to 1 (all coefficients divided by a0 from the cookbook).

struct BiquadCoeffs {
    float b0 = 1.0f, b1 = 0.0f, b2 = 0.0f;
    float a1 = 0.0f, a2 = 0.0f;
};

// Single-channel biquad state (transposed Direct Form II).
struct BiquadState {
    float s1 = 0.0f, s2 = 0.0f;
    void reset() noexcept { s1 = s2 = 0.0f; }
};

// Process one sample through a biquad with the given coefficients and state.
inline float biquad_process(float x, const BiquadCoeffs& c, BiquadState& s) noexcept {
    float y = c.b0 * x + s.s1;
    s.s1 = c.b1 * x - c.a1 * y + s.s2;
    s.s2 = c.b2 * x - c.a2 * y;
    return y;
}

// ──────────────────────────────────────────────────────────────
// Coefficient factory functions (EQ Cookbook formulas)
// ──────────────────────────────────────────────────────────────

namespace biquad {

inline BiquadCoeffs make_lowpass(float freq_hz, float q, float sr) noexcept {
    const float w0    = 2.0f * static_cast<float>(DSP_PI) * freq_hz / sr;
    const float cos_w = std::cos(w0);
    const float alpha = std::sin(w0) / (2.0f * q);
    const float a0_inv = 1.0f / (1.0f + alpha);
    BiquadCoeffs c;
    c.b0 = (1.0f - cos_w) * 0.5f * a0_inv;
    c.b1 = (1.0f - cos_w) * a0_inv;
    c.b2 = c.b0;
    c.a1 = -2.0f * cos_w * a0_inv;
    c.a2 = (1.0f - alpha) * a0_inv;
    return c;
}

inline BiquadCoeffs make_highpass(float freq_hz, float q, float sr) noexcept {
    const float w0    = 2.0f * static_cast<float>(DSP_PI) * freq_hz / sr;
    const float cos_w = std::cos(w0);
    const float alpha = std::sin(w0) / (2.0f * q);
    const float a0_inv = 1.0f / (1.0f + alpha);
    BiquadCoeffs c;
    c.b0 =  (1.0f + cos_w) * 0.5f * a0_inv;
    c.b1 = -(1.0f + cos_w) * a0_inv;
    c.b2 =  c.b0;
    c.a1 = -2.0f * cos_w * a0_inv;
    c.a2 = (1.0f - alpha) * a0_inv;
    return c;
}

// Bandpass with constant 0 dB peak gain (H(w0) = 1).
// The cookbook's BPF formula with sin/2Q normalisation gives 0 dB at centre.
inline BiquadCoeffs make_bandpass(float freq_hz, float q, float sr) noexcept {
    const float w0    = 2.0f * static_cast<float>(DSP_PI) * freq_hz / sr;
    const float alpha = std::sin(w0) / (2.0f * q);
    const float a0_inv = 1.0f / (1.0f + alpha);
    BiquadCoeffs c;
    c.b0 =  alpha * a0_inv;
    c.b1 =  0.0f;
    c.b2 = -alpha * a0_inv;
    c.a1 = -2.0f * std::cos(w0) * a0_inv;
    c.a2 = (1.0f - alpha) * a0_inv;
    return c;
}

// Peaking EQ: boost or cut centred at freq_hz with bandwidth controlled by Q.
inline BiquadCoeffs make_peak(float freq_hz, float q, float gain_db, float sr) noexcept {
    const float A     = std::pow(10.0f, gain_db / 40.0f);  // sqrt of linear amplitude
    const float w0    = 2.0f * static_cast<float>(DSP_PI) * freq_hz / sr;
    const float alpha = std::sin(w0) / (2.0f * q);
    const float a0_inv = 1.0f / (1.0f + alpha / A);
    BiquadCoeffs c;
    c.b0 = (1.0f + alpha * A) * a0_inv;
    c.b1 = -2.0f * std::cos(w0) * a0_inv;
    c.b2 = (1.0f - alpha * A) * a0_inv;
    c.a1 = c.b1;
    c.a2 = (1.0f - alpha / A) * a0_inv;
    return c;
}

// Low-shelf (Audio EQ Cookbook, S=1 slope).
inline BiquadCoeffs make_low_shelf(float freq_hz, float gain_db, float sr) noexcept {
    const float A     = std::pow(10.0f, gain_db / 40.0f);
    const float w0    = 2.0f * static_cast<float>(DSP_PI) * freq_hz / sr;
    const float cos_w = std::cos(w0);
    const float sin_w = std::sin(w0);
    // S=1 gives maximum slope without overshoot; alpha = sin(w0)/2 * sqrt((A+1/A)*(1/S-1)+2)
    const float alpha = (sin_w / 2.0f) * std::sqrt((A + 1.0f / A) * (1.0f / 1.0f - 1.0f) + 2.0f);
    // When S=1 the sqrt term simplifies: (A+1/A)*(1-1)+2 = 2, so alpha = sin(w0)*sqrt(2)/2 = sin(w0)/sqrt(2)
    // Use the simplified form for numerical cleanliness.
    const float alpha2 = sin_w * std::sqrt(2.0f) / 2.0f;
    (void)alpha; // suppress unused warning
    const float sqrtA2 = 2.0f * std::sqrt(A) * alpha2;
    const float a0_inv = 1.0f / ((A + 1.0f) + (A - 1.0f) * cos_w + sqrtA2);
    BiquadCoeffs c;
    c.b0 =  A * ((A + 1.0f) - (A - 1.0f) * cos_w + sqrtA2) * a0_inv;
    c.b1 =  2.0f * A * ((A - 1.0f) - (A + 1.0f) * cos_w) * a0_inv;
    c.b2 =  A * ((A + 1.0f) - (A - 1.0f) * cos_w - sqrtA2) * a0_inv;
    c.a1 = -2.0f * ((A - 1.0f) + (A + 1.0f) * cos_w) * a0_inv;
    c.a2 = ((A + 1.0f) + (A - 1.0f) * cos_w - sqrtA2) * a0_inv;
    return c;
}

// High-shelf (Audio EQ Cookbook, S=1 slope).
inline BiquadCoeffs make_high_shelf(float freq_hz, float gain_db, float sr) noexcept {
    const float A     = std::pow(10.0f, gain_db / 40.0f);
    const float w0    = 2.0f * static_cast<float>(DSP_PI) * freq_hz / sr;
    const float cos_w = std::cos(w0);
    const float alpha = std::sin(w0) * std::sqrt(2.0f) / 2.0f;
    const float sqrtA2 = 2.0f * std::sqrt(A) * alpha;
    const float a0_inv = 1.0f / ((A + 1.0f) - (A - 1.0f) * cos_w + sqrtA2);
    BiquadCoeffs c;
    c.b0 =  A * ((A + 1.0f) + (A - 1.0f) * cos_w + sqrtA2) * a0_inv;
    c.b1 = -2.0f * A * ((A - 1.0f) + (A + 1.0f) * cos_w) * a0_inv;
    c.b2 =  A * ((A + 1.0f) + (A - 1.0f) * cos_w - sqrtA2) * a0_inv;
    c.a1 =  2.0f * ((A - 1.0f) - (A + 1.0f) * cos_w) * a0_inv;
    c.a2 = ((A + 1.0f) - (A - 1.0f) * cos_w - sqrtA2) * a0_inv;
    return c;
}

} // namespace biquad

// ──────────────────────────────────────────────────────────────
// Stereo biquad (two independent state machines, shared coefficients)
// ──────────────────────────────────────────────────────────────
struct StereoBiquad {
    BiquadCoeffs coeffs;
    BiquadState  state_l, state_r;

    void reset() noexcept { state_l.reset(); state_r.reset(); }

    inline float process_l(float x) noexcept { return biquad_process(x, coeffs, state_l); }
    inline float process_r(float x) noexcept { return biquad_process(x, coeffs, state_r); }
};

// ──────────────────────────────────────────────────────────────
// Linkwitz-Riley 4th order filter (LR4)
// LR4 = two cascaded Butterworth 2nd-order biquads at the SAME frequency.
// Butterworth Q = 1/√2 ≈ 0.70711 gives maximally flat passband.
// Two in cascade gives a Butterworth-squared (Linkwitz-Riley) response:
//   - −6dB at the crossover frequency (both halves sum to 0dB flat)
//   - −80dB/decade roll-off
// ──────────────────────────────────────────────────────────────
static constexpr float BUTTERWORTH_Q = 0.70711f;

struct Lr4Filter {
    // Two cascaded biquads per stage, separate L/R state for each stage.
    BiquadCoeffs stage_coeffs[2];  // stage[0] and stage[1] have identical coefficients for LR4
    BiquadState  state_l[2];
    BiquadState  state_r[2];

    void make_lowpass(float freq_hz, float sr) noexcept {
        BiquadCoeffs bc = biquad::make_lowpass(freq_hz, BUTTERWORTH_Q, sr);
        stage_coeffs[0] = stage_coeffs[1] = bc;
    }

    void make_highpass(float freq_hz, float sr) noexcept {
        BiquadCoeffs bc = biquad::make_highpass(freq_hz, BUTTERWORTH_Q, sr);
        stage_coeffs[0] = stage_coeffs[1] = bc;
    }

    void reset() noexcept {
        state_l[0].reset(); state_l[1].reset();
        state_r[0].reset(); state_r[1].reset();
    }

    inline float process_l(float x) noexcept {
        float y = biquad_process(x, stage_coeffs[0], state_l[0]);
        return biquad_process(y, stage_coeffs[1], state_l[1]);
    }

    inline float process_r(float x) noexcept {
        float y = biquad_process(x, stage_coeffs[0], state_r[0]);
        return biquad_process(y, stage_coeffs[1], state_r[1]);
    }
};
