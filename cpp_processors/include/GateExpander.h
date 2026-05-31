#pragma once
#include "DspHelpers.h"

// GateExpander
// ─────────────────────────────────────────────────────────────────────────────
// Noise gate with 5-state machine and optional downward expansion.
//
// States:
//   CLOSED   — signal attenuated by range_linear.
//   OPENING  — gain ramping up toward 1.0 (attack coefficient).
//   OPEN     — signal passes at unity gain (with makeup if desired).
//   HOLDING  — signal dropped below close threshold; hold timer counting down.
//   CLOSING  — gain ramping down toward range_linear (release coefficient).
//
// Hysteresis:
//   open_threshold  > close_threshold by hysteresis_db.
//   Prevents rapid chattering when a signal hovers near the threshold.
//
// Expansion mode (ratio > 1):
//   Below the threshold the gain is further reduced proportional to the
//   instantaneous level: gain = (level / threshold)^(1/ratio - 1)
//   This gives a gentler onset than the hard gate, useful for drum bleed etc.
//
// Level detection:
//   Peak envelope with very fast attack (0.1ms) and user-configurable release
//   (matched to the gate release time for consistent response).
// ─────────────────────────────────────────────────────────────────────────────

class GateExpander {
public:
    enum class GateState { CLOSED, OPENING, OPEN, HOLDING, CLOSING };

    explicit GateExpander(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    void set_threshold(float db)   noexcept;
    void set_hysteresis(float db)  noexcept;  // default 6 dB
    void set_ratio(float r)        noexcept;  // 1 = hard gate, >1 = expander
    void set_attack(float ms)      noexcept;
    void set_hold(float ms)        noexcept;
    void set_release(float ms)     noexcept;
    void set_range(float db)       noexcept;  // floor when closed, default -80 dB

    GateState get_state() const noexcept { return state_; }

    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;

    float open_threshold_linear_;
    float close_threshold_linear_;  // = open_threshold * db_to_linear(-hysteresis_db)
    float range_linear_;
    float ratio_;
    float attack_coeff_;
    float release_coeff_;

    // Level detector — peak envelope.
    float det_attack_coeff_;   // very fast (0.1ms)
    float det_release_coeff_;  // tracks release_ms_

    float level_;  // current envelope level
    float gain_;   // current smoothed gain

    GateState state_;
    int hold_samples_;       // total hold duration in samples
    int hold_samples_left_;  // countdown

    // Stored parameter values for recomputing on prepare().
    float threshold_db_, hysteresis_db_;
    float attack_ms_, release_ms_, hold_ms_;

    void recompute_thresholds() noexcept;
    void recompute_coeffs() noexcept;
};
