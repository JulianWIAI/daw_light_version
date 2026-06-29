/**
 * TelemetryNoiseFloor.h
 *
 * Header-only spectral noise-floor calibrator for the C++ telemetry pipeline.
 *
 * Purpose
 * -------
 * The TelemetryAnalyzer computes FFT-based band energies and chroma relative
 * to the global spectral mean.  Even -80 dBFS ambient system noise (ADC noise,
 * fan interference, EMI) produces non-zero relative values in every band because
 * the normalisation divides all bins by the same near-zero denominator.
 *
 * TelemetryNoiseFloor accumulates FFT magnitude spectra from silence periods
 * and subtracts the resulting per-bin baseline from active-signal spectra
 * before any band or chroma computation takes place.
 *
 * Algorithm
 * ---------
 * 1. Caller feeds FFT magnitude frames from silence periods via add_frame().
 * 2. An exponential moving average (EMA, α = ALPHA = 0.05) builds a per-bin
 *    baseline over at least MIN_FRAMES frames (≈ 0.93 s at 44100/2048).
 * 3. During the MIN_FRAMES warm-up the per-bin minimum of EMA and the new
 *    frame is used instead of the plain EMA to prevent transient spikes
 *    (clicks, impulses during "silence") from inflating the floor.
 * 4. subtract() applies the stored floor multiplied by OVERSUB (= 1.2) to the
 *    live spectrum and half-wave-rectifies the result (max(0, x)).
 *
 * Thread safety
 * -------------
 * This class is NOT thread-safe.  All methods must be called from the same
 * thread — the TelemetryAnalyzer background worker thread.
 *
 * This matches the Python counterpart TelemetryNoiseFloorCalibrator in
 * core/telemetry_noise_floor.py.  Any constant changes should be kept in sync.
 */

#pragma once

#include <algorithm>   // std::fill, std::copy, std::min
#include <vector>

class TelemetryNoiseFloor {
public:
    // ── Tunable constants ─────────────────────────────────────────────────────

    /** Minimum silence frames required before the floor is considered reliable.
     *  At 44100 Hz / 2048-sample FFT: 20 × 46.4 ms ≈ 0.93 s calibration time. */
    static constexpr int   MIN_FRAMES = 20;

    /** EMA smoothing coefficient (α).  Smaller = slower adaptation, more stable
     *  floor under slowly changing environments (e.g. fan speed, temperature). */
    static constexpr float ALPHA      = 0.05f;

    /** Over-subtraction multiplier applied to the stored floor before subtract().
     *  1.2 = 20 % over-compensation; ensures a clean residual rather than
     *  spectral valleys caused by exact or under-subtraction. */
    static constexpr float OVERSUB    = 1.2f;

    // ── Construction ──────────────────────────────────────────────────────────

    /** @param n_bins  number of FFT magnitude bins (= FFT_SIZE / 2 + 1) */
    explicit TelemetryNoiseFloor(int n_bins)
        : _n_bins(n_bins)
        , _floor(static_cast<std::size_t>(n_bins), 0.f)
        , _frames(0)
    {}

    // ── Public interface ───────────────────────────────────────────────────────

    /**
     * Feed one silence-period FFT magnitude spectrum into the floor estimate.
     *
     * Must be called only when the input RMS indicates genuine silence (i.e.
     * above the "effectively zero" epsilon but below the gate open threshold).
     * It should NOT be called when the input is all-zeros (gate-closed zeros
     * pushed by the Python TelemetryNoiseGate) — doing so would drive the
     * floor toward zero and defeat the calibration.
     *
     * @param mags  pointer to _n_bins float magnitudes (background thread only)
     */
    void add_frame(const float* mags) {
        if (_frames == 0) {
            // Bootstrap: initialise floor directly from the first silence frame.
            std::copy(mags, mags + _n_bins, _floor.begin());

        } else if (_frames < MIN_FRAMES) {
            // Warm-up: use per-bin minimum of EMA and new frame to suppress
            // transient spikes that could inflate the floor estimate.
            for (int i = 0; i < _n_bins; ++i) {
                const float ema = ALPHA * mags[i] + (1.f - ALPHA) * _floor[i];
                _floor[i] = (ema < mags[i]) ? ema : mags[i];
            }

        } else {
            // Steady state: standard exponential moving average.
            for (int i = 0; i < _n_bins; ++i)
                _floor[i] = ALPHA * mags[i] + (1.f - ALPHA) * _floor[i];
        }

        ++_frames;
    }

    /**
     * Subtract the stored noise floor from mags[] and write the result to out[].
     *
     * Applies OVERSUB to the stored floor before subtraction and half-wave
     * rectifies the output so no bin goes below zero.
     *
     * If not yet calibrated (frame_count() < MIN_FRAMES) out[] is a plain copy
     * of mags[] — the function is always safe to call regardless of state.
     *
     * @param mags  input FFT magnitude spectrum (_n_bins floats)
     * @param out   output buffer (_n_bins floats; may equal mags for in-place use)
     */
    void subtract(const float* mags, float* out) const {
        if (_frames < MIN_FRAMES) {
            // Not yet calibrated — pass through unchanged.
            if (out != mags)
                std::copy(mags, mags + _n_bins, out);
            return;
        }

        for (int i = 0; i < _n_bins; ++i) {
            const float residual = mags[i] - _floor[i] * OVERSUB;
            out[i] = (residual > 0.f) ? residual : 0.f;
        }
    }

    // ── State queries ──────────────────────────────────────────────────────────

    /** Returns true once at least MIN_FRAMES silence frames have been seen. */
    bool is_calibrated() const noexcept { return _frames >= MIN_FRAMES; }

    /** Number of silence frames accumulated since the last reset(). */
    int frame_count() const noexcept { return _frames; }

    // ── Reset ──────────────────────────────────────────────────────────────────

    /** Reset the floor to all-zeros and discard calibration history.
     *  Call when the audio device changes or the project is reloaded. */
    void reset() noexcept {
        std::fill(_floor.begin(), _floor.end(), 0.f);
        _frames = 0;
    }

private:
    int                _n_bins;   ///< Number of FFT bins (immutable after construction)
    std::vector<float> _floor;    ///< Per-bin EMA noise floor
    int                _frames;   ///< Silence frames accumulated so far
};