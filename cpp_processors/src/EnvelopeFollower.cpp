/*
 * EnvelopeFollower.cpp  --  Implementation of One-Pole IIR Envelope Follower
 * ===========================================================================
 * See EnvelopeFollower.h for the algorithm and coefficient formula.
 */

#include "EnvelopeFollower.h"
#include <cmath>
#include <algorithm>

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

EnvelopeFollower::EnvelopeFollower(float sample_rate,
                                   float attack_ms,
                                   float release_ms) noexcept
    : attack_coeff_(0.0f)
    , release_coeff_(0.0f)
    , env_(0.0f)
{
    prepare(sample_rate, attack_ms, release_ms);
}

// ---------------------------------------------------------------------------
// prepare()  --  recompute IIR coefficients
// ---------------------------------------------------------------------------

void EnvelopeFollower::prepare(float sample_rate,
                                float attack_ms,
                                float release_ms) noexcept
{
    // Guard against divide-by-zero: minimum sample rate 1 Hz.
    const float sr = sample_rate > 0.0f ? sample_rate : 44100.0f;
    attack_coeff_  = _coeff(attack_ms,  sr);
    release_coeff_ = _coeff(release_ms, sr);
}

// ---------------------------------------------------------------------------
// process()  --  advance the envelope by one RMS sample
// ---------------------------------------------------------------------------

float EnvelopeFollower::process(float input_rms) noexcept
{
    // Use the attack coefficient when the signal is rising (input > env),
    // and the release coefficient when it is falling.
    const float coeff = (input_rms >= env_) ? attack_coeff_ : release_coeff_;
    env_ = coeff * env_ + (1.0f - coeff) * input_rms;
    return env_;
}

// ---------------------------------------------------------------------------
// reset()
// ---------------------------------------------------------------------------

void EnvelopeFollower::reset() noexcept
{
    env_ = 0.0f;
}

// ---------------------------------------------------------------------------
// _coeff()  --  static coefficient helper
// ---------------------------------------------------------------------------

float EnvelopeFollower::_coeff(float time_ms, float sample_rate) noexcept
{
    // A time constant of 0 ms means instant tracking → coefficient = 0.
    if (time_ms <= 0.0f) return 0.0f;

    // One-pole coefficient mapped from time-constant via the natural
    // exponential decay:  coeff = exp( -1 / (T_seconds × sample_rate) )
    const float t_samples = (time_ms * 0.001f) * sample_rate;
    return std::expf(-1.0f / t_samples);
}
