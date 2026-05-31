// GridDefinition.cpp  --  Grid type enumeration and note-value definitions.
// ==========================================================================
// Builds the complete lookup table of all grid values at program startup.
// See GridDefinition.h for the data model description.

#include "GridDefinition.h"
#include <algorithm>
#include <stdexcept>

// ── Build the static grid table ──────────────────────────────────────────────
// Straight-note durations in ticks at 960 PPQN:
//   1/1   = 4 * 960 = 3840
//   1/2   = 2 * 960 = 1920
//   1/4   =     960 = 960
//   1/8   =     960 / 2 = 480
//   1/16  =     960 / 4 = 240
//   1/32  =     960 / 8 = 120
//   1/64  =     960 / 16 = 60
//   1/128 =     960 / 32 = 30
//
// Triplet  = (2/3) * straight_ticks
// Dotted   = (3/2) * straight_ticks

static std::vector<GridValue> build_grid_table()
{
    std::vector<GridValue> table;
    table.reserve(24);

    // Helper: push a straight note value.
    auto add_straight = [&](int division, int64_t ticks)
    {
        GridValue g;
        g.label    = "1/" + std::to_string(division);
        g.type     = GridType::Straight;
        g.division = division;
        g.ticks    = ticks;
        g.beats    = static_cast<double>(ticks) / PPQN;
        table.push_back(g);
    };

    // Helper: push a triplet note value (2/3 of straight).
    auto add_triplet = [&](int division, int64_t straight_ticks)
    {
        // Use exact integer: multiply by 2 first then divide by 3.
        int64_t t = (straight_ticks * 2) / 3;
        GridValue g;
        g.label    = "1/" + std::to_string(division) + "T";
        g.type     = GridType::Triplet;
        g.division = division;
        g.ticks    = t;
        g.beats    = static_cast<double>(t) / PPQN;
        table.push_back(g);
    };

    // Helper: push a dotted note value (3/2 of straight).
    auto add_dotted = [&](int division, int64_t straight_ticks)
    {
        int64_t t = (straight_ticks * 3) / 2;
        GridValue g;
        g.label    = "1/" + std::to_string(division) + "D";
        g.type     = GridType::Dotted;
        g.division = division;
        g.ticks    = t;
        g.beats    = static_cast<double>(t) / PPQN;
        table.push_back(g);
    };

    // ── Straight notes (1/1 to 1/128) ────────────────────────────────────────
    // Whole note: 4 beats * 960 = 3840 ticks
    add_straight(1,   3840);
    add_straight(2,   1920);
    add_straight(4,    960);
    add_straight(8,    480);
    add_straight(16,   240);
    add_straight(32,   120);
    add_straight(64,    60);
    add_straight(128,   30);

    // ── Triplet notes (1/4T to 1/64T) ─────────────────────────────────────────
    // Only triplets from quarter note downwards are practically usable.
    add_triplet(4,    960);   // 1/4T  = 640 ticks = 2/3 beat
    add_triplet(8,    480);   // 1/8T  = 320 ticks = 1/3 beat
    add_triplet(16,   240);   // 1/16T = 160 ticks = 1/6 beat
    add_triplet(32,   120);   // 1/32T =  80 ticks = 1/12 beat
    add_triplet(64,    60);   // 1/64T =  40 ticks = 1/24 beat

    // ── Dotted notes (1/4D to 1/64D) ──────────────────────────────────────────
    add_dotted(4,    960);    // 1/4D  = 1440 ticks = 1.5  beats
    add_dotted(8,    480);    // 1/8D  =  720 ticks = 0.75 beats
    add_dotted(16,   240);    // 1/16D =  360 ticks = 0.375 beats
    add_dotted(32,   120);    // 1/32D =  180 ticks = 0.1875 beats
    add_dotted(64,    60);    // 1/64D =   90 ticks = 0.09375 beats

    // ── Free / tick grid ──────────────────────────────────────────────────────
    // One PPQN tick — finest resolution: 1/960 of a beat.
    {
        GridValue g;
        g.label    = "Free";
        g.type     = GridType::Free;
        g.division = PPQN;
        g.ticks    = 1;
        g.beats    = 1.0 / PPQN;
        table.push_back(g);
    }

    return table;
}

// ── Static table (built once on first access) ────────────────────────────────
const std::vector<GridValue>& GridDefinition::all_grids()
{
    static const std::vector<GridValue> table = build_grid_table();
    return table;
}

const GridValue* GridDefinition::find(const std::string& label)
{
    for (const GridValue& g : all_grids()) {
        if (g.label == label)
            return &g;
    }
    return nullptr;
}

double GridDefinition::ticks_to_beats(int64_t ticks)
{
    return static_cast<double>(ticks) / PPQN;
}

int64_t GridDefinition::beats_to_ticks(double beats)
{
    return static_cast<int64_t>(beats * PPQN);
}
