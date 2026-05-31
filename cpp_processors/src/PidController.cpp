/*
 * PidController.cpp  --  Implementation of Discrete-Time PID Controller
 * =======================================================================
 * See PidController.h for the full algorithm description.
 */

#include "PidController.h"
#include <cmath>

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

PidController::PidController(const Params& p) noexcept
    : params_(p)
    , integral_(0.0f)
    , prev_error_(0.0f)
{
}

// ---------------------------------------------------------------------------
// set_params()
// ---------------------------------------------------------------------------

void PidController::set_params(const Params& p) noexcept
{
    params_ = p;
    // Reset integral when the setpoint changes to avoid a sudden jump.
    reset();
}

// ---------------------------------------------------------------------------
// process()
// ---------------------------------------------------------------------------

float PidController::process(float process_variable, float dt) noexcept
{
    // Avoid divide-by-zero for zero time step.
    if (dt <= 0.0f) return 0.0f;

    // Compute error: positive error → signal is below target → need gain boost.
    const float error = params_.setpoint - process_variable;

    // Integral term with anti-windup clamping.
    integral_ += error * dt;
    integral_  = _clamp(integral_,
                        -params_.integral_max,
                        +params_.integral_max);

    // Derivative term (rate of change of error, forward-Euler).
    const float derivative = (error - prev_error_) / dt;
    prev_error_            = error;

    // PID sum.
    const float u = params_.kp * error
                  + params_.ki * integral_
                  + params_.kd * derivative;

    // Clamp output to the user-defined gain range.
    return _clamp(u, params_.output_min, params_.output_max);
}

// ---------------------------------------------------------------------------
// reset()
// ---------------------------------------------------------------------------

void PidController::reset() noexcept
{
    integral_   = 0.0f;
    prev_error_ = 0.0f;
}
