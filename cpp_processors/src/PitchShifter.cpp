/*
 * PitchShifter.cpp  --  Manual pitch shifter + harmonizer implementation
 * =======================================================================
 * See PitchShifter.h for the algorithm description.
 */

#include "PitchShifter.h"
#include <cstring>
#include <cmath>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ─────────────────────────────────────────────────────────────────────────────
// Construction & lifecycle
// ─────────────────────────────────────────────────────────────────────────────

PitchShifter::PitchShifter(float sample_rate) {
    prepare(sample_rate);
}

void PitchShifter::prepare(float sample_rate) {
    sample_rate_ = (sample_rate > 0.f) ? sample_rate : 44100.f;
    _recompute_ratios();
    reset();
}

void PitchShifter::reset() {
    std::fill(std::begin(bufl_), std::end(bufl_), 0.f);
    std::fill(std::begin(bufr_), std::end(bufr_), 0.f);
    std::fill(std::begin(mxfl_), std::end(mxfl_), 0.f);
    std::fill(std::begin(mxfr_), std::end(mxfr_), 0.f);
    std::fill(std::begin(hxfl_), std::end(hxfl_), 0.f);
    std::fill(std::begin(hxfr_), std::end(hxfr_), 0.f);

    wpos_  = CIRC / 2;
    mrpos_ = 0.f;
    hrpos_ = 0.f;
    mxfp_  = XFADE;
    hxfp_  = XFADE;
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void PitchShifter::set_semitones(int semitones) {
    semitones_ = std::max(-12, std::min(12, semitones));
    _recompute_ratios();
}

void PitchShifter::set_cents(int cents) {
    cents_ = std::max(-100, std::min(100, cents));
    _recompute_ratios();
}

void PitchShifter::set_harmonizer(bool enabled) {
    harmonizer_ = enabled;
}

void PitchShifter::set_harmony_semitones(int semi) {
    harm_semi_ = std::max(-24, std::min(24, semi));
    _recompute_ratios();
}

void PitchShifter::set_mix(float mix) {
    mix_ = std::max(0.f, std::min(1.f, mix));
}

void PitchShifter::set_output_gain(float db) {
    out_gain_ = std::pow(10.f, db / 20.f);
}

void PitchShifter::_recompute_ratios() {
    /* Main voice: total cents = semitones*100 + fine cents */
    float total_cents = (float)(semitones_ * 100 + cents_);
    mratio_ = std::pow(2.f, total_cents / 1200.f);

    /* Harmony voice: harmony_semitones relative to original (unshifted). */
    float harm_cents = (float)(harm_semi_ * 100);
    hratio_ = std::pow(2.f, harm_cents / 1200.f);
}

// ─────────────────────────────────────────────────────────────────────────────
// Circular-buffer linear-interpolation read
// ─────────────────────────────────────────────────────────────────────────────

inline float PitchShifter::_read_interp(const float* buf, float pos) const noexcept {
    int   i0 = (int)pos & CIRC_MASK;
    int   i1 = (i0 + 1) & CIRC_MASK;
    float fr = pos - std::floor(pos);
    return buf[i0] + fr * (buf[i1] - buf[i0]);
}

// ─────────────────────────────────────────────────────────────────────────────
// OLA crossfade: save current trajectory, jump read pointer
// ─────────────────────────────────────────────────────────────────────────────

void PitchShifter::_start_crossfade(float& rpos, float ratio,
                                    float* xfl, float* xfr, int& xfp,
                                    int jump_samples) {
    /* Pre-compute XFADE samples from the CURRENT read position (fade-out). */
    float tmp = rpos;
    for (int i = 0; i < XFADE; ++i) {
        xfl[i] = _read_interp(bufl_, tmp);
        xfr[i] = _read_interp(bufr_, tmp);
        tmp += ratio;
        if (tmp >= (float)CIRC) tmp -= (float)CIRC;
        if (tmp <  0.f)         tmp += (float)CIRC;
    }

    /* Jump. */
    rpos += (float)jump_samples;
    if (rpos >= (float)CIRC) rpos -= (float)CIRC;
    if (rpos <  0.f)         rpos += (float)CIRC;

    xfp = 0;
}

// ─────────────────────────────────────────────────────────────────────────────
// Per-voice single-sample processing
// ─────────────────────────────────────────────────────────────────────────────

void PitchShifter::_voice_sample(float& rpos, float ratio,
                                  float* xfl, float* xfr, int& xfp,
                                  float& out_l, float& out_r) {
    if (xfp < XFADE) {
        /* Blend: old position (fade-out) → new position (fade-in). */
        float new_l = _read_interp(bufl_, rpos);
        float new_r = _read_interp(bufr_, rpos);
        float t     = (float)(xfp + 1) / (float)XFADE;
        float w_in  = 0.5f - 0.5f * std::cosf(t * (float)M_PI);
        float w_out = 1.f - w_in;
        out_l = w_out * xfl[xfp] + w_in * new_l;
        out_r = w_out * xfr[xfp] + w_in * new_r;
        ++xfp;
    } else {
        out_l = _read_interp(bufl_, rpos);
        out_r = _read_interp(bufr_, rpos);
    }

    /* Advance read pointer. */
    rpos += ratio;
    if (rpos >= (float)CIRC) rpos -= (float)CIRC;

    /* Check gap and trigger crossfade when out of range. */
    if (xfp >= XFADE) {
        float gap = (float)(wpos_ & CIRC_MASK) - rpos;
        if (gap < 0.f) gap += (float)CIRC;

        if (gap < (float)GRAIN_MIN) {
            _start_crossfade(rpos, ratio, xfl, xfr, xfp, -GRAIN_SZ);
        } else if (gap > (float)GRAIN_MAX) {
            _start_crossfade(rpos, ratio, xfl, xfr, xfp, +GRAIN_SZ);
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Main process loop
// ─────────────────────────────────────────────────────────────────────────────

void PitchShifter::process(float* left, float* right, int num_samples) {

    for (int i = 0; i < num_samples; ++i) {

        /* ── 1. Write input sample into the shared circular buffer ── */
        int wi   = wpos_ & CIRC_MASK;
        bufl_[wi] = left[i];
        bufr_[wi] = right[i];
        ++wpos_;

        /* ── 2. Main voice: pitch-shifted output ── */
        float ml, mr;
        _voice_sample(mrpos_, mratio_, mxfl_, mxfr_, mxfp_, ml, mr);

        /* ── 3. Harmony voice (optional) ── */
        float final_l = ml;
        float final_r = mr;

        if (harmonizer_) {
            float hl, hr;
            _voice_sample(hrpos_, hratio_, hxfl_, hxfr_, hxfp_, hl, hr);
            final_l += mix_ * hl;
            final_r += mix_ * hr;
        }

        /* ── 4. Apply output gain and write back ── */
        left[i]  = final_l * out_gain_;
        right[i] = final_r * out_gain_;
    }
}
