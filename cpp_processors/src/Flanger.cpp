#include "Flanger.h"
#include <algorithm>
#include <cmath>

static constexpr float TWO_PI_F = 6.28318530718f;

// ─────────────────────────────────────────────────────────────────────────────
// Construction / preparation
// ─────────────────────────────────────────────────────────────────────────────

Flanger::Flanger(float sample_rate)
    : sample_rate_(sample_rate)
    , rate_hz_(0.5f)
    , depth_ms_(2.0f)
    , center_ms_(5.0f)
    , feedback_(0.5f)
    , wet_(0.5f), dry_(0.5f)
    , waveform_(0)          // sine
    , stereo_width_(0.5f)   // 90° offset — natural stereo
    , write_pos_(0)
    , lfo_phase_l_(0.0f)
    , lfo_phase_r_(0.0f)    // will be set in prepare()
    , fb_l_(0.0f), fb_r_(0.0f)
{
    buf_l_.fill(0.0f);
    buf_r_.fill(0.0f);
    prepare(sample_rate);
}

void Flanger::prepare(float sample_rate) {
    sample_rate_ = sample_rate;
    // R LFO starts at (stereo_width × π) radians = (stereo_width × 0.5) cycles
    lfo_phase_l_ = 0.0f;
    lfo_phase_r_ = stereo_width_ * 0.5f;  // fraction of one cycle
    reset();
}

void Flanger::reset() noexcept {
    buf_l_.fill(0.0f);
    buf_r_.fill(0.0f);
    write_pos_ = 0;
    lfo_phase_l_ = 0.0f;
    lfo_phase_r_ = stereo_width_ * 0.5f;
    fb_l_ = fb_r_ = 0.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void Flanger::set_rate(float hz) noexcept {
    rate_hz_ = std::max(0.01f, hz);
}

void Flanger::set_depth(float ms) noexcept {
    depth_ms_ = std::max(0.0f, ms);
}

void Flanger::set_center(float ms) noexcept {
    // Clamp: center must leave room for depth modulation within the buffer.
    center_ms_ = std::max(0.1f, std::min(ms, 8.0f));
}

void Flanger::set_feedback(float f) noexcept {
    // ±0.95 clamp keeps the system stable for all waveform shapes.
    feedback_ = std::max(-0.95f, std::min(f, 0.95f));
}

void Flanger::set_wet(float w) noexcept {
    wet_ = std::max(0.0f, std::min(w, 1.0f));
    dry_ = 1.0f - wet_;
}

void Flanger::set_waveform(int w) noexcept {
    waveform_ = std::max(0, std::min(w, 2));
}

void Flanger::set_stereo_width(float w) noexcept {
    stereo_width_ = std::max(0.0f, std::min(w, 1.0f));
    // Update R phase immediately so the new width takes effect from next sample.
    lfo_phase_r_ = lfo_phase_l_ + stereo_width_ * 0.5f;
    if (lfo_phase_r_ >= 1.0f) lfo_phase_r_ -= 1.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

float Flanger::_lfo(float phase) const noexcept {
    switch (waveform_) {
        case 1: {
            // Triangle: peak at 0.25, trough at 0.75.
            const float t = phase - 0.25f;
            const float abs_t = t < 0.0f ? -t : t;
            return 1.0f - 4.0f * abs_t;
        }
        case 2:
            // Square: clean on/off.
            return phase < 0.5f ? 1.0f : -1.0f;
        default:
            // Sine (case 0 and fallback).
            return std::sin(TWO_PI_F * phase);
    }
}

float Flanger::_read_interp(const std::array<float, FLANGER_BUF_SIZE>& buf,
                             float delay_samps) const noexcept {
    // Linear interpolation — sufficient precision for short flanger delays.
    const auto  ui   = static_cast<unsigned>(write_pos_);
    const auto  d    = static_cast<unsigned>(delay_samps);
    const float frac = delay_samps - static_cast<float>(d);

    const float y0 = buf[(ui - d     ) & FLANGER_BUF_MASK];
    const float y1 = buf[(ui - d - 1u) & FLANGER_BUF_MASK];
    return y0 + frac * (y1 - y0);
}

// ─────────────────────────────────────────────────────────────────────────────
// Main processing loop
// ─────────────────────────────────────────────────────────────────────────────

void Flanger::process(float* left, float* right, int num_samples) noexcept {
    const float sr     = sample_rate_;
    const float phase_inc = rate_hz_ / sr;

    for (int i = 0; i < num_samples; ++i) {
        // ── Compute modulated delay times in samples ──────────────────────────
        // LFO output ∈ [−1, +1]; scaled to ms then converted to samples.
        const float lfo_l = _lfo(lfo_phase_l_);
        const float lfo_r = _lfo(lfo_phase_r_);

        float d_l = (center_ms_ + depth_ms_ * lfo_l) * sr / 1000.0f;
        float d_r = (center_ms_ + depth_ms_ * lfo_r) * sr / 1000.0f;

        // Clamp to valid buffer range (at least 1 sample, at most BUF-2).
        d_l = std::max(1.0f, std::min(d_l, float(FLANGER_BUF_SIZE - 2)));
        d_r = std::max(1.0f, std::min(d_r, float(FLANGER_BUF_SIZE - 2)));

        // ── Write input + feedback into the delay buffer ──────────────────────
        buf_l_[write_pos_] = left[i]  + feedback_ * fb_l_ + DENORMAL_GUARD;
        buf_r_[write_pos_] = right[i] + feedback_ * fb_r_ + DENORMAL_GUARD;

        // ── Read back the modulated delay ─────────────────────────────────────
        const float delay_l = _read_interp(buf_l_, d_l);
        const float delay_r = _read_interp(buf_r_, d_r);

        // Store for next sample's feedback path.
        fb_l_ = delay_l;
        fb_r_ = delay_r;

        write_pos_ = (write_pos_ + 1) & FLANGER_BUF_MASK;

        // ── Wet/dry mix ───────────────────────────────────────────────────────
        left[i]  = dry_ * left[i]  + wet_ * delay_l;
        right[i] = dry_ * right[i] + wet_ * delay_r;

        // ── Advance LFO phases ────────────────────────────────────────────────
        lfo_phase_l_ += phase_inc;
        lfo_phase_r_ += phase_inc;
        if (lfo_phase_l_ >= 1.0f) lfo_phase_l_ -= 1.0f;
        if (lfo_phase_r_ >= 1.0f) lfo_phase_r_ -= 1.0f;
    }
}
