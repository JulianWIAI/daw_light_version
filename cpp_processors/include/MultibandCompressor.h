#pragma once
#include <array>
#include "DspHelpers.h"
#include "BiquadFilter.h"

// MultibandCompressor
// ─────────────────────────────────────────────────────────────────────────────
// 4-band compressor using Linkwitz-Riley 4th-order (LR4) crossover filters.
// Each band has its own compression parameters and can be muted or soloed.
//
// Crossover topology (3 crossover points, 4 bands):
//   Band 0: LP  @ xover[0]
//   Band 1: HP  @ xover[0], LP  @ xover[1]
//   Band 2: HP  @ xover[0], HP  @ xover[1], LP  @ xover[2]
//   Band 3: HP  @ xover[0], HP  @ xover[1], HP  @ xover[2]
//
// LR4 filters are used so that reconstructed output (sum of all bands) is
// frequency-flat with linear phase behaviour.  LR4 = two cascaded identical
// Butterworth 2nd-order biquads at Q = 1/√2.
// ─────────────────────────────────────────────────────────────────────────────

struct BandConfig {
    float threshold_db  = -20.0f;
    float ratio         =   4.0f;
    float attack_ms     =   5.0f;
    float release_ms    = 100.0f;
    float makeup_db     =   0.0f;
    bool  muted         = false;
    bool  soloed        = false;
};

class MultibandCompressor {
public:
    static constexpr int NUM_BANDS    = 4;
    static constexpr int NUM_XOVERS   = 3;

    explicit MultibandCompressor(float sample_rate);

    void prepare(float sample_rate);
    void reset() noexcept;

    void set_crossover(int index, float hz) noexcept;
    void set_band(int index, BandConfig cfg) noexcept;

    void process(float* left, float* right, int num_samples) noexcept;

private:
    float sample_rate_;
    float xover_hz_[NUM_XOVERS];
    BandConfig bands_[NUM_BANDS];

    // Per-crossover LR4 filters — separate LP and HP instances.
    // LP splits the signal below the crossover; HP keeps everything above.
    Lr4Filter lp_[NUM_XOVERS];  // low-pass at each crossover
    Lr4Filter hp_[NUM_XOVERS];  // high-pass at each crossover

    // Pre-allocated split band buffers — avoids any heap use inside process().
    std::array<float, MAX_BLOCK_SIZE> band_l_[NUM_BANDS];
    std::array<float, MAX_BLOCK_SIZE> band_r_[NUM_BANDS];

    // Per-band envelope and gain state.
    float env_[NUM_BANDS]  = {};  // peak envelope
    float gain_[NUM_BANDS] = {};  // current smoothed gain (linear)

    void rebuild_filters() noexcept;
    void compute_band_gain(int band, float env_val, float& gain_out) noexcept;
};
