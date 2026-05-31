/*
 * LoudnessAutomation.cpp  --  Real-Time Loudness Automation Processor
 * =====================================================================
 * See LoudnessAutomation.h for the full algorithm and signal-flow description.
 */

#include "LoudnessAutomation.h"
#include <cmath>
#include <algorithm>

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

LoudnessAutomation::LoudnessAutomation(float sample_rate) noexcept
    : sample_rate_(sample_rate > 0.0f ? sample_rate : 44100.0f)
    , params_()
    , follower_()
    , pid_()
    , current_gain_(1.0f)
    , target_gain_(1.0f)
{
    _rebuild();
}

// ---------------------------------------------------------------------------
// prepare()  --  re-initialise at a new sample rate
// ---------------------------------------------------------------------------

void LoudnessAutomation::prepare(float sample_rate) noexcept
{
    sample_rate_ = sample_rate > 0.0f ? sample_rate : 44100.0f;
    _rebuild();
}

// ---------------------------------------------------------------------------
// set_params()
// ---------------------------------------------------------------------------

void LoudnessAutomation::set_params(const Params& p) noexcept
{
    params_ = p;
    _rebuild();
}

// ---------------------------------------------------------------------------
// _rebuild()  --  sync sub-components after any parameter change
// ---------------------------------------------------------------------------

void LoudnessAutomation::_rebuild() noexcept
{
    // Rebuild the envelope follower with new attack/release times.
    follower_.prepare(sample_rate_, params_.attack_ms, params_.release_ms);

    // Rebuild the PID with the new gains and target.
    PidController::Params pid_p;
    pid_p.kp           = params_.kp;
    pid_p.ki           = params_.ki;
    pid_p.kd           = params_.kd;
    pid_p.setpoint     = params_.target_dbfs;
    pid_p.output_min   = params_.gain_min_db;
    pid_p.output_max   = params_.gain_max_db;
    pid_p.integral_max = 20.0f;  // fixed anti-windup window
    pid_.set_params(pid_p);
}

// ---------------------------------------------------------------------------
// process()  --  main per-block DSP entry point
// ---------------------------------------------------------------------------

void LoudnessAutomation::process(float* left, float* right,
                                  int n_frames) noexcept
{
    if (n_frames <= 0) return;

    // ── Step 1: measure the instantaneous RMS of this block ─────────────────
    const float block_rms = RmsAnalyzer::compute_rms(left, right, n_frames);

    // ── Step 2: smooth the RMS through the one-pole envelope follower ────────
    const float smoothed_rms = follower_.process(block_rms);

    // ── Step 3: convert to dBFS for the PID ──────────────────────────────────
    const float current_db = RmsAnalyzer::to_dbfs(smoothed_rms);

    // ── Step 4: PID produces a gain correction in dB ─────────────────────────
    // dt is one block's worth of time in seconds.
    const float dt = static_cast<float>(n_frames) / sample_rate_;
    const float gain_correction_db = pid_.process(current_db, dt);

    // ── Step 5: convert target gain correction to a linear multiplier ────────
    // Clamp the dB correction before converting to avoid extreme linear values.
    const float clamped_db = _clamp(gain_correction_db,
                                    params_.gain_min_db,
                                    params_.gain_max_db);
    target_gain_ = _db_to_linear(clamped_db);

    // ── Step 6: per-sample linear gain interpolation (zipper-noise prevention)
    // Linearly ramp current_gain_ toward target_gain_ over the whole block.
    // This is the key requirement: one ramp step per sample, not per buffer.
    const float gain_step = (target_gain_ - current_gain_)
                            / static_cast<float>(n_frames);

    for (int i = 0; i < n_frames; ++i) {
        current_gain_ += gain_step;
        left[i]        *= current_gain_;
        right[i]       *= current_gain_;
    }

    // Snap current_gain_ exactly to target_gain_ at the end of the block to
    // eliminate floating-point drift accumulation over many blocks.
    current_gain_ = target_gain_;
}

// ---------------------------------------------------------------------------
// reset()
// ---------------------------------------------------------------------------

void LoudnessAutomation::reset() noexcept
{
    follower_.reset();
    pid_.reset();
    current_gain_ = 1.0f;
    target_gain_  = 1.0f;
}

// ---------------------------------------------------------------------------
// current_gain_db()
// ---------------------------------------------------------------------------

float LoudnessAutomation::current_gain_db() const noexcept
{
    if (current_gain_ <= 1e-7f) return -120.0f;
    return 20.0f * std::log10f(current_gain_);
}
