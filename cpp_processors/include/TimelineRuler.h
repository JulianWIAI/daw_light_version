// TimelineRuler.h  --  Timeline ruler label formatting for the piano-roll.
// ==========================================================================
// Three display modes are supported:
//
//   BarsBeats  "BAR 1"  /  "1:2"  /  "1:3"  (bar : beat in 4/4)
//   Time       "0:00.000"  (minutes : seconds . milliseconds)
//   SMPTE      "00:00:00:00"  (HH:MM:SS:FF at a configurable frame rate)
//
// SMPTE frame rates supported: 24, 25, 29.97 (drop-frame), 30.
//
// Usage:
//   Call ruler_labels() to get the list of (beat_position, label_text,
//   is_major) triples that the painter should draw.
//
//   For formatting a single position (e.g., a playhead tooltip), use
//   the individual format_* helpers directly.

#pragma once

#include <string>
#include <vector>

// ── Ruler display mode ───────────────────────────────────────────────────────
enum class RulerMode {
    BarsBeats,  // bar / beat numbers (default, no BPM needed)
    Time,       // mm:ss.ms  (needs BPM)
    SMPTE       // HH:MM:SS:FF  (needs BPM and FPS)
};

// ── One ruler label entry ────────────────────────────────────────────────────
struct RulerLabel {
    double      beat;      // horizontal position in quarter-note beats
    std::string text;      // formatted label string
    bool        is_major;  // true = bar line (larger tick / brighter colour)
};

// ── TimelineRuler ────────────────────────────────────────────────────────────
class TimelineRuler {
public:
    // ── Single-position formatters ────────────────────────────────────────────

    // "BAR 1", "1:2", "1:3" etc.
    // beat      : absolute position in quarter-note beats
    // time_sig  : beats per bar (almost always 4 for 4/4)
    static std::string format_bars_beats(double beat, int time_sig = 4);

    // "0:00.000"
    // beat      : absolute position in quarter-note beats
    // bpm       : tempo in beats-per-minute
    static std::string format_time(double beat, double bpm);

    // "00:00:00:00"
    // beat      : absolute position in quarter-note beats
    // bpm       : tempo in beats-per-minute
    // fps       : frame rate (24, 25, 29.97, 30)
    static std::string format_smpte(double beat, double bpm, double fps);

    // ── Multi-label generator ─────────────────────────────────────────────────
    // Generate a list of ruler labels for the visible range [start_beat, end_beat].
    // The function chooses label density automatically based on pixels_per_beat so
    // that adjacent labels never overlap.
    //
    //   start_beat     : leftmost visible beat
    //   end_beat       : rightmost visible beat
    //   pixels_per_beat: BEAT_WIDTH of the piano-roll (typically 80)
    //   bpm            : tempo (used only for Time / SMPTE modes)
    //   mode           : display mode
    //   fps            : frame rate (used only for SMPTE mode)
    //   time_sig       : beats per bar (used only for BarsBeats mode)
    static std::vector<RulerLabel> ruler_labels(
        double start_beat,
        double end_beat,
        double pixels_per_beat,
        double bpm,
        RulerMode mode,
        double fps       = 30.0,
        int    time_sig  = 4
    );

private:
    // Internal: convert beats to seconds.
    static double _beats_to_seconds(double beat, double bpm);

    // Internal: format a seconds value as mm:ss.mmm.
    static std::string _fmt_time(double seconds);

    // Internal: format a seconds value as HH:MM:SS:FF.
    static std::string _fmt_smpte(double seconds, double fps);
};
