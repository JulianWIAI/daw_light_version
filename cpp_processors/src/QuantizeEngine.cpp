// QuantizeEngine.cpp  --  Grid snap and quantize utilities.
// ===========================================================
// See QuantizeEngine.h for the public interface and usage notes.

#include "QuantizeEngine.h"
#include <cmath>
#include <algorithm>
#include <stdexcept>

// ── Internal helper: round a double to N decimal places ──────────────────────
static inline double round6(double v)
{
    // Multiplying by 1e6 then rounding reduces accumulated drift for irrational
    // grid steps (e.g., triplet = 2/3 beat, which has an infinite decimal expansion).
    return std::round(v * 1e6) / 1e6;
}

// ── snap_nearest ─────────────────────────────────────────────────────────────
double QuantizeEngine::snap_nearest(double beat, double grid_beats)
{
    if (grid_beats <= 0.0) return beat;
    // round-half-away-from-zero via std::round
    double snapped = std::round(beat / grid_beats) * grid_beats;
    return std::max(0.0, round6(snapped));
}

// ── snap_floor ───────────────────────────────────────────────────────────────
double QuantizeEngine::snap_floor(double beat, double grid_beats)
{
    if (grid_beats <= 0.0) return beat;
    double snapped = std::floor(beat / grid_beats) * grid_beats;
    return std::max(0.0, round6(snapped));
}

// ── snap_ceil ────────────────────────────────────────────────────────────────
double QuantizeEngine::snap_ceil(double beat, double grid_beats)
{
    if (grid_beats <= 0.0) return beat;
    double snapped = std::ceil(beat / grid_beats) * grid_beats;
    return std::max(0.0, round6(snapped));
}

// ── quantize ─────────────────────────────────────────────────────────────────
double QuantizeEngine::quantize(double beat, double grid_beats, double strength)
{
    if (grid_beats <= 0.0 || strength <= 0.0) return beat;
    strength = std::min(1.0, std::max(0.0, strength));
    double snapped = snap_nearest(beat, grid_beats);
    // Linear blend: beat + strength * (snapped - beat) = (1-s)*beat + s*snapped
    return beat + strength * (snapped - beat);
}

// ── grid_positions ───────────────────────────────────────────────────────────
std::vector<double> QuantizeEngine::grid_positions(double start_beat,
                                                    double end_beat,
                                                    double grid_beats)
{
    std::vector<double> positions;
    if (grid_beats <= 0.0 || end_beat < start_beat) return positions;

    // First grid line at or after start_beat.
    double first = snap_ceil(start_beat, grid_beats);

    // Estimate the number of grid lines to pre-allocate.
    int approx_count = static_cast<int>((end_beat - first) / grid_beats) + 2;
    if (approx_count > 0) positions.reserve(approx_count);

    double pos = first;
    // Safety cap: no more than 100 000 lines (prevents infinite loop on tiny grid).
    int safety = 100000;
    while (pos <= end_beat + 1e-9 && safety-- > 0) {
        positions.push_back(pos);
        pos = round6(pos + grid_beats);
    }

    return positions;
}
