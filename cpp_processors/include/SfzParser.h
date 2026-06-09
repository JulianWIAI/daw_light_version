/**
 * SfzParser.h -- Lightweight SFZ v1/v2 text parser for GUI metadata.
 * =============================================================================
 * Extracts instrument structure (key/velocity ranges, sample paths, round-robin
 * settings, CC labels) from an .sfz file WITHOUT instantiating the audio engine.
 *
 * Used by the Python GUI to render key-range maps, velocity layer displays, and
 * CC control panels.  Audio playback is handled separately by SfizzEngine.
 *
 * SFZ opcodes parsed:
 *   lokey / hikey / key         -- MIDI key range (integers or note names)
 *   lovel / hivel               -- velocity range (0-127)
 *   sample                      -- relative path to audio file
 *   seq_length / seq_position   -- round-robin sequence info
 *   volume                      -- dB gain (float)
 *   pan                         -- stereo pan -100 to +100 (float)
 *   group / off_by              -- group membership integers
 *   label_ccN                   -- custom CC label for controller N
 *   #include                    -- single-level file inclusion
 *
 * Not a full SFZ interpreter; only opcodes relevant to GUI visualization are
 * extracted.  Unknown opcodes are silently skipped.
 */

#pragma once

#include <string>
#include <vector>
#include <utility>   // pair

// ── Key / velocity range ──────────────────────────────────────────────────────

struct SfzKeyRange {
    int lo = 0;     // MIDI note 0-127
    int hi = 127;
};

struct SfzVelRange {
    int lo = 0;     // velocity 0-127
    int hi = 127;
};

// ── Single region entry ───────────────────────────────────────────────────────

struct SfzRegionInfo {
    SfzKeyRange  key_range;
    SfzVelRange  vel_range;
    std::string  sample;           // relative path as written in the SFZ
    float        volume      = 0.0f;  // dB
    float        pan         = 0.0f;  // -100 to +100
    int          seq_length  = 1;     // round-robin cycle length
    int          seq_position= 1;     // position within the cycle (1-based)
    int          group       = 0;     // group index (0 = no group)
};

// ── Group containing one or more regions ─────────────────────────────────────

struct SfzGroupInfo {
    SfzKeyRange              key_range;   // inherited default for child regions
    SfzVelRange              vel_range;
    float                    volume = 0.0f;
    float                    pan    = 0.0f;
    std::vector<SfzRegionInfo> regions;
};

// ── Top-level instrument descriptor (returned to Python GUI) ─────────────────

struct SfzInstrumentInfo {
    std::string  name;              // file stem of the .sfz file
    std::string  path;              // absolute path to the .sfz file
    int          num_regions = 0;   // total region count across all groups
    int          num_groups  = 0;

    // Grouped view — matches the <group>/<region> nesting in the SFZ.
    std::vector<SfzGroupInfo> groups;

    // Flat view — all regions in key order for easy iteration.
    std::vector<SfzRegionInfo> regions;

    // CC labels defined with <label_ccN> opcodes (CC number → label string).
    std::vector<std::pair<int, std::string>> cc_labels;
};

// ── Parser ────────────────────────────────────────────────────────────────────

class SfzParser {
public:
    // Parse the given .sfz file and return the instrument metadata.
    // Returns an empty SfzInstrumentInfo (num_regions == 0) on failure.
    static SfzInstrumentInfo parse(const std::string& sfz_path);

private:
    // Convert a note name ("C4", "D#3", "Bb2") or integer string to MIDI note.
    // Returns -1 on parse failure.
    static int parse_note_value(const std::string& s);

    // Load file text, expand #include directives (single level), strip comments.
    static std::string load_and_preprocess(const std::string& path,
                                            const std::string& base_dir);
};
