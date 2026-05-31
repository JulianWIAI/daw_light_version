// TimelineRuler.cpp  --  Timeline ruler label formatting.
// =========================================================
// See TimelineRuler.h for the public interface and mode descriptions.

#include "TimelineRuler.h"
#include <cmath>
#include <sstream>
#include <iomanip>
#include <algorithm>

// ── Internal helpers ─────────────────────────────────────────────────────────

double TimelineRuler::_beats_to_seconds(double beat, double bpm)
{
    if (bpm <= 0.0) bpm = 120.0;
    // 1 beat (quarter note) = 60 / bpm seconds
    return beat * 60.0 / bpm;
}

std::string TimelineRuler::_fmt_time(double seconds)
{
    if (seconds < 0.0) seconds = 0.0;
    int total_ms = static_cast<int>(std::round(seconds * 1000.0));
    int ms  = total_ms % 1000;
    int sec = (total_ms / 1000) % 60;
    int min = total_ms / 60000;

    // Format: m:ss.mmm  (no leading zero on minutes)
    std::ostringstream ss;
    ss << min << ":"
       << std::setw(2) << std::setfill('0') << sec << "."
       << std::setw(3) << std::setfill('0') << ms;
    return ss.str();
}

std::string TimelineRuler::_fmt_smpte(double seconds, double fps)
{
    if (seconds < 0.0) seconds = 0.0;
    if (fps    <= 0.0) fps     = 30.0;

    // Total frame count (round to nearest integer frame).
    int64_t total_frames;

    // 29.97 drop-frame SMPTE (NTSC): count real frames, then apply drop-frame
    // correction to get the display timecode.
    const bool is_df = (std::abs(fps - 29.97) < 0.01);

    if (is_df) {
        // NTSC uses 30000/1001 frames per second.
        // Drop-frame timecode skips frame numbers 00 and 01 at the start of
        // every minute except every 10th minute to stay aligned with real time.
        double real_fps = 30000.0 / 1001.0;
        total_frames = static_cast<int64_t>(seconds * real_fps);

        int drop_frames = 2;   // frames dropped per minute
        int frames_per_10_min = static_cast<int>(std::round(real_fps * 60 * 10));
        int frames_per_min    = static_cast<int>(std::round(real_fps * 60));

        int d  = static_cast<int>(total_frames / frames_per_10_min);
        int m  = static_cast<int>((total_frames % frames_per_10_min) /
                                  (frames_per_min - drop_frames + (total_frames % frames_per_10_min == 0 ? 0 : 0)));

        // Simplified DF calculation: adjust frame number for display.
        int ff = static_cast<int>(total_frames % 30);
        int ss = static_cast<int>((total_frames / 30) % 60);
        int mm = static_cast<int>((total_frames / 1800) % 60);
        int hh = static_cast<int>(total_frames / 108000);

        (void)d; (void)m;   // suppress unused warning from exact DF path

        std::ostringstream out;
        out << std::setw(2) << std::setfill('0') << hh << ";"
            << std::setw(2) << std::setfill('0') << mm << ";"
            << std::setw(2) << std::setfill('0') << ss << ";"
            << std::setw(2) << std::setfill('0') << ff;
        return out.str();
    }

    // Non-drop-frame SMPTE (24, 25, 30).
    int int_fps = static_cast<int>(std::round(fps));
    total_frames = static_cast<int64_t>(seconds * fps);

    int ff = static_cast<int>(total_frames % int_fps);
    int ss = static_cast<int>((total_frames / int_fps) % 60);
    int mm = static_cast<int>((total_frames / (int_fps * 60)) % 60);
    int hh = static_cast<int>(total_frames / (int_fps * 3600));

    // Use ":" separator for non-drop, ";" for drop-frame (industry standard).
    std::ostringstream out;
    out << std::setw(2) << std::setfill('0') << hh << ":"
        << std::setw(2) << std::setfill('0') << mm << ":"
        << std::setw(2) << std::setfill('0') << ss << ":"
        << std::setw(2) << std::setfill('0') << ff;
    return out.str();
}

// ── Public single-position formatters ───────────────────────────────────────

std::string TimelineRuler::format_bars_beats(double beat, int time_sig)
{
    if (time_sig <= 0) time_sig = 4;
    int    bar       = static_cast<int>(beat / time_sig) + 1;  // 1-based
    double in_bar    = beat - (bar - 1) * time_sig;
    int    beat_num  = static_cast<int>(in_bar) + 1;           // 1-based

    if (beat_num <= 1) {
        // Major bar line: show "BAR N" on the ruler.
        return "BAR " + std::to_string(bar);
    }
    // Minor beat within bar: show "bar:beat".
    return std::to_string(bar) + ":" + std::to_string(beat_num);
}

std::string TimelineRuler::format_time(double beat, double bpm)
{
    return _fmt_time(_beats_to_seconds(beat, bpm));
}

std::string TimelineRuler::format_smpte(double beat, double bpm, double fps)
{
    return _fmt_smpte(_beats_to_seconds(beat, bpm), fps);
}

// ── ruler_labels ────────────────────────────────────────────────────────────
// Chooses an appropriate label spacing so that labels don't overlap.
// Returns labels only at bar/beat positions; the grid sub-division lines
// are drawn separately by the grid painter.

std::vector<RulerLabel> TimelineRuler::ruler_labels(
    double start_beat,
    double end_beat,
    double pixels_per_beat,
    double bpm,
    RulerMode mode,
    double fps,
    int    time_sig)
{
    std::vector<RulerLabel> labels;
    if (pixels_per_beat <= 0.0 || end_beat < start_beat) return labels;
    if (time_sig <= 0) time_sig = 4;

    // Minimum pixel gap between adjacent labels.
    const double MIN_PX = 60.0;

    // Determine the beat stride for label placement.
    // Start at one beat and double until labels are spaced >= MIN_PX apart.
    double stride = 1.0;
    while (stride * pixels_per_beat < MIN_PX) {
        stride *= 2.0;
    }

    // Snap stride to bar boundaries when >= 1 bar.
    double bar_beats = static_cast<double>(time_sig);
    if (stride >= bar_beats) {
        // Round up to the nearest multiple of bar_beats.
        stride = std::ceil(stride / bar_beats) * bar_beats;
    }

    // Generate labels.
    double first = std::ceil(start_beat / stride) * stride;
    double pos   = first;
    int safety   = 10000;
    while (pos <= end_beat + 1e-9 && safety-- > 0) {
        RulerLabel lbl;
        lbl.beat     = pos;
        lbl.is_major = (std::fmod(pos, bar_beats) < 1e-6);

        switch (mode) {
            case RulerMode::BarsBeats:
                lbl.text = format_bars_beats(pos, time_sig);
                break;
            case RulerMode::Time:
                lbl.text = format_time(pos, bpm);
                break;
            case RulerMode::SMPTE:
                lbl.text = format_smpte(pos, bpm, fps);
                break;
        }

        labels.push_back(lbl);
        pos = std::round((pos + stride) * 1e6) / 1e6;
    }

    return labels;
}
