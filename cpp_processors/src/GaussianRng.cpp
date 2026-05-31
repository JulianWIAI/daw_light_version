/*
 * GaussianRng.cpp  --  Implementation of the Gaussian PRNG
 * =========================================================
 * See GaussianRng.h for the design rationale and usage contract.
 *
 * Key implementation decisions:
 *
 * Xorshift64 shifts (13, 7, 17):
 *   These three shift values are from Marsaglia (2003), "Xorshift RNGs",
 *   Journal of Statistical Software.  They provide a maximal-period
 *   sequence of length 2^64 - 1 and pass all SmallCrush tests.
 *
 * Box-Muller transform:
 *   Given two independent uniform samples u1 ∈ (0,1] and u2 ∈ (0,1):
 *     z0 = √(-2 ln u1) · cos(2π u2)      ← returned this call
 *     z1 = √(-2 ln u1) · sin(2π u2)      ← cached for next call
 *   Both z0 and z1 are independent N(0,1) samples.
 *
 *   u1 is drawn from (0, 1] (excluding 0) to prevent log(0).  The
 *   do-while loop is hit with probability ≈ 2^-53 — negligible cost.
 *
 * uniform_open() → (0, 1):
 *   The top 53 bits of the 64-bit output fill an IEEE 754 double's
 *   mantissa.  Dividing by 2^53 maps the range [1, 2^53] to (0, 1).
 *   The result can never be exactly 0 (minimum uint output = 1 after
 *   the shift) or exactly 1 (the denominator exceeds the numerator).
 */

#include "GaussianRng.h"

// 2π and 1/2^53 as compile-time constants.
static constexpr double TWO_PI       = 6.283185307179586476925;
static constexpr double INV_2_POW_53 = 1.0 / 9007199254740992.0;  // 1 / 2^53

// ---------------------------------------------------------------------------
// Construction / seeding
// ---------------------------------------------------------------------------

GaussianRng::GaussianRng(uint64_t seed)
    : state_(seed != 0 ? seed : 0xDEADBEEFCAFEBABEULL)
    , has_spare_(false)
    , spare_(0.0)
{}

void GaussianRng::reseed(uint64_t seed) {
    // Xorshift64 must never have a zero state; fall back to the default constant.
    state_     = (seed != 0) ? seed : 0xDEADBEEFCAFEBABEULL;
    has_spare_ = false;
    spare_     = 0.0;
}

// ---------------------------------------------------------------------------
// Core PRNG  --  Xorshift64
// ---------------------------------------------------------------------------

uint64_t GaussianRng::xorshift64() {
    // Three-tap xorshift: x ^= x<<13; x ^= x>>7; x ^= x<<17
    // Shift triplet (13, 7, 17) produces a maximal-period LFSRs in GF(2^64).
    state_ ^= state_ << 13;
    state_ ^= state_ >> 7;
    state_ ^= state_ << 17;
    return state_;
}

double GaussianRng::uniform_open() {
    // Extract top 53 bits (full double mantissa) and scale to (0, 1).
    // Minimum non-zero value: 1 * INV_2_POW_53 ≈ 1.1e-16.
    // Maximum value:  (2^53 - 1) * INV_2_POW_53 < 1.0.
    return static_cast<double>(xorshift64() >> 11) * INV_2_POW_53;
}

// ---------------------------------------------------------------------------
// Box-Muller Gaussian sampler
// ---------------------------------------------------------------------------

double GaussianRng::sample(double mu, double sigma) {
    // If a spare standard-normal value was cached last call, use it now.
    if (has_spare_) {
        has_spare_ = false;
        // Scale cached standard-normal spare to the requested distribution.
        return mu + sigma * spare_;
    }

    // Draw u1 from (0, 1] — exclude 0 to avoid log(0) = -inf.
    double u1;
    do { u1 = uniform_open(); } while (u1 == 0.0);

    // Draw u2 from (0, 1).
    const double u2 = uniform_open();

    // Box-Muller transform produces two independent standard-normal values.
    const double magnitude = std::sqrt(-2.0 * std::log(u1));
    const double angle     = TWO_PI * u2;

    // Cache the second value (z1) for the next call so we only run the
    // transform every other invocation.
    spare_     = magnitude * std::sin(angle);
    has_spare_ = true;

    // Return the first value (z0) scaled to N(mu, sigma²).
    return mu + sigma * (magnitude * std::cos(angle));
}

// ---------------------------------------------------------------------------
// Probability density function (for UI visualisation)
// ---------------------------------------------------------------------------

double GaussianRng::pdf(double x, double mu, double sigma) {
    // f(x) = 1/(σ√(2π)) · exp(-½·((x-μ)/σ)²)
    // This is the formula stated in the requirement; not called by sample().
    const double z     = (x - mu) / sigma;
    const double coeff = 1.0 / (sigma * std::sqrt(TWO_PI));
    return coeff * std::exp(-0.5 * z * z);
}
