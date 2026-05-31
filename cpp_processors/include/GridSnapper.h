// GridSnapper.h  --  High-level grid snap + ruler API for Python bindings.
// =========================================================================
// Combines GridDefinition, QuantizeEngine, and TimelineRuler into a single
// class that the Python side (and its pybind11 binding) interacts with.
//
// Typical Python usage:
//
//   snapper = dp.GridSnapper()
//   snapper.set_grid("1/16")
//   snapped_beat = snapper.snap(raw_beat)
//   lines  = snapper.grid_lines(start_beat, end_beat)
//   labels = snapper.ruler_labels(start_beat, end_beat, pixels_per_beat)

#pragma once

#include "GridDefinition.h"
#include "QuantizeEngine.h"
#include "TimelineRuler.h"
#include <string>
#include <vector>

class GridSnapper {
public:
    // ── Construction & configuration ─────────────────────────────────────────

    GridSnapper();

    // Set the active grid by label ("1/4", "1/16T", "Free" …).
    // Silently ignores unknown labels and retains the previous grid.
    void set_grid(const std::string& label);

    // Return the active grid label.
    std::string grid_label() const;

    // Return the active grid step in quarter-note beats.
    double grid_beats() const;

    // Quantize strength [0, 1].  1 = full snap, 0 = off.
    void   set_strength(double s);
    double strength() const;

    // Ruler mode (BarsBeats / Time / SMPTE).
    // Accepted string values: "BarsBeats", "Time", "SMPTE"
    void        set_ruler_mode(const std::string& mode_str);
    std::string ruler_mode_str() const;

    // SMPTE frame rate (24, 25, 29.97, 30).
    void   set_fps(double fps);
    double fps() const;

    // Tempo in BPM — used for Time and SMPTE ruler modes.
    void   set_bpm(double bpm);
    double bpm() const;

    // Beats per bar (time signature numerator, almost always 4).
    void set_time_sig(int beats_per_bar);
    int  time_sig() const;

    // ── Grid operations ───────────────────────────────────────────────────────

    // Snap a beat to the nearest grid line.
    double snap(double beat) const;

    // Return all grid-line positions in [start_beat, end_beat].
    std::vector<double> grid_lines(double start_beat, double end_beat) const;

    // ── Ruler labels ──────────────────────────────────────────────────────────

    // Return formatted ruler labels for the visible range.
    // pixels_per_beat is the piano-roll's BEAT_WIDTH constant (typically 80).
    std::vector<RulerLabel> ruler_labels(double start_beat,
                                         double end_beat,
                                         double pixels_per_beat) const;

    // Format a single beat position as a string in the current ruler mode.
    std::string format_position(double beat) const;

private:
    std::string        label_;       // active grid label
    double             grid_beats_;  // active grid step in beats
    double             strength_;    // quantize strength
    RulerMode          ruler_mode_;
    double             fps_;
    double             bpm_;
    int                time_sig_;

    // Refresh grid_beats_ from label_.
    void _refresh();
};
