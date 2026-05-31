/*
 * TimingWeightFunction.cpp  --  Musical Grid Position Classifier
 * ===============================================================
 * See TimingWeightFunction.h for the full weight model and design notes.
 *
 * Implementation notes:
 *
 * Grid resolution:
 *   The smallest subdivision recognised is the 1/16 note (0.25 quarter-note
 *   beats).  This is sufficient for virtually all MIDI patterns; 32nd-note
 *   and triplet offsets are classified as off-grid and receive the maximum
 *   offbeat reduction.
 *
 * Snap rounding:
 *   std::round(bar_pos / SIXTEENTH) * SIXTEENTH snaps to the nearest 16th.
 *   Floating-point arithmetic at 64-bit precision keeps the snap error below
 *   1e-10 beats for any reasonable beat position value (up to ~10 000 bars).
 *
 * Modulo arithmetic:
 *   std::fmod(gp, 1.0) extracts the fractional part of a beat, i.e. the
 *   sub-quarter position.  Values < 1e-9 indicate a quarter-note boundary;
 *   values < 1e-9 after fmod(gp, 0.5) indicate an eighth-note boundary.
 *   The epsilon 1e-9 is well above accumulated floating-point error and well
 *   below any meaningful musical position difference (< 0.001 ms at 300 bpm).
 */

#include "TimingWeightFunction.h"
#include <algorithm>

// Resolution of the grid used for classification (one 16th note).
static constexpr double SIXTEENTH = 0.25;

// Floating-point epsilon used for "is this exactly on a grid line?" checks.
static constexpr double FP_EPS = 1e-9;

// ---------------------------------------------------------------------------
// Construction / configuration
// ---------------------------------------------------------------------------

TimingWeightFunction::TimingWeightFunction(Config cfg)
    : cfg_(cfg)
{}

void TimingWeightFunction::set_config(Config cfg) {
    cfg_ = cfg;
}

const TimingWeightFunction::Config& TimingWeightFunction::config() const {
    return cfg_;
}

// ---------------------------------------------------------------------------
// Beat classification
// ---------------------------------------------------------------------------

TimingWeightFunction::BeatClass
TimingWeightFunction::classify(double     bar_pos,
                                double     beats_per_bar,
                                double*    out_snap_error) const
{
    // Snap bar_pos to the nearest 1/16-note grid point.
    const double grid_pos   = std::round(bar_pos / SIXTEENTH) * SIXTEENTH;
    const double snap_error = std::abs(bar_pos - grid_pos);

    // Return the snap error to the caller so it can apply the snap factor.
    if (out_snap_error) *out_snap_error = snap_error;

    // ── Downbeat ────────────────────────────────────────────────────────────
    // Beat 1 of the bar sits at grid position 0.0.
    if (grid_pos < FP_EPS)
        return BeatClass::DOWNBEAT;

    // ── Quarter-note boundaries ─────────────────────────────────────────────
    // fmod(grid_pos, 1.0) is nearly zero at every quarter-note beat.
    const double qn_frac = std::fmod(grid_pos, 1.0);
    if (qn_frac < FP_EPS) {
        // Beat 3 in 4/4 (grid_pos == 2.0) is the "backbeat" — musically
        // strong even though it is not the downbeat.  Only recognise it
        // when the bar has at least 4 beats (time sigs like 4/4, 5/4, 6/4).
        if (beats_per_bar >= 4.0 && std::abs(grid_pos - 2.0) < FP_EPS)
            return BeatClass::STRONG_BEAT;

        // All other quarter beats (2, 4 in 4/4; 2 in 3/4; …) are plain
        // quarter beats — slightly de-emphasised relative to downbeat.
        return BeatClass::QUARTER_BEAT;
    }

    // ── Eighth-note upbeats (0.5, 1.5, 2.5, 3.5 …) ─────────────────────────
    // fmod(grid_pos, 0.5) is nearly zero at every eighth-note boundary
    // that is not already a quarter-note boundary (handled above).
    const double en_frac = std::fmod(grid_pos, 0.5);
    if (en_frac < FP_EPS)
        return BeatClass::EIGHTH_BEAT;

    // ── 16th-note offbeats (0.25, 0.75, 1.25, …) ───────────────────────────
    // Everything else on the 1/16 grid is a 16th-note offbeat.
    return BeatClass::SIXTEENTH_BEAT;
}

// ---------------------------------------------------------------------------
// Main weight calculation
// ---------------------------------------------------------------------------

double TimingWeightFunction::weight(double beat_position) const {
    // Guard against degenerate time signatures.
    const double beats_per_bar = static_cast<double>(std::max(1, cfg_.time_sig_num));

    // Fold beat_position into a per-bar position [0, beats_per_bar).
    double bar_pos = std::fmod(beat_position, beats_per_bar);
    if (bar_pos < 0.0) bar_pos += beats_per_bar;  // handle negative positions

    // Classify the position and get the distance from the nearest grid point.
    double snap_error = 0.0;
    const BeatClass bc = classify(bar_pos, beats_per_bar, &snap_error);

    // Notes further from the grid than snap_tolerance are treated as off-grid:
    // they receive the maximum offbeat reduction with no snap scaling.
    if (snap_error > cfg_.snap_tolerance)
        return 1.0 - cfg_.offbeat_reduction * 1.5;

    // Determine the raw weight offset for this beat class.
    double offset = 0.0;
    switch (bc) {
        case BeatClass::DOWNBEAT:
            // Bar's first beat — maximum accent.
            offset = +cfg_.downbeat_boost;
            break;

        case BeatClass::STRONG_BEAT:
            // Beat 3 in 4/4 — secondary accent (backbeat), ~55% of downbeat.
            offset = +cfg_.downbeat_boost * 0.55;
            break;

        case BeatClass::QUARTER_BEAT:
            // Beats 2, 4 — slightly de-emphasised (weak beats).
            offset = -cfg_.offbeat_reduction * 0.40;
            break;

        case BeatClass::EIGHTH_BEAT:
            // Eighth upbeats — noticeably lighter than quarter beats.
            offset = -cfg_.offbeat_reduction * 1.00;
            break;

        case BeatClass::SIXTEENTH_BEAT:
            // 16th offbeats — weakest subdivision on the grid.
            offset = -cfg_.offbeat_reduction * 1.50;
            break;
    }

    // Scale the offset by how precisely on-grid the note lands.
    //   snap_factor = 1.0 when snap_error = 0  (exactly on the grid)
    //   snap_factor = 0.0 when snap_error = snap_tolerance  (at the edge)
    // This gives a smooth transition from full accent to no accent as notes
    // drift away from the grid — capturing the character of laid-back or
    // rushed playing without hard discontinuities.
    const double snap_factor = 1.0 - snap_error / cfg_.snap_tolerance;
    offset *= std::max(0.0, snap_factor);

    // Final weight centred on 1.0 — always positive; typical range [0.80, 1.20].
    return 1.0 + offset;
}
