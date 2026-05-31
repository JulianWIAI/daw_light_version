/*
 * TimingWeightFunction.h  --  Musical Grid Position to Velocity-Weight Mapper
 * =============================================================================
 * Maps a MIDI event's position on the musical grid to a deterministic
 * floating-point weight multiplier.  The weight is > 1.0 on structurally
 * strong beats and < 1.0 on weak offbeats, giving notes the natural dynamic
 * contour of a human performance.
 *
 * Grid classification model (4/4 default parameters):
 *
 *   Grid position     Beat class        Weight (default params)
 *   ─────────────     ──────────        ───────────────────────
 *   Beat 1 (bar 0)    DOWNBEAT          1.0 + downbeat_boost         → 1.15
 *   Beat 3            STRONG_BEAT       1.0 + downbeat_boost × 0.55  → 1.08
 *   Beats 2, 4        QUARTER_BEAT      1.0 - offbeat_reduction×0.40 → 0.97
 *   Eighth upbeats    EIGHTH_BEAT       1.0 - offbeat_reduction×1.00 → 0.92
 *   16th offbeats     SIXTEENTH_BEAT    1.0 - offbeat_reduction×1.50 → 0.88
 *
 * Snap tolerance:
 *   Notes that land within snap_tolerance beats of a grid point are
 *   classified by that point.  The weight offset is then scaled by a
 *   snap_factor (1.0 − snap_error / snap_tolerance) so a note that is
 *   half-way between the grid and the tolerance boundary gets half the
 *   boost/reduction of a perfectly on-grid note.
 *
 * Notes outside the snap tolerance are treated as off-grid and receive
 * the maximum offbeat reduction (SIXTEENTH_BEAT treatment).
 *
 * Thread safety:
 *   weight() is const and reads only cfg_.  Safe to call from multiple
 *   threads as long as set_config() is not called concurrently.
 */

#pragma once

#include <cmath>

class TimingWeightFunction {
public:
    // All user-configurable parameters in one POD struct.  Replace atomically
    // by calling set_config() with an updated copy.
    struct Config {
        int    time_sig_num      = 4;    // Beats per bar  (e.g. 4 for 4/4)
        int    time_sig_denom    = 4;    // Beat value     (e.g. 4 = quarter note)
        double downbeat_boost    = 0.15; // Weight addition on the bar's first beat [0, 1]
        double offbeat_reduction = 0.08; // Weight subtraction on offbeats [0, 1]
        double snap_tolerance    = 0.10; // Max beat distance to snap to a grid point
    };

    // Construct with optional initial config.
    explicit TimingWeightFunction(Config cfg = Config{});

    // Compute the velocity weight multiplier for a note at beat_position.
    //
    // beat_position : absolute beat from the start of the song.
    //                 0.0 = song start; 4.0 = bar 2 beat 1 in 4/4.
    //                 One unit always equals one quarter note.
    //
    // Returns a positive multiplier; typical range [0.80, 1.20].
    // Multiply the base velocity by this value before adding Gaussian noise.
    double weight(double beat_position) const;

    void          set_config(Config cfg);
    const Config& config() const;

private:
    // Beat-strength categories on a 1/16-note grid (finest supported
    // subdivision — sufficient for all common MIDI patterns).
    enum class BeatClass {
        DOWNBEAT,       // First beat of the bar — maximum accent
        STRONG_BEAT,    // Secondary structural beat (beat 3 in 4/4)
        QUARTER_BEAT,   // Quarter-note beat that is neither down nor strong
        EIGHTH_BEAT,    // Eighth-note upbeat
        SIXTEENTH_BEAT  // 16th-note offbeat — minimum accent
    };

    Config cfg_;

    // Classify a within-bar position to a BeatClass.
    // bar_pos       : position in beats within the current bar [0, beats_per_bar)
    // beats_per_bar : total beats per bar (from time signature numerator)
    // out_snap_error: receives the distance (beats) from the snapped grid point
    BeatClass classify(double bar_pos,
                       double beats_per_bar,
                       double* out_snap_error) const;
};
