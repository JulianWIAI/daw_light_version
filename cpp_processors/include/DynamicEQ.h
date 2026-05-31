#pragma once
#include <array>
#include "DspHelpers.h"
#include "BiquadFilter.h"

// DynamicEQ
// ─────────────────────────────────────────────────────────────────────────────
// Up to 8 bands of dynamic equalisation.  Each band has a static gain component
// and a dynamic (level-triggered) component that reduces (or boosts) the static
// gain when the sidechain envelope crosses a threshold.
//
// Per-band algorithm:
//   1. Run input through a bandpass sidechain biquad to isolate frequency energy.
//   2. RMS-smooth the rectified sidechain output → envelope in dB.
//   3. When envelope_db > threshold_db, compute
//        gain_reduction_db = (1/ratio - 1) * (envelope_db - threshold_db)
//      This is equivalent to soft-downward compression applied to the EQ gain.
//   4. total_band_gain_db = static_gain_db + gain_reduction_db (clamped sensibly).
//   5. Apply that gain as a peak-EQ biquad on the main signal path.
//
// Coefficient update strategy:
//   The peak-EQ biquad coefficients depend on the computed gain, so they change
//   every sample.  We avoid recomputing unless the gain changes by more than
//   0.01 dB — a level of change below hearing threshold — to reduce CPU cost.
// ─────────────────────────────────────────────────────────────────────────────

struct DynEQBand {
    float freq_hz       = 1000.0f;
    float q             =    1.0f;
    float static_gain_db=    0.0f;
    float threshold_db  =  -20.0f;
    float ratio         =    2.0f;
    float attack_ms     =    5.0f;
    float release_ms    =   50.0f;
    bool  enabled       =  true;
};

class DynamicEQ {
public:
    static constexpr int MAX_BANDS = 8;

    explicit DynamicEQ(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    void set_num_bands(int n) noexcept;
    void set_band(int i, DynEQBand b) noexcept;

    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    int   num_bands_;
    DynEQBand band_params_[MAX_BANDS];

    // Sidechain bandpass filter to isolate the detection range per band.
    StereoBiquad sc_filter_[MAX_BANDS];

    // Peak-EQ filter applied on the main signal path per band.
    StereoBiquad eq_filter_[MAX_BANDS];

    // RMS envelope followers (one per channel per band).
    float rms_l_[MAX_BANDS] = {};
    float rms_r_[MAX_BANDS] = {};

    // Smoothed gain in dB per band (L and R track independently so the same
    // sidechain drives both; in practice they share the mono-summed envelope).
    float smooth_gain_db_[MAX_BANDS] = {};

    // Remember last applied gain to avoid unnecessary coefficient recomputes.
    float last_applied_gain_db_[MAX_BANDS] = {};

    void rebuild_sidechain_filter(int i) noexcept;
    void rebuild_eq_filter(int i, float gain_db) noexcept;
};
