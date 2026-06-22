#pragma once
/*
 * MasteringFlavorProcessor.h  --  Offline mastering color / character processor
 * =============================================================================
 *
 * Applies one of three mastering "flavors" to a stereo float32 buffer.
 * Designed for the single-pass export pipeline: the processor runs ONCE on
 * the raw project mix, and every export target (MP3, WAV, stems) then branches
 * off the flavored result.
 *
 * Flavors
 * -------
 *  0  TRANSPARENT   — pass-through; no DSP applied.
 *  1  ANALOG_WARMTH — Padé tanh saturation (drive 2×, wet 6 %) blended with
 *                     a high-shelf EQ (−1 dB @ 12 kHz, S=1) to tame digital
 *                     brightness before saturation.
 *  2  CLUB_FESTIVAL — Low-shelf EQ (+2 dB @ 50 Hz) for sub-bass density,
 *                     followed by a feed-forward VCA compressor (threshold
 *                     −6 dBFS, ratio 3:1, attack 30 ms, release 50 ms,
 *                     +2 dB makeup gain).
 *
 * EQ coefficients are computed from the Audio EQ Cookbook (R. Bristow-Johnson)
 * using double-precision arithmetic; processing uses single-precision.
 *
 * Thread-safety: NOT thread-safe.  Use one instance per export run.
 */

#include <cmath>
#include <algorithm>
#include "DspHelpers.h"

enum class MasteringFlavor : int {
    TRANSPARENT   = 0,
    ANALOG_WARMTH = 1,
    CLUB_FESTIVAL = 2,
};


class MasteringFlavorProcessor {
public:
    explicit MasteringFlavorProcessor(float sample_rate = 44100.0f);

    void set_flavor(int flavor) noexcept;
    MasteringFlavor flavor()      const noexcept { return _flavor; }
    float           sample_rate() const noexcept { return _sample_rate; }

    // Reset all EQ and compressor state.
    // Call this between export targets if the same processor is reused.
    void reset() noexcept;

    // Process n_samples in-place.  Both pointers must point to buffers of at
    // least n_samples floats.  Flavor 0 is a true no-op (no writes).
    void process(float* left, float* right, int n_samples) noexcept;

private:
    // ── Biquad building block ──────────────────────────────────────────────────
    struct BiquadCoeff {
        double b0{1.0}, b1{0.0}, b2{0.0};
        double a1{0.0}, a2{0.0};   // a0 normalised to 1
    };

    struct BiquadState {
        double x1{0.0}, x2{0.0}, y1{0.0}, y2{0.0};

        void reset() noexcept { x1 = x2 = y1 = y2 = 0.0; }

        float tick(float in, const BiquadCoeff& c) noexcept {
            double y = c.b0*in + c.b1*x1 + c.b2*x2 - c.a1*y1 - c.a2*y2;
            x2 = x1;  x1 = in;
            y2 = y1;  y1 = y;
            return static_cast<float>(y);
        }
    };

    // Audio EQ Cookbook shelf formulas (S = shelf slope, 1.0 = maximum slope).
    static BiquadCoeff _make_high_shelf(float f0, float dB_gain, float sr,
                                        float S = 1.0f) noexcept;
    static BiquadCoeff _make_low_shelf (float f0, float dB_gain, float sr,
                                        float S = 1.0f) noexcept;

    void _process_analog(float* L, float* R, int n) noexcept;
    void _process_club  (float* L, float* R, int n) noexcept;

    // ── State ──────────────────────────────────────────────────────────────────
    float           _sample_rate;
    MasteringFlavor _flavor{MasteringFlavor::TRANSPARENT};

    // Flavor 1 — high-shelf EQ (−1 dB @ 12 kHz)
    BiquadCoeff _hs;
    BiquadState _hs_L, _hs_R;

    // Flavor 2 — low-shelf EQ (+2 dB @ 50 Hz)
    BiquadCoeff _ls;
    BiquadState _ls_L, _ls_R;

    // Flavor 2 — VCA compressor
    float _comp_env{0.0f};           // running envelope (linear)
    float _comp_attack_c{0.0f};      // one-pole attack coefficient
    float _comp_release_c{0.0f};     // one-pole release coefficient
    float _comp_makeup_lin{1.0f};    // makeup gain (linear)

    static constexpr float COMP_THRESHOLD_DB = -6.0f;
    static constexpr float COMP_RATIO        =  3.0f;
    static constexpr float COMP_MAKEUP_DB    =  2.0f;
    static constexpr float ANALOG_DRIVE      =  2.0f;
    static constexpr float ANALOG_WET        =  0.06f;
};
