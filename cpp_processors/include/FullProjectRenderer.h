/**
 * FullProjectRenderer.h -- Stereo offline mix bus for full-project mastering.
 * ===========================================================================
 * Allocates one large stereo float32 buffer (the "mix bus") and accepts
 * individual track contributions via mix_track().  Each contribution can
 * carry static volume / pan values or time-varying AutomationProcessor
 * curves that are evaluated per-frame during the mix.
 *
 * Typical offline render pipeline
 * --------------------------------
 *  1. Python renders MIDI (FluidSynth) → (2, N) float32 PCM buffers
 *  2. Python reads audio clips from disk → (2, N) float32 PCM buffers
 *  3. Python applies per-track pedalboard FX chains to each buffer
 *  4. For each track: call mix_track() with optional AutomationProcessor
 *  5. Call get_L() / get_R() to retrieve the finished stereo mix
 *  6. Python applies mastering chain (LUFS targeting, brickwall limiter)
 *
 * Thread safety: not thread-safe.  All methods are called from one
 * Python thread (MasteringExportWorker).
 */

#pragma once

#include "AutomationProcessor.h"

#include <algorithm>
#include <vector>

class FullProjectRenderer {
public:
    FullProjectRenderer() = default;

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    /**
     * Allocate the mix bus for n_frames stereo samples at sample_rate Hz.
     * Must be called before the first mix_track() call.
     * Calling prepare() again resets and reallocates.
     */
    void prepare(int n_frames, int sample_rate);

    /**
     * Zero the mix bus without reallocating memory.
     * Useful when re-rendering the same project for a different target.
     */
    void reset();

    // ── Mixing ────────────────────────────────────────────────────────────────

    /**
     * Add one stereo track buffer to the mix bus starting at at_frame.
     *
     * Parameters
     * ----------
     * L, R         : Pointers to the left / right channel PCM data.
     *                Length must be track_frames.
     * track_frames : Number of frames in L and R.
     * at_frame     : Start position in the mix bus (0 = beginning of song).
     * volume       : Static linear gain (0 = silence, 1 = unity, 2 = +6 dB).
     *                Ignored if vol_auto is non-null and has control points.
     * pan          : Static stereo pan (-1 = full L, 0 = centre, +1 = full R).
     *                Ignored if pan_auto is non-null and has control points.
     * vol_auto     : Optional per-frame volume automation (may be nullptr).
     * pan_auto     : Optional per-frame pan    automation (may be nullptr).
     *
     * Pan law: equal-power linear approximation
     *   gain_L = volume * max(0, 1 - pan)
     *   gain_R = volume * max(0, 1 + pan)
     *
     * Frames that would extend past the mix bus boundary are silently clipped.
     */
    void mix_track(const float* L,
                   const float* R,
                   int          track_frames,
                   int          at_frame,
                   double       volume,
                   double       pan,
                   const AutomationProcessor* vol_auto,
                   const AutomationProcessor* pan_auto);

    // ── Output ────────────────────────────────────────────────────────────────

    /** Return a copy of the left  channel mix buffer. */
    std::vector<float> get_L() const { return mix_L_; }

    /** Return a copy of the right channel mix buffer. */
    std::vector<float> get_R() const { return mix_R_; }

    /** Total number of frames in the mix bus (set by prepare()). */
    int get_n_frames() const { return n_frames_; }

private:
    std::vector<float> mix_L_;      // left  channel accumulator
    std::vector<float> mix_R_;      // right channel accumulator
    int n_frames_    = 0;
    int sample_rate_ = 44100;
};
