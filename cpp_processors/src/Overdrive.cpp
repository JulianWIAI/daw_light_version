#include "Overdrive.h"
#include <algorithm>
#include <cmath>

// ─────────────────────────────────────────────────────────────────────────────
// Construction / preparation
// ─────────────────────────────────────────────────────────────────────────────

Overdrive::Overdrive(float sample_rate)
    : sample_rate_(sample_rate)
    , mode_(0)              // OVERDRIVE by default
    , pregain_linear_(1.0f) // 0 dB
    , output_linear_(1.0f)
    , tone_type_(0)         // low-pass
    , tone_hz_(3500.0f)
{
    prepare(sample_rate);
}

void Overdrive::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    _rebuild_tone();
    reset();
}

void Overdrive::reset() noexcept {
    tone_filter_.reset();
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void Overdrive::set_mode(int mode) noexcept {
    mode_ = dsp::clamp(mode, 0, 2);
}

void Overdrive::set_pregain(float db) noexcept {
    pregain_linear_ = dsp::db_to_linear(dsp::clamp(db, 0.0f, 60.0f));
}

void Overdrive::set_tone(float hz) noexcept {
    tone_hz_ = dsp::clamp(hz, 200.0f, 8000.0f);
    _rebuild_tone();
}

void Overdrive::set_tone_type(int type) noexcept {
    tone_type_ = dsp::clamp(type, 0, 2);
    _rebuild_tone();
}

void Overdrive::set_output(float db) noexcept {
    output_linear_ = dsp::db_to_linear(dsp::clamp(db, -24.0f, 6.0f));
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

void Overdrive::_rebuild_tone() noexcept {
    switch (tone_type_) {
        case 1:
            // High-pass: removes lows before clipping → tighter distortion.
            tone_filter_.coeffs = biquad::make_highpass(tone_hz_, 0.70711f, sample_rate_);
            break;
        case 2:
            // High-shelf +6 dB: tilts spectrum upward for presence/air.
            tone_filter_.coeffs = biquad::make_high_shelf(tone_hz_, 6.0f, sample_rate_);
            break;
        default:
            // Low-pass: removes highs for warmer, smoother distortion.
            tone_filter_.coeffs = biquad::make_lowpass(tone_hz_, 0.70711f, sample_rate_);
            break;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Main processing loop
// ─────────────────────────────────────────────────────────────────────────────

void Overdrive::process(float* left, float* right, int num_samples) noexcept {
    for (int i = 0; i < num_samples; ++i) {
        // ── Pre-gain ──────────────────────────────────────────────────────────
        float l = left[i]  * pregain_linear_;
        float r = right[i] * pregain_linear_;

        // ── Tone filter (before clipping — shapes which frequencies distort) ──
        l = tone_filter_.process_l(l);
        r = tone_filter_.process_r(r);

        // ── Waveshaper ────────────────────────────────────────────────────────
        switch (mode_) {
            case 1:  l = _distortion(l); r = _distortion(r); break;
            case 2:  l = _fuzz(l);       r = _fuzz(r);       break;
            default: l = _overdrive(l);  r = _overdrive(r);  break;
        }

        // ── Output trim ───────────────────────────────────────────────────────
        left[i]  = l * output_linear_;
        right[i] = r * output_linear_;
    }
}
