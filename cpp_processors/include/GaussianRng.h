/*
 * GaussianRng.h  --  Gaussian Pseudo-Random Number Generator
 * ===========================================================
 * Implements a stateful Gaussian (normal-distribution) sampler using
 * the Box-Muller transform applied to a fast Xorshift64 PRNG.
 *
 * Why Xorshift64?
 *   - Period 2^64 - 1 — sufficient for any practical MIDI sequence
 *   - Passes BigCrush statistical-randomness tests
 *   - Fits in one cache line; zero heap allocation
 *   - Deterministic: same seed always produces the same sequence,
 *     enabling reproducible offline exports
 *
 * Why Box-Muller transform?
 *   - Exact standard-normal samples (no approximation error)
 *   - Produces values in pairs; the unused spare is cached to halve the
 *     number of transcendental calls across successive invocations
 *   - O(1) per call with cheap log/cos/sin on modern FPUs
 *
 * The pdf() static method implements the probability density formula
 * from the prompt exactly:
 *
 *   f(x) = 1/(σ√(2π)) · exp(-½((x-μ)/σ)²)
 *
 * Thread safety:
 *   NOT thread-safe.  Create one instance per audio thread or protect
 *   shared access with a mutex.
 */

#pragma once

#include <cstdint>
#include <cmath>

class GaussianRng {
public:
    // Construct with an optional seed.  The default seed is a non-zero
    // constant so default-constructed instances behave deterministically.
    explicit GaussianRng(uint64_t seed = 0xDEADBEEFCAFEBABEULL);

    // Draw one sample from N(mu, sigma²).
    //   mu    : mean of the distribution (the centre value)
    //   sigma : standard deviation (the spread); must be > 0
    // Returns a double drawn from the specified Gaussian.
    double sample(double mu = 0.0, double sigma = 1.0);

    // Replace the internal PRNG state with a new seed.
    // Call this at the start of an offline export to make the humanization
    // sequence reproducible across multiple render passes.
    void reseed(uint64_t seed);

    // Evaluate the probability density at x for N(mu, sigma²).
    // This is the formula from the requirement:
    //   f(x) = 1/(σ√(2π)) · exp(-½·((x-μ)/σ)²)
    // Not used internally; provided for UI curve visualisation.
    static double pdf(double x, double mu, double sigma);

private:
    uint64_t state_;      // Xorshift64 state; must never be zero
    bool     has_spare_;  // Box-Muller produces two values; cache the second
    double   spare_;      // Cached second standard-normal value

    // Advance the Xorshift64 state and return the new 64-bit value.
    uint64_t xorshift64();

    // Map the 64-bit output to a uniform double in the open interval (0, 1).
    // Uses the top 53 bits to fill the IEEE 754 double mantissa exactly.
    double uniform_open();
};
