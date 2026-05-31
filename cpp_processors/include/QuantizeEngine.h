// QuantizeEngine.h  --  Grid snap and quantize utilities.
// =========================================================
// All methods are stateless and take beat positions as doubles
// (one beat = one quarter note).
//
// Rounding rules for the three snap variants:
//   snap_nearest  : rounds to the closest grid point (std round-half-away).
//   snap_floor    : moves to the grid point at or before the beat.
//   snap_ceil     : moves to the grid point at or after the beat.
//
// quantize() blends the snapped position with the original using a
// strength parameter [0 = no change, 1 = full snap].
//
// grid_positions() returns every grid line position in a beat range,
// which the piano-roll renderer uses to draw the vertical grid lines.

#pragma once

#include <vector>

class QuantizeEngine {
public:
    // Round beat to the nearest grid multiple.
    // beat must be >= 0.0; result is always >= 0.0.
    static double snap_nearest(double beat, double grid_beats);

    // Round beat down to the largest multiple of grid_beats <= beat.
    static double snap_floor(double beat, double grid_beats);

    // Round beat up to the smallest multiple of grid_beats >= beat.
    static double snap_ceil(double beat, double grid_beats);

    // Blend beat toward its snapped position.
    //   strength = 0.0  → return beat unchanged
    //   strength = 1.0  → return snap_nearest(beat, grid_beats)
    //   intermediate    → linear interpolation between the two
    static double quantize(double beat, double grid_beats, double strength);

    // Return the list of all grid-line beat positions in [start_beat, end_beat].
    // The list always begins at the grid multiple >= start_beat and ends at or
    // below end_beat.  Values are rounded to 6 decimal places to suppress
    // floating-point drift when grid_beats is irrational (e.g., triplets).
    static std::vector<double> grid_positions(double start_beat,
                                              double end_beat,
                                              double grid_beats);
};
