/*
 * VelocityHumanizer.h  --  Real-Time MIDI Velocity Humanization Engine
 * ======================================================================
 * Combines a Gaussian random velocity offset with a deterministic musical
 * timing-weight function to produce non-robotic, musically natural
 * velocity variation.
 *
 * Algorithm (per event):
 *
 *   1. Query TimingWeightFunction for the beat-position weight w.
 *        w > 1.0  →  event lands on a strong beat  (accent)
 *        w < 1.0  →  event lands on a weak offbeat (de-emphasis)
 *
 *   2. Compute the timing-weighted mean:
 *        μ_eff = clamp(base_velocity × w, 1, 127)
 *
 *   3. Draw one sample from the Gaussian N(μ_eff, σ²):
 *        v = GaussianRng::sample(μ_eff, σ)
 *      where σ is the user's "Humanize / Variance" knob.
 *
 *   4. Round to the nearest integer and clamp to [1, 127].
 *
 * This satisfies the requirement's probability density formula exactly:
 *
 *   f(x) = 1/(σ√(2π)) · exp(-½·((x-μ)/σ)²)
 *
 * with μ = μ_eff (the timing-weighted target velocity).
 *
 * Reproducibility:
 *   For offline export, call reseed(project_seed) before rendering so
 *   the same MIDI data always produces identical humanization output
 *   across multiple export passes.
 *
 * Thread safety:
 *   humanize() is NOT thread-safe; call only from the audio thread.
 *   set_params() and reseed() must also be called from the audio thread,
 *   or with external synchronisation.
 */

#pragma once

#include "GaussianRng.h"
#include "TimingWeightFunction.h"
#include <cstdint>

class VelocityHumanizer {
public:
    // All tunable parameters in one plain-old-data struct.
    struct Params {
        double   sigma             = 8.0;   // Gaussian spread in velocity units [0, 64]
        double   downbeat_boost    = 0.15;  // Fractional accent boost on bar's beat 1 [0, 1]
        double   offbeat_reduction = 0.08;  // Fractional cut on weak offbeats [0, 1]
        int      time_sig_num      = 4;     // Beats per bar (numerator)
        int      time_sig_denom    = 4;     // Beat value (denominator; 4 = quarter note)
        double   snap_tolerance    = 0.10;  // Grid-snap window in beats
        uint64_t seed              = 0;     // PRNG seed; 0 = built-in constant default
    };

    // Construct with optional initial parameters.
    explicit VelocityHumanizer(Params p = Params{});

    // Humanize a single MIDI note velocity.
    //
    // base_velocity : the automation-line target value [1, 127]
    // beat_position : absolute beat of the note (0.0 = song start)
    //
    // Returns an integer in [1, 127].
    int humanize(int base_velocity, double beat_position);

    // Replace all parameters atomically.
    // Timing config and PRNG seed are updated immediately.
    void set_params(const Params& p);

    const Params& params() const;

    // Re-seed the Gaussian RNG for reproducible offline renders.
    void reseed(uint64_t seed);

private:
    Params               params_;   // Current parameter snapshot
    GaussianRng          rng_;      // Gaussian PRNG (Box-Muller / Xorshift64)
    TimingWeightFunction timing_;   // Deterministic beat-position weight calculator

    // Build a TimingWeightFunction::Config from the current Params.
    TimingWeightFunction::Config build_timing_config() const;
};
