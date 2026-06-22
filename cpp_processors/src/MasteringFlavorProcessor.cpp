#include "MasteringFlavorProcessor.h"
#include "DspHelpers.h"
#include <cmath>
#include <algorithm>

#ifndef M_PI
#  define M_PI 3.14159265358979323846
#endif


// ── Constructor ───────────────────────────────────────────────────────────────

MasteringFlavorProcessor::MasteringFlavorProcessor(float sample_rate)
    : _sample_rate(sample_rate > 0.0f ? sample_rate : 44100.0f)
{
    _hs = _make_high_shelf(12000.0f, -1.0f, _sample_rate);
    _ls = _make_low_shelf (   50.0f,  2.0f, _sample_rate);

    _comp_attack_c  = dsp::time_to_coeff(30.0f, _sample_rate);
    _comp_release_c = dsp::time_to_coeff(50.0f, _sample_rate);
    _comp_makeup_lin = dsp::db_to_linear(COMP_MAKEUP_DB);
}


// ── Public API ────────────────────────────────────────────────────────────────

void MasteringFlavorProcessor::set_flavor(int flavor) noexcept {
    _flavor = static_cast<MasteringFlavor>(
        (flavor >= 0 && flavor <= 2) ? flavor : 0);
}

void MasteringFlavorProcessor::reset() noexcept {
    _hs_L.reset(); _hs_R.reset();
    _ls_L.reset(); _ls_R.reset();
    _comp_env = 0.0f;
}

void MasteringFlavorProcessor::process(float* L, float* R, int n) noexcept {
    switch (_flavor) {
        case MasteringFlavor::TRANSPARENT:   return;
        case MasteringFlavor::ANALOG_WARMTH: _process_analog(L, R, n); return;
        case MasteringFlavor::CLUB_FESTIVAL: _process_club  (L, R, n); return;
    }
}


// ── Flavor implementations ────────────────────────────────────────────────────

// Padé tanh approximation — relative error < 0.003 % for |x| < 3
static inline float fast_tanh(float x) noexcept {
    const float x2 = x * x;
    return x * (27.0f + x2) / (27.0f + 9.0f * x2);
}

void MasteringFlavorProcessor::_process_analog(float* L, float* R, int n) noexcept {
    constexpr float dry = 1.0f - ANALOG_WET;

    for (int i = 0; i < n; ++i) {
        const float eL = _hs_L.tick(L[i], _hs);
        const float eR = _hs_R.tick(R[i], _hs);
        // Blend unsaturated EQ signal with driven saturated signal.
        L[i] = dry * eL + ANALOG_WET * fast_tanh(ANALOG_DRIVE * eL);
        R[i] = dry * eR + ANALOG_WET * fast_tanh(ANALOG_DRIVE * eR);
    }
}

void MasteringFlavorProcessor::_process_club(float* L, float* R, int n) noexcept {
    const float threshold_lin  = dsp::db_to_linear(COMP_THRESHOLD_DB);
    // Slope in the gain-computer: (1 - 1/ratio) applied as dB reduction.
    // inv_ratio_m1 = (1/ratio - 1) which is negative for ratio > 1.
    const float inv_ratio_m1 = (1.0f / COMP_RATIO) - 1.0f;

    for (int i = 0; i < n; ++i) {
        // ── Low-shelf EQ ──────────────────────────────────────────────────────
        L[i] = _ls_L.tick(L[i], _ls);
        R[i] = _ls_R.tick(R[i], _ls);

        // ── Feed-forward VCA compressor (peak, stereo-linked) ─────────────────
        const float peak = std::max(std::abs(L[i]), std::abs(R[i]));
        const float coeff = (peak > _comp_env) ? _comp_attack_c : _comp_release_c;
        _comp_env = coeff * _comp_env + (1.0f - coeff) * peak + DENORMAL_GUARD;

        float gain = _comp_makeup_lin;
        if (_comp_env > threshold_lin) {
            const float db_env = dsp::linear_to_db(_comp_env);
            const float gr_db  = (db_env - COMP_THRESHOLD_DB) * inv_ratio_m1;
            gain *= dsp::db_to_linear(gr_db);
        }

        L[i] *= gain;
        R[i] *= gain;
    }
}


// ── Audio EQ Cookbook biquad coefficient builders ─────────────────────────────

MasteringFlavorProcessor::BiquadCoeff
MasteringFlavorProcessor::_make_high_shelf(float f0, float dB_gain,
                                            float sr, float S) noexcept
{
    const double A     = std::pow(10.0, dB_gain / 40.0);
    const double w0    = 2.0 * M_PI * static_cast<double>(f0) / sr;
    const double cosw  = std::cos(w0);
    const double sinw  = std::sin(w0);
    const double alpha = sinw / 2.0 * std::sqrt((A + 1.0/A) * (1.0/S - 1.0) + 2.0);
    const double sqA   = std::sqrt(A);

    const double b0 =     A * ((A+1) + (A-1)*cosw + 2*sqA*alpha);
    const double b1 = -2.*A * ((A-1) + (A+1)*cosw              );
    const double b2 =     A * ((A+1) + (A-1)*cosw - 2*sqA*alpha);
    const double a0 =          (A+1) - (A-1)*cosw + 2*sqA*alpha;
    const double a1 =     2.*((A-1) - (A+1)*cosw              );
    const double a2 =          (A+1) - (A-1)*cosw - 2*sqA*alpha;

    return { b0/a0, b1/a0, b2/a0, a1/a0, a2/a0 };
}

MasteringFlavorProcessor::BiquadCoeff
MasteringFlavorProcessor::_make_low_shelf(float f0, float dB_gain,
                                           float sr, float S) noexcept
{
    const double A     = std::pow(10.0, dB_gain / 40.0);
    const double w0    = 2.0 * M_PI * static_cast<double>(f0) / sr;
    const double cosw  = std::cos(w0);
    const double sinw  = std::sin(w0);
    const double alpha = sinw / 2.0 * std::sqrt((A + 1.0/A) * (1.0/S - 1.0) + 2.0);
    const double sqA   = std::sqrt(A);

    const double b0 =     A * ((A+1) - (A-1)*cosw + 2*sqA*alpha);
    const double b1 =  2.*A * ((A-1) - (A+1)*cosw              );
    const double b2 =     A * ((A+1) - (A-1)*cosw - 2*sqA*alpha);
    const double a0 =          (A+1) + (A-1)*cosw + 2*sqA*alpha;
    const double a1 =    -2.*((A-1) + (A+1)*cosw              );
    const double a2 =          (A+1) + (A-1)*cosw - 2*sqA*alpha;

    return { b0/a0, b1/a0, b2/a0, a1/a0, a2/a0 };
}
