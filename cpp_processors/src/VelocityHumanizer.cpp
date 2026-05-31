/*
 * VelocityHumanizer.cpp  --  Implementation of the Velocity Humanization Engine
 * ===============================================================================
 * See VelocityHumanizer.h for the full algorithm description and parameter docs.
 */

#include "VelocityHumanizer.h"
#include <algorithm>
#include <cmath>

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

// Clamp a double to the closed interval [lo, hi] without branching on NaN.
static inline double clamp_d(double v, double lo, double hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

VelocityHumanizer::VelocityHumanizer(Params p)
    : params_(p)
    , rng_(p.seed != 0 ? p.seed : 0xDEADBEEFCAFEBABEULL)
    , timing_(build_timing_config())
{}

// ---------------------------------------------------------------------------
// Configuration helpers
// ---------------------------------------------------------------------------

TimingWeightFunction::Config VelocityHumanizer::build_timing_config() const {
    // Copy the Params timing fields into a TimingWeightFunction::Config struct.
    TimingWeightFunction::Config cfg;
    cfg.time_sig_num      = params_.time_sig_num;
    cfg.time_sig_denom    = params_.time_sig_denom;
    cfg.downbeat_boost    = params_.downbeat_boost;
    cfg.offbeat_reduction = params_.offbeat_reduction;
    cfg.snap_tolerance    = params_.snap_tolerance;
    return cfg;
}

void VelocityHumanizer::set_params(const Params& p) {
    params_ = p;
    // Propagate timing parameters to the sub-component.
    timing_.set_config(build_timing_config());
    // Only re-seed if the caller provided an explicit seed.
    if (p.seed != 0) rng_.reseed(p.seed);
}

const VelocityHumanizer::Params& VelocityHumanizer::params() const {
    return params_;
}

void VelocityHumanizer::reseed(uint64_t seed) {
    rng_.reseed(seed);
}

// ---------------------------------------------------------------------------
// Core humanization
// ---------------------------------------------------------------------------

int VelocityHumanizer::humanize(int base_velocity, double beat_position) {
    // Step 1 — Clamp the input to the valid MIDI velocity range.
    const double base = clamp_d(static_cast<double>(base_velocity), 1.0, 127.0);

    // Step 2 — Apply the deterministic timing weight to get μ_eff.
    //
    //   μ_eff = base_velocity × w
    //
    // where w > 1 on downbeats (boost accent) and w < 1 on offbeats
    // (reduce accent), modelling the natural dynamic shaping of a human
    // player who pushes into downbeats and floats on offbeats.
    const double w      = timing_.weight(beat_position);
    const double mu_eff = clamp_d(base * w, 1.0, 127.0);

    // Step 3 — Sample from the Gaussian N(μ_eff, σ²).
    //
    // This satisfies the requirement's PDF:
    //   f(x) = 1/(σ√(2π)) · exp(-½·((x-μ_eff)/σ)²)
    //
    // σ is clamped to [0.01, 64] to keep the distribution well-behaved;
    // sigma = 0 would produce a zero-width (deterministic) Dirac distribution.
    const double sigma = clamp_d(params_.sigma, 0.01, 64.0);
    const double raw   = rng_.sample(mu_eff, sigma);

    // Step 4 — Round to the nearest integer and clamp to [1, 127].
    // lround() is used rather than a simple cast to avoid truncation bias.
    const int result = static_cast<int>(std::lround(raw));
    return std::max(1, std::min(127, result));
}
