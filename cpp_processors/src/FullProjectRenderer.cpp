/**
 * FullProjectRenderer.cpp -- Stereo offline mix bus implementation.
 * ==================================================================
 * See FullProjectRenderer.h for the full API description.
 */

#include "FullProjectRenderer.h"

#include <algorithm>
#include <cmath>
#include <cstring>

// ── Lifecycle ─────────────────────────────────────────────────────────────────

void FullProjectRenderer::prepare(int n_frames, int sample_rate) {
    n_frames_    = n_frames;
    sample_rate_ = sample_rate > 0 ? sample_rate : 44100;

    // Allocate and zero-fill both channel buffers.
    mix_L_.assign(n_frames_, 0.0f);
    mix_R_.assign(n_frames_, 0.0f);
}

void FullProjectRenderer::reset() {
    // Re-zero without reallocating — used when the same bus is reused
    // across multiple mastering targets in the same export session.
    std::fill(mix_L_.begin(), mix_L_.end(), 0.0f);
    std::fill(mix_R_.begin(), mix_R_.end(), 0.0f);
}

// ── Mixing ────────────────────────────────────────────────────────────────────

void FullProjectRenderer::mix_track(
    const float* L,
    const float* R,
    int          track_frames,
    int          at_frame,
    double       volume,
    double       pan,
    const AutomationProcessor* vol_auto,
    const AutomationProcessor* pan_auto)
{
    // Guard: nothing to do if the track starts beyond the mix bus end.
    if (at_frame >= n_frames_ || track_frames <= 0) return;

    // Clamp to the mix bus length so we never write out of bounds.
    const int end_frame = std::min(at_frame + track_frames, n_frames_);
    const int n         = end_frame - at_frame;

    // Start time (in seconds) corresponding to at_frame.
    const double start_secs = static_cast<double>(at_frame) / sample_rate_;

    // ── Generate per-frame automation buffers ────────────────────────────────
    // Using local std::vector avoids heap allocation on subsequent calls via
    // the small-buffer optimisation on most STL implementations.
    std::vector<float> vol_buf(n);
    std::vector<float> pan_buf(n);

    if (vol_auto && vol_auto->has_points()) {
        // Per-frame volume from automation curve.
        vol_auto->fill_buffer(vol_buf.data(), n, start_secs,
                              static_cast<double>(sample_rate_));
    } else {
        // Constant volume — fill once.
        std::fill(vol_buf.begin(), vol_buf.end(),
                  static_cast<float>(volume));
    }

    if (pan_auto && pan_auto->has_points()) {
        // Per-frame pan from automation curve.
        pan_auto->fill_buffer(pan_buf.data(), n, start_secs,
                              static_cast<double>(sample_rate_));
    } else {
        // Constant pan.
        std::fill(pan_buf.begin(), pan_buf.end(),
                  static_cast<float>(pan));
    }

    // ── Accumulate into the mix bus ──────────────────────────────────────────
    // Pan law: equal-power linear approximation.
    //   g_L = vol * max(0, 1 - pan)
    //   g_R = vol * max(0, 1 + pan)
    // This gives unity gain for a centred signal and full attenuation for a
    // hard-panned signal on the opposite side.
    float* out_L = mix_L_.data() + at_frame;
    float* out_R = mix_R_.data() + at_frame;

    for (int i = 0; i < n; ++i) {
        const float v   = vol_buf[i];
        const float p   = pan_buf[i];
        const float g_l = v * std::max(0.0f, 1.0f - p);
        const float g_r = v * std::max(0.0f, 1.0f + p);
        out_L[i] += L[i] * g_l;
        out_R[i] += R[i] * g_r;
    }
}
