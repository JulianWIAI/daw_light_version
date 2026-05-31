/*
 * PidController.h  --  Discrete-Time PID Controller
 * ==================================================
 * Implements a standard proportional-integral-derivative controller suitable
 * for low-rate audio gain automation (called once per block, not per sample).
 *
 * Discrete update law (forward-Euler integration):
 *
 *   e(t)     = setpoint − process_variable
 *   integral += e(t) × dt               [anti-windup: clamped to ±integral_max]
 *   deriv    = (e(t) − e(t-1)) / dt
 *   u(t)     = Kp×e + Ki×integral + Kd×deriv
 *   output   = clamp(u(t), output_min, output_max)
 *
 * In the loudness context:
 *   process_variable  = smoothed RMS in dBFS (from EnvelopeFollower)
 *   setpoint          = target loudness in dBFS
 *   output            = requested gain correction in dB
 */

#pragma once

class PidController {
public:
    struct Params {
        float kp           = 1.0f;    // proportional gain
        float ki           = 0.1f;    // integral gain
        float kd           = 0.05f;   // derivative gain
        float setpoint     = -18.0f;  // target loudness in dBFS
        float output_min   = -30.0f;  // minimum output (most gain reduction, dB)
        float output_max   = +12.0f;  // maximum output (most gain boost, dB)
        float integral_max = 20.0f;   // anti-windup integral accumulator clamp
    };

    explicit PidController(const Params& p = Params{}) noexcept;

    // Replace all parameters; call reset() separately if desired.
    void set_params(const Params& p) noexcept;

    // Read current parameters.
    const Params& params() const noexcept { return params_; }

    // Advance by one time step dt (seconds), process_variable in dBFS.
    // Returns the gain correction in dB.
    float process(float process_variable, float dt) noexcept;

    // Zero integral accumulator and previous error.
    void reset() noexcept;

private:
    Params params_;
    float  integral_;    // running integral of error
    float  prev_error_;  // previous error for derivative term

    // Inline clamp helper.
    static float _clamp(float v, float lo, float hi) noexcept {
        return v < lo ? lo : (v > hi ? hi : v);
    }
};
