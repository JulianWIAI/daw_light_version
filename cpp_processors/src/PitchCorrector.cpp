/*
 * PitchCorrector.cpp  --  Auto-Tune style pitch correction implementation
 * ========================================================================
 * See PitchCorrector.h for the full algorithm description.
 */

#include "PitchCorrector.h"
#include <cstring>
#include <cmath>
#include <limits>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// ─────────────────────────────────────────────────────────────────────────────
// Construction & lifecycle
// ─────────────────────────────────────────────────────────────────────────────

PitchCorrector::PitchCorrector(float sample_rate) {
    prepare(sample_rate);
}

void PitchCorrector::prepare(float sample_rate) {
    sample_rate_ = (sample_rate > 0.f) ? sample_rate : 44100.f;
    _update_smooth_coeff();
    reset();
}

void PitchCorrector::reset() {
    /* Clear all audio buffers and state. */
    std::fill(std::begin(abuf_),   std::end(abuf_),   0.f);
    std::fill(std::begin(yin_d_),  std::end(yin_d_),  0.f);
    std::fill(std::begin(yin_cm_), std::end(yin_cm_), 0.f);
    std::fill(std::begin(yin_tmp_),std::end(yin_tmp_),0.f);
    std::fill(std::begin(bufl_),   std::end(bufl_),   0.f);
    std::fill(std::begin(bufr_),   std::end(bufr_),   0.f);
    std::fill(std::begin(xfl_),    std::end(xfl_),    0.f);
    std::fill(std::begin(xfr_),    std::end(xfr_),    0.f);

    awrite_ = 0;
    hop_ctr_ = 0;
    detected_hz_ = 0.f;
    target_hz_   = 0.f;
    period_samps_= 512;
    wpos_  = CIRC / 2;   // prime with half-buffer latency so read can lag write
    rpos_  = 0.f;
    ratio_ = 1.f;
    xfp_   = XFADE;      // crossfade inactive
}

// ─────────────────────────────────────────────────────────────────────────────
// Parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void PitchCorrector::set_scale(int scale) {
    scale_type_ = scale;
}

void PitchCorrector::set_root(int root) {
    root_note_ = root & 11;   // clamp to 0..11
}

void PitchCorrector::set_retune_speed(float speed) {
    retune_speed_ = std::max(0.f, std::min(1.f, speed));
    _update_smooth_coeff();
}

void PitchCorrector::set_amount(float amount) {
    amount_ = std::max(0.f, std::min(1.f, amount));
}

void PitchCorrector::set_output_gain(float db) {
    out_gain_ = std::pow(10.f, db / 20.f);
}

void PitchCorrector::_update_smooth_coeff() {
    /*
     * Map retune_speed_ 0..1 to a time constant 1..300 ms.
     * At speed=0: TC = 1 ms  (instant)
     * At speed=1: TC = 300 ms (very slow glide)
     * smooth_c_ = 1 - exp(-1 / (tc_samples))
     */
    float tc_ms = 1.f + retune_speed_ * 299.f;
    float tc_samples = tc_ms * 0.001f * sample_rate_;
    smooth_c_ = 1.f - std::exp(-1.f / tc_samples);
}

// ─────────────────────────────────────────────────────────────────────────────
// YIN pitch detection
// ─────────────────────────────────────────────────────────────────────────────

float PitchCorrector::_yin_detect() {
    /*
     * Linearise the circular ring buffer into yin_tmp_ so YIN sees a
     * contiguous array of YIN_N samples (oldest → newest).
     */
    for (int i = 0; i < YIN_N; ++i) {
        yin_tmp_[i] = abuf_[(awrite_ + i) & (YIN_N - 1)];
    }

    const float* x = yin_tmp_;   // shorthand

    /* Silence check: skip if RMS is below -60 dBFS. */
    float energy = 0.f;
    for (int i = 0; i < YIN_W; ++i) energy += x[i] * x[i];
    if (energy < 1e-8f) return 0.f;

    /*
     * Step 1 – Difference function:
     *   d[tau] = sum_{j=0}^{W-1} (x[j] - x[j+tau])^2
     * Only compute tau = 1 .. YIN_W-1 (tau=0 gives zero by definition).
     */
    yin_d_[0] = 1.f;    // CMNDF formula requires d[0] = 1
    for (int tau = 1; tau < YIN_W; ++tau) {
        float sum = 0.f;
        for (int j = 0; j < YIN_W; ++j) {
            float delta = x[j] - x[j + tau];
            sum += delta * delta;
        }
        yin_d_[tau] = sum;
    }

    /*
     * Step 2 – Cumulative Mean Normalised Difference Function (CMNDF):
     *   cm[0] = 1
     *   cm[tau] = d[tau] / ((1/tau) * sum_{j=1}^{tau} d[j])
     */
    yin_cm_[0] = 1.f;
    float running = 0.f;
    for (int tau = 1; tau < YIN_W; ++tau) {
        running += yin_d_[tau];
        yin_cm_[tau] = (running > 0.f) ? yin_d_[tau] * (float)tau / running
                                       : 1.f;
    }

    /*
     * Limit search range to musically plausible periods.
     * Min period → max 4200 Hz (highest note we care about correcting)
     * Max period → min   50 Hz (below which correction is unreliable)
     */
    int tau_min = std::max(2, (int)(sample_rate_ / 4200.f));
    int tau_max = std::min(YIN_W - 2, (int)(sample_rate_ / 50.f));

    /*
     * Step 3 – Find the first local minimum below the threshold.
     * We accept the global minimum if nothing is below YIN_TH.
     */
    int tau_star = 0;
    float best_val = std::numeric_limits<float>::max();

    for (int tau = tau_min; tau <= tau_max; ++tau) {
        if (yin_cm_[tau] < YIN_TH) {
            /* Descend to the bottom of this valley. */
            while (tau + 1 <= tau_max && yin_cm_[tau + 1] < yin_cm_[tau])
                ++tau;
            tau_star = tau;
            break;
        }
        /* Track global minimum as fallback. */
        if (yin_cm_[tau] < best_val) {
            best_val  = yin_cm_[tau];
            tau_star  = tau;
        }
    }

    if (tau_star < 2) return 0.f;   // no reliable detection

    /*
     * Step 4 – Parabolic interpolation for sub-sample period accuracy.
     */
    float better_tau = (float)tau_star;
    if (tau_star > tau_min && tau_star < tau_max) {
        float s0 = yin_cm_[tau_star - 1];
        float s1 = yin_cm_[tau_star];
        float s2 = yin_cm_[tau_star + 1];
        float denom = 2.f * (2.f * s1 - s0 - s2);
        if (std::abs(denom) > 1e-10f)
            better_tau = (float)tau_star + (s2 - s0) / denom;
    }

    return (better_tau > 0.f) ? sample_rate_ / better_tau : 0.f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Scale snapping
// ─────────────────────────────────────────────────────────────────────────────

float PitchCorrector::_snap_to_scale(float hz) const {
    if (hz <= 0.f) return hz;

    /* Scale interval tables (semitones relative to root). */
    static const int MAJOR[7]      = {0, 2, 4, 5, 7, 9, 11};
    static const int MINOR[7]      = {0, 2, 3, 5, 7, 8, 10};
    static const int CHROMATIC[12] = {0,1,2,3,4,5,6,7,8,9,10,11};

    const int* intervals = nullptr;
    int n_ivl = 0;
    switch (scale_type_) {
        case 0:  intervals = MAJOR;      n_ivl = 7;  break;
        case 1:  intervals = MINOR;      n_ivl = 7;  break;
        default: intervals = CHROMATIC;  n_ivl = 12; break;
    }

    /* Convert Hz to a fractional MIDI note number (A4 = 440 Hz = MIDI 69). */
    float midi = 69.f + 12.f * std::log2f(hz / 440.f);

    /* Find the nearest scale note across a wide MIDI range (0..127). */
    float min_dist    = 1e9f;
    float nearest_midi = std::roundf(midi);

    for (int octave = -1; octave <= 10; ++octave) {
        for (int i = 0; i < n_ivl; ++i) {
            float note = (float)(root_note_ + intervals[i] + octave * 12);
            float dist = std::abs(midi - note);
            if (dist < min_dist) {
                min_dist     = dist;
                nearest_midi = note;
            }
        }
    }

    /* Convert nearest MIDI back to Hz. */
    return 440.f * std::pow(2.f, (nearest_midi - 69.f) / 12.f);
}

// ─────────────────────────────────────────────────────────────────────────────
// Circular-buffer read with linear interpolation
// ─────────────────────────────────────────────────────────────────────────────

inline float PitchCorrector::_read_interp(const float* buf, float pos) const noexcept {
    int   i0 = (int)pos & CIRC_MASK;
    int   i1 = (i0 + 1) & CIRC_MASK;
    float fr = pos - std::floor(pos);
    return buf[i0] + fr * (buf[i1] - buf[i0]);
}

// ─────────────────────────────────────────────────────────────────────────────
// OLA crossfade: save current trajectory, then jump the read pointer
// ─────────────────────────────────────────────────────────────────────────────

void PitchCorrector::_start_crossfade(int jump_samples) {
    /*
     * Pre-compute XFADE output samples from the CURRENT (old) read position.
     * These become the fade-out signal while the new position fades in.
     */
    float tmp = rpos_;
    for (int i = 0; i < XFADE; ++i) {
        xfl_[i] = _read_interp(bufl_, tmp);
        xfr_[i] = _read_interp(bufr_, tmp);
        tmp += ratio_;
        if (tmp >= (float)CIRC) tmp -= (float)CIRC;
        if (tmp <  0.f)         tmp += (float)CIRC;
    }

    /* Jump the read pointer. */
    rpos_ += (float)jump_samples;
    if (rpos_ >= (float)CIRC) rpos_ -= (float)CIRC;
    if (rpos_ <  0.f)         rpos_ += (float)CIRC;

    xfp_ = 0;   // activate crossfade
}

// ─────────────────────────────────────────────────────────────────────────────
// Main process loop
// ─────────────────────────────────────────────────────────────────────────────

void PitchCorrector::process(float* left, float* right, int num_samples) {

    for (int i = 0; i < num_samples; ++i) {

        /* ── 1. Write input into circular pitch-shift buffer ── */
        int wi = wpos_ & CIRC_MASK;
        bufl_[wi] = left[i];
        bufr_[wi] = right[i];
        ++wpos_;

        /* ── 2. Feed mono mix into YIN analysis ring buffer ── */
        abuf_[awrite_ & (YIN_N - 1)] = (left[i] + right[i]) * 0.5f;
        ++awrite_;

        /* ── 3. Run YIN every YIN_HOP samples ── */
        if (++hop_ctr_ >= YIN_HOP) {
            hop_ctr_ = 0;
            float det = _yin_detect();

            if (det > 0.f) {
                detected_hz_  = det;
                float snapped = _snap_to_scale(det);
                target_hz_    = snapped;

                /* Period estimate used for crossfade jump size. */
                period_samps_ = std::max(64,
                    std::min(CIRC / 4, (int)(sample_rate_ / det)));

                /* Compute desired correction ratio, blended by amount_. */
                float full_ratio = snapped / det;
                float desired_ratio = 1.f + amount_ * (full_ratio - 1.f);

                /* Smoothly approach desired_ratio at retune_speed. */
                ratio_ += smooth_c_ * (desired_ratio - ratio_);
            } else {
                /* Unvoiced — decay ratio back to 1.0 quickly. */
                ratio_ += smooth_c_ * 4.f * (1.f - ratio_);
            }
        }

        /* ── 4. Read output from circular buffer ── */
        float out_l, out_r;

        if (xfp_ < XFADE) {
            /* Active crossfade: blend fade-out (old pos) with fade-in (new pos). */
            float new_l = _read_interp(bufl_, rpos_);
            float new_r = _read_interp(bufr_, rpos_);
            float t     = (float)(xfp_ + 1) / (float)XFADE;
            /* Hanning window: w_in goes 0→1, w_out goes 1→0. */
            float w_in  = 0.5f - 0.5f * std::cosf(t * (float)M_PI);
            float w_out = 1.f - w_in;
            out_l = w_out * xfl_[xfp_] + w_in * new_l;
            out_r = w_out * xfr_[xfp_] + w_in * new_r;
            ++xfp_;
        } else {
            out_l = _read_interp(bufl_, rpos_);
            out_r = _read_interp(bufr_, rpos_);
        }

        /* ── 5. Advance read pointer ── */
        rpos_ += ratio_;
        if (rpos_ >= (float)CIRC) rpos_ -= (float)CIRC;

        /* ── 6. Check gap and trigger crossfade if needed ── */
        if (xfp_ >= XFADE) {
            /* Gap = write_pos − read_pos (in circular space). */
            float gap = (float)(wpos_ & CIRC_MASK) - rpos_;
            if (gap < 0.f) gap += (float)CIRC;

            if (gap < (float)GRAIN_MIN) {
                /* Read is catching up to write (pitch up): jump back. */
                _start_crossfade(-period_samps_);
            } else if (gap > (float)GRAIN_MAX) {
                /* Read has fallen too far behind (pitch down): jump forward. */
                _start_crossfade(period_samps_);
            }
        }

        /* ── 7. Write output (apply output trim gain) ── */
        left[i]  = out_l * out_gain_;
        right[i] = out_r * out_gain_;
    }
}
