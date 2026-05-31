// GridDefinition.h  --  Grid type enumeration and note-value definitions.
// ========================================================================
// Defines every grid variant a DAW piano roll supports:
//   Straight notes   : 1/1 through 1/128
//   Triplet notes    : 1/4T through 1/64T  (2/3 of the straight value)
//   Dotted notes     : 1/4D through 1/64D  (3/2 of the straight value)
//   Free / tick grid : one PPQN tick (finest possible resolution)
//
// All durations are expressed in two equivalent forms:
//   ticks  : integer PPQN ticks (PPQN = 960)
//   beats  : floating-point quarter-note beats (1 beat = PPQN ticks)
//
// The static helper functions convert between these two forms and provide
// a lookup table keyed by the human-readable label used in the GUI.

#pragma once

#include <string>
#include <vector>
#include <cstdint>

// ── PPQN constant (Pulses Per Quarter Note) ─────────────────────────────────
// 960 is the standard internal tick resolution.  One quarter note = 960 ticks.
static constexpr int PPQN = 960;

// ── Grid type tag ────────────────────────────────────────────────────────────
enum class GridType {
    Straight,   // plain note value (1/1, 1/2, 1/4 ...)
    Triplet,    // 2/3 of the corresponding straight value
    Dotted,     // 3/2 of the corresponding straight value
    Free        // single tick — finest resolution
};

// ── Per-value descriptor ─────────────────────────────────────────────────────
struct GridValue {
    std::string label;     // display string, e.g. "1/16", "1/8T", "1/4D", "Free"
    GridType    type;
    int         division;  // denominator of the note name (4 = quarter, 16 = 16th …)
    int64_t     ticks;     // duration in PPQN ticks
    double      beats;     // duration in quarter-note beats
};

// ── GridDefinition ───────────────────────────────────────────────────────────
class GridDefinition {
public:
    // Return the complete ordered list of all supported grid values.
    // The list is ordered: straight 1/1 … 1/128, triplets, dotted, Free.
    static const std::vector<GridValue>& all_grids();

    // Look up a grid value by its label string.
    // Returns nullptr if the label is not found.
    static const GridValue* find(const std::string& label);

    // Convert ticks to beats.
    static double ticks_to_beats(int64_t ticks);

    // Convert beats to ticks (truncates to integer).
    static int64_t beats_to_ticks(double beats);
};
