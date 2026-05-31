#pragma once
#include <cmath>
#include <algorithm>

// Small DC offset added to envelope followers to prevent CPU-stalling denormal floats
// on x86 hardware that lacks flush-to-zero mode.
static constexpr float DENORMAL_GUARD = 1e-25f;

// Maximum block size for pre-allocated working buffers — avoids heap allocation in the audio thread.
static constexpr int MAX_BLOCK_SIZE = 4096;

// Maximum look-ahead at 192kHz for 20ms = 3840 samples; round up to power-of-2 for safe buffer sizing.
static constexpr int MAX_LOOKAHEAD = 4096;

namespace dsp {

inline float db_to_linear(float db) noexcept {
    return std::pow(10.0f, db / 20.0f);
}

inline float linear_to_db(float v) noexcept {
    // Clamp to avoid log(0); -200 dB corresponds to the clamp floor.
    return 20.0f * std::log10(std::max(v, 1e-10f));
}

// Returns a one-pole IIR smoothing coefficient for the given time constant in milliseconds.
// Derived from: y[n] = coeff * y[n-1] + (1-coeff) * x[n], solving for the -3dB
// point at the reciprocal of the time constant.
// Returns 0 (no smoothing / instant) when ms <= 0.
inline float time_to_coeff(float ms, float sr) noexcept {
    if (ms <= 0.0f) return 0.0f;
    return std::exp(-1000.0f / (ms * sr));
}

template<typename T>
inline T clamp(T val, T lo, T hi) noexcept {
    return val < lo ? lo : (val > hi ? hi : val);
}

} // namespace dsp
