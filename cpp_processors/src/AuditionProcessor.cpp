/**
 * AuditionProcessor.cpp -- Loudness-targeted audition stage implementation.
 * ==========================================================================
 * See AuditionProcessor.h for design notes and the AuditionMode enum.
 */

#include "AuditionProcessor.h"
#include <cmath>

// ── Construction ──────────────────────────────────────────────────────────────

AuditionProcessor::AuditionProcessor(float sample_rate)
    : limiter_(sample_rate)
{}

// ── Lifecycle ─────────────────────────────────────────────────────────────────

void AuditionProcessor::prepare(float sample_rate) {
    // Delegate to the embedded limiter so its delay lines are properly sized.
    limiter_.prepare(sample_rate);
}

void AuditionProcessor::reset() noexcept {
    limiter_.reset();
}

// ── Target configuration ──────────────────────────────────────────────────────

void AuditionProcessor::configure(float pre_gain_db, float ceiling_db,
                                   float attack_ms,   float release_ms) {
    // Convert dB gain to a linear multiplier: gain = 10^(dB/20)
    pre_gain_ = std::pow(10.0f, pre_gain_db / 20.0f);

    // Forward ceiling and timing parameters to the BrickwallLimiter.
    limiter_.set_ceiling(ceiling_db);
    limiter_.set_attack(attack_ms);
    limiter_.set_release(release_ms);
}

// ── Real-time processing ──────────────────────────────────────────────────────

void AuditionProcessor::process(float* L, float* R, int n_frames) noexcept {
    // Stage 1: Pre-gain — shift the signal toward the loudness target.
    // For PREVIEW (+7 dB) this makes the bus sound louder / more compressed,
    // simulating a commercial -7 LUFS master.
    // For STREAMING (0 dB) the signal level is unchanged; the limiter alone
    // ensures the true-peak constraint is met.
    for (int i = 0; i < n_frames; ++i) {
        L[i] *= pre_gain_;
        R[i] *= pre_gain_;
    }

    // Stage 2: Brickwall true-peak limiter — prevents digital overs.
    // Uses the Catmull-Rom inter-sample peak detection from BrickwallLimiter
    // to catch peaks that would alias above the ceiling during D/A conversion.
    limiter_.process(L, R, n_frames);
}
