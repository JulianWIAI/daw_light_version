/**
 * AuditionProcessor.h -- Loudness-targeted audition stage for the MasterBus.
 * ============================================================================
 * Each AuditionProcessor wraps one BrickwallLimiter with a pre-gain stage so
 * the user can preview how the project would sound at a specific commercial
 * loudness target (e.g. -7 LUFS for a preview release, -14 LUFS for streaming)
 * before committing to an offline mastering export.
 *
 * Processing chain (per block):
 *   1. Scalar pre-gain  -- boosts / attenuates the summed bus to match the
 *                          target integrated loudness relative to the reference
 *                          (-14 LUFS streaming = 0 dB offset; -7 LUFS preview
 *                          = +7 dB offset; and so on for any future target).
 *   2. BrickwallLimiter -- true-peak ceiling prevents inter-sample overs and
 *                          clips that would cause distortion on D/A conversion.
 *
 * AuditionMode
 * ------------
 * The enum defined here is the single source of truth used by both
 * AuditionProcessor (to select a preset) and MasterBus (to route the signal).
 * pybind11 exposes it as daw_processors.AuditionMode so the Python UI can
 * pass integer values to MasterBus.set_audition_mode() without magic numbers.
 */

#pragma once

#include <cmath>
#include "BrickwallLimiter.h"

// ── Audition mode identifiers ─────────────────────────────────────────────────

enum class AuditionMode : int {
    BYPASS    = 0,  // Normal path: user-configured FX chain + user limiter.
    PREVIEW   = 1,  // Simulate a -7  LUFS loudness master (+7 dB pre-gain, -1 dBFS ceiling).
    STREAMING = 2,  // Simulate a -14 LUFS streaming master (0 dB pre-gain, -1 dBFS ceiling).
};


// ── AuditionProcessor ─────────────────────────────────────────────────────────

class AuditionProcessor {
public:
    // Construct for the given sample rate.  configure() must be called
    // afterwards to set the target gain and ceiling values.
    explicit AuditionProcessor(float sample_rate = 44100.0f);

    // ── Lifecycle ──────────────────────────────────────────────────────────────

    // Reconfigure the embedded BrickwallLimiter for a new sample rate.
    // Called from MasterBus::prepare() whenever the audio format changes.
    void prepare(float sample_rate);

    // Reset the limiter's internal state (delay lines, gain history).
    // Call when starting a new audio stream to avoid start-up transients.
    void reset() noexcept;

    // ── Target configuration ──────────────────────────────────────────────────

    // Set the loudness simulation parameters.
    //
    // pre_gain_db : Gain applied before the limiter to approximate the target
    //               integrated loudness relative to the mix reference.
    //               Examples:
    //                 PREVIEW   (+7.0 dB)  — mix is assumed to sit near -14 LUFS;
    //                                        +7 dB nudges it toward -7 LUFS territory
    //                 STREAMING ( 0.0 dB)  — pass through at reference level
    //
    // ceiling_db  : True-peak ceiling for the BrickwallLimiter (e.g. -1.0 dBFS).
    //               Streaming platforms require ≤ -1.0 dBFS true peak.
    //
    // attack_ms   : Limiter attack time in milliseconds.
    // release_ms  : Limiter release time in milliseconds.
    void configure(float pre_gain_db, float ceiling_db,
                   float attack_ms, float release_ms);

    // ── Real-time processing ──────────────────────────────────────────────────

    // Process in-place: apply pre-gain then BrickwallLimiter.
    // Must be called from the audio thread only (not GUI thread).
    void process(float* L, float* R, int n_frames) noexcept;

private:
    // Linear gain factor derived from pre_gain_db.
    float pre_gain_ = 1.0f;

    // Embedded limiter — reconfigured in configure() / prepare().
    BrickwallLimiter limiter_;
};
