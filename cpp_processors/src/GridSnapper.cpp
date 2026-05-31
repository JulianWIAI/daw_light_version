// GridSnapper.cpp  --  High-level grid snap + ruler API.
// ========================================================
// See GridSnapper.h for the public interface.

#include "GridSnapper.h"
#include <algorithm>

// ── Constructor ───────────────────────────────────────────────────────────────
GridSnapper::GridSnapper()
    : label_("1/16")
    , grid_beats_(0.25)
    , strength_(1.0)
    , ruler_mode_(RulerMode::BarsBeats)
    , fps_(30.0)
    , bpm_(120.0)
    , time_sig_(4)
{
    _refresh();
}

// ── Grid configuration ────────────────────────────────────────────────────────
void GridSnapper::set_grid(const std::string& label)
{
    const GridValue* gv = GridDefinition::find(label);
    if (gv) {
        label_      = label;
        grid_beats_ = gv->beats;
    }
    // Unknown labels are silently ignored so existing behaviour is preserved.
}

std::string GridSnapper::grid_label() const { return label_; }
double      GridSnapper::grid_beats()  const { return grid_beats_; }

void   GridSnapper::set_strength(double s) { strength_ = std::max(0.0, std::min(1.0, s)); }
double GridSnapper::strength()             const { return strength_; }

void GridSnapper::set_ruler_mode(const std::string& mode_str)
{
    if      (mode_str == "Time")      ruler_mode_ = RulerMode::Time;
    else if (mode_str == "SMPTE")     ruler_mode_ = RulerMode::SMPTE;
    else                              ruler_mode_ = RulerMode::BarsBeats;
}

std::string GridSnapper::ruler_mode_str() const
{
    switch (ruler_mode_) {
        case RulerMode::Time:      return "Time";
        case RulerMode::SMPTE:     return "SMPTE";
        default:                   return "BarsBeats";
    }
}

void   GridSnapper::set_fps(double fps)  { fps_      = (fps > 0.0) ? fps : 30.0; }
double GridSnapper::fps()          const  { return fps_; }

void   GridSnapper::set_bpm(double bpm)  { bpm_      = (bpm > 0.0) ? bpm : 120.0; }
double GridSnapper::bpm()          const  { return bpm_; }

void GridSnapper::set_time_sig(int b) { time_sig_ = (b > 0) ? b : 4; }
int  GridSnapper::time_sig()    const { return time_sig_; }

// ── Grid operations ───────────────────────────────────────────────────────────
double GridSnapper::snap(double beat) const
{
    return QuantizeEngine::quantize(beat, grid_beats_, strength_);
}

std::vector<double> GridSnapper::grid_lines(double start_beat,
                                             double end_beat) const
{
    return QuantizeEngine::grid_positions(start_beat, end_beat, grid_beats_);
}

// ── Ruler labels ──────────────────────────────────────────────────────────────
std::vector<RulerLabel> GridSnapper::ruler_labels(double start_beat,
                                                   double end_beat,
                                                   double pixels_per_beat) const
{
    return TimelineRuler::ruler_labels(
        start_beat, end_beat, pixels_per_beat,
        bpm_, ruler_mode_, fps_, time_sig_
    );
}

std::string GridSnapper::format_position(double beat) const
{
    switch (ruler_mode_) {
        case RulerMode::Time:  return TimelineRuler::format_time(beat, bpm_);
        case RulerMode::SMPTE: return TimelineRuler::format_smpte(beat, bpm_, fps_);
        default:               return TimelineRuler::format_bars_beats(beat, time_sig_);
    }
}

// ── Internal ──────────────────────────────────────────────────────────────────
void GridSnapper::_refresh()
{
    const GridValue* gv = GridDefinition::find(label_);
    if (gv) grid_beats_ = gv->beats;
}
