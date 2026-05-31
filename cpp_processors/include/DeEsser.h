#pragma once
#include <array>
#include "DspHelpers.h"
#include "BiquadFilter.h"

// DeEsser
// ─────────────────────────────────────────────────────────────────────────────
// Detects sibilant energy via a bandpass sidechain filter and applies dynamic
// gain reduction either to the full signal (WIDEBAND) or only to the high
// frequency content above the detection frequency (SPLIT).
//
// Sidechain chain:
//   Two cascaded bandpass biquads (steeper selectivity than a single stage).
//   Centred at freq_hz, Q derived from the target ±2000 Hz bandwidth.
//
// WIDEBAND mode:
//   The computed gain reduction is applied uniformly to both L and R channels.
//
// SPLIT mode:
//   The signal is split via a complementary LP/HP pair at freq_hz.  Gain
//   reduction is applied only to the HP portion; the LP portion passes through
//   unchanged.  Recombining the two portions gives a spectrally-localised
//   de-ess.  This avoids pumping on the full signal.
//
// Gain computer:
//   target_gain = ceiling / peak   if peak > ceiling, else 1.0
//   where ceiling = db_to_linear(threshold_db).
//   Smoothed with separate attack and release coefficients.
// ─────────────────────────────────────────────────────────────────────────────

class DeEsser {
public:
    enum class Mode { WIDEBAND, SPLIT };

    explicit DeEsser(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    void set_frequency(float hz) noexcept;
    void set_threshold(float db) noexcept;
    void set_ratio(float ratio) noexcept;
    void set_attack(float ms) noexcept;
    void set_release(float ms) noexcept;
    void set_split_mode(bool split) noexcept;

    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    float freq_hz_;
    float threshold_linear_;
    float ratio_;
    float attack_coeff_;
    float release_coeff_;
    Mode  mode_;

    // Two cascaded bandpass stages for sidechain; L and R tracked separately
    // so stereo material is detected independently.
    BiquadCoeffs sc_bp_coeffs_[2];
    BiquadState  sc_l_[2], sc_r_[2];

    // LP/HP pair used only in SPLIT mode.
    BiquadCoeffs split_lp_coeffs_, split_hp_coeffs_;
    BiquadState  split_lp_l_, split_lp_r_;
    BiquadState  split_hp_l_, split_hp_r_;

    // Smoothed gain per channel.
    float gain_l_, gain_r_;

    // Stored parameter values for prepare() re-initialisation.
    float attack_ms_, release_ms_;

    void rebuild_filters() noexcept;
};
