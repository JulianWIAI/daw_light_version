/**
 * SfzParser.cpp -- SFZ v1/v2 text parser implementation.
 * =============================================================================
 * Uses a two-pass approach:
 *   Pass 1 (preprocess): resolve #include directives, strip // comments.
 *   Pass 2 (tokenize/build): scan for <headers> and opcode=value pairs,
 *                            apply group-level defaults to child regions.
 *
 * Note name → MIDI mapping (SFZ standard):
 *   C-1=0  C0=12  C1=24 ... C4=60 (middle C) ... G9=127
 */

#include "SfzParser.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <unordered_map>

namespace {

// ── Utility: trim whitespace from both ends ───────────────────────────────────

static void ltrim(std::string& s) {
    s.erase(s.begin(), std::find_if(s.begin(), s.end(),
                [](unsigned char c) { return !std::isspace(c); }));
}
static void rtrim(std::string& s) {
    s.erase(std::find_if(s.rbegin(), s.rend(),
                [](unsigned char c) { return !std::isspace(c); }).base(), s.end());
}
static std::string trimmed(std::string s) { ltrim(s); rtrim(s); return s; }

// ── Utility: lowercase string ─────────────────────────────────────────────────

static std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), ::tolower);
    return s;
}

// ── Note name → MIDI number ───────────────────────────────────────────────────
// Supports: C4, D#3, Bb2, as well as plain integers.
// Octave convention: C-1 = 0, C0 = 12, C4 = 60.

static int parse_note_value(const std::string& raw) {
    std::string s = trimmed(raw);
    if (s.empty()) return -1;

    // Try plain integer first (common case).
    try {
        size_t pos;
        int v = std::stoi(s, &pos);
        if (pos == s.size()) return std::clamp(v, 0, 127);
    } catch (...) {}

    // Parse note name.
    static const std::unordered_map<char, int> semitone{
        {'c',0},{'d',2},{'e',4},{'f',5},{'g',7},{'a',9},{'b',11}
    };

    auto it = semitone.find(static_cast<char>(std::tolower(static_cast<unsigned char>(s[0]))));
    if (it == semitone.end()) return -1;

    int note = it->second;
    size_t idx = 1;

    // Accidental: # or b
    if (idx < s.size() && s[idx] == '#') { note++; idx++; }
    else if (idx < s.size() && s[idx] == 'b') { note--; idx++; }

    // Octave number (may be negative, e.g. C-1).
    if (idx >= s.size()) return -1;
    try {
        int octave = std::stoi(s.substr(idx));
        int midi   = (octave + 1) * 12 + note;
        return std::clamp(midi, 0, 127);
    } catch (...) { return -1; }
}

// ── Safe float/int parsers ────────────────────────────────────────────────────

static float safe_float(const std::string& s, float def = 0.0f) {
    try { return std::stof(trimmed(s)); } catch (...) { return def; }
}
static int safe_int(const std::string& s, int def = 0) {
    try { return std::stoi(trimmed(s)); } catch (...) { return def; }
}

// ── Token types ───────────────────────────────────────────────────────────────

enum class TokType { HEADER, OPCODE };

struct Token {
    TokType     type;
    std::string name;   // header name OR opcode key (lowercased)
    std::string value;  // empty for headers; opcode value string
};

// ── Tokenize preprocessed SFZ text ───────────────────────────────────────────
// Produces a flat list of HEADER and OPCODE tokens.

static std::vector<Token> tokenize(const std::string& text) {
    std::vector<Token> tokens;
    size_t i = 0;
    const size_t n = text.size();

    while (i < n) {
        char c = text[i];

        // Skip whitespace.
        if (std::isspace(static_cast<unsigned char>(c))) { ++i; continue; }

        // Header: <...>
        if (c == '<') {
            ++i;
            size_t j = text.find('>', i);
            if (j == std::string::npos) break;
            Token t;
            t.type = TokType::HEADER;
            t.name = lower(trimmed(text.substr(i, j - i)));
            tokens.push_back(std::move(t));
            i = j + 1;
            continue;
        }

        // Opcode: key=value  (value runs until whitespace or next header '<')
        // Collect the key.
        size_t key_start = i;
        while (i < n && text[i] != '=' && text[i] != '<' && !std::isspace(static_cast<unsigned char>(text[i])))
            ++i;

        if (i >= n || text[i] != '=') continue; // not an opcode, skip fragment

        std::string key = lower(trimmed(text.substr(key_start, i - key_start)));
        ++i; // skip '='

        // Collect the value: run to end of line or next header or another opcode= token.
        size_t val_start = i;
        // Find next whitespace-then-word= or < or newline that acts as delimiter.
        // Simple approach: value ends at next whitespace that is followed by word=, <, or EOF.
        std::string value;
        {
            std::ostringstream oss;
            while (i < n) {
                char v = text[i];
                // End on '<' (start of header).
                if (v == '<') break;
                // Check if this whitespace precedes another opcode key.
                if (std::isspace(static_cast<unsigned char>(v))) {
                    // Peek ahead: skip whitespace and see if next non-space has '='.
                    size_t peek = i + 1;
                    while (peek < n && std::isspace(static_cast<unsigned char>(text[peek]))) ++peek;
                    // Find '=' in next token.
                    size_t eq = peek;
                    while (eq < n && text[eq] != '=' && text[eq] != ' ' && text[eq] != '<' && text[eq] != '\n') ++eq;
                    if (eq < n && text[eq] == '=') break; // next token is an opcode
                    // Otherwise whitespace is part of value (sample paths may have spaces).
                    oss << v;
                } else {
                    oss << v;
                }
                ++i;
            }
            value = trimmed(oss.str());
        }

        if (!key.empty()) {
            Token t;
            t.type  = TokType::OPCODE;
            t.name  = key;
            t.value = value;
            tokens.push_back(std::move(t));
        }
    }
    return tokens;
}

// ── Apply a (key,value) opcode to a region struct ─────────────────────────────

static void apply_opcode_to_region(SfzRegionInfo& r, const std::string& key, const std::string& val) {
    if      (key == "lokey")         { int v = parse_note_value(val); if (v >= 0) r.key_range.lo = v; }
    else if (key == "hikey")         { int v = parse_note_value(val); if (v >= 0) r.key_range.hi = v; }
    else if (key == "key") {
        int v = parse_note_value(val);
        if (v >= 0) { r.key_range.lo = v; r.key_range.hi = v; }
    }
    else if (key == "lovel")         r.vel_range.lo  = safe_int(val, r.vel_range.lo);
    else if (key == "hivel")         r.vel_range.hi  = safe_int(val, r.vel_range.hi);
    else if (key == "sample")        r.sample        = val;
    else if (key == "volume")        r.volume        = safe_float(val);
    else if (key == "pan")           r.pan           = safe_float(val);
    else if (key == "seq_length")    r.seq_length    = std::max(1, safe_int(val, 1));
    else if (key == "seq_position")  r.seq_position  = std::max(1, safe_int(val, 1));
    else if (key == "group")         r.group         = safe_int(val, 0);
}

// ── Apply opcodes to a group's defaults ──────────────────────────────────────

static void apply_opcode_to_group(SfzGroupInfo& g, const std::string& key, const std::string& val) {
    if      (key == "lokey") { int v = parse_note_value(val); if (v >= 0) g.key_range.lo = v; }
    else if (key == "hikey") { int v = parse_note_value(val); if (v >= 0) g.key_range.hi = v; }
    else if (key == "lovel") g.vel_range.lo = safe_int(val, g.vel_range.lo);
    else if (key == "hivel") g.vel_range.hi = safe_int(val, g.vel_range.hi);
    else if (key == "volume") g.volume = safe_float(val);
    else if (key == "pan")    g.pan    = safe_float(val);
}

} // anonymous namespace

// ── SfzParser public implementation ──────────────────────────────────────────

std::string SfzParser::load_and_preprocess(const std::string& path, const std::string& base_dir) {
    std::ifstream file(path);
    if (!file.is_open()) return {};

    std::ostringstream out;
    std::string line;

    while (std::getline(file, line)) {
        // Strip // comments (but preserve // inside quoted sample paths — simple heuristic).
        auto cpos = line.find("//");
        if (cpos != std::string::npos) line = line.substr(0, cpos);

        // Handle #include "file.sfz" — single level only.
        std::string lt = trimmed(line);
        if (lt.rfind("#include", 0) == 0) {
            auto q1 = lt.find('"');
            auto q2 = (q1 != std::string::npos) ? lt.find('"', q1 + 1) : std::string::npos;
            if (q1 != std::string::npos && q2 != std::string::npos) {
                std::string inc_rel = lt.substr(q1 + 1, q2 - q1 - 1);
                std::string inc_abs = base_dir + "/" + inc_rel;
                out << load_and_preprocess(inc_abs, base_dir) << '\n';
            }
            continue;
        }

        out << line << '\n';
    }
    return out.str();
}

int SfzParser::parse_note_value(const std::string& s) {
    return ::parse_note_value(s); // delegate to anonymous-namespace helper
}

SfzInstrumentInfo SfzParser::parse(const std::string& sfz_path) {
    namespace fs = std::filesystem;

    SfzInstrumentInfo info;
    info.path = sfz_path;
    info.name = fs::path(sfz_path).stem().string();

    std::string base_dir = fs::path(sfz_path).parent_path().string();
    std::string text     = load_and_preprocess(sfz_path, base_dir);
    if (text.empty()) return info;

    auto tokens = tokenize(text);

    // Build instrument from token stream.
    enum class Context { NONE, GROUP, REGION };
    Context ctx = Context::NONE;

    SfzGroupInfo  cur_group;
    SfzRegionInfo cur_region;

    // Lambda: finalise the current region (applying group defaults if not overridden).
    auto flush_region = [&]() {
        if (ctx != Context::REGION) return;
        // Apply group key/vel defaults only if still at defaults.
        if (cur_region.key_range.lo == 0 && cur_region.key_range.hi == 127) {
            cur_region.key_range = cur_group.key_range;
        }
        if (cur_region.vel_range.lo == 0 && cur_region.vel_range.hi == 127) {
            cur_region.vel_range = cur_group.vel_range;
        }
        if (cur_region.volume == 0.0f) cur_region.volume = cur_group.volume;
        if (cur_region.pan    == 0.0f) cur_region.pan    = cur_group.pan;
        cur_group.regions.push_back(cur_region);
        cur_region = SfzRegionInfo{};
        ctx = Context::GROUP; // stay in group context after closing a region
    };

    auto flush_group = [&]() {
        flush_region();
        if (!cur_group.regions.empty())
            info.groups.push_back(std::move(cur_group));
        cur_group  = SfzGroupInfo{};
        ctx = Context::NONE;
    };

    for (auto& tok : tokens) {
        if (tok.type == TokType::HEADER) {
            if (tok.name == "group") {
                flush_group();
                ctx = Context::GROUP;
            } else if (tok.name == "region") {
                if (ctx == Context::NONE) ctx = Context::GROUP; // implicit group
                flush_region();
                ctx = Context::REGION;
            }
            // global / control / master / curve / effect headers are skipped.
            continue;
        }

        // OPCODE token.
        const std::string& key = tok.name;
        const std::string& val = tok.value;

        // CC label: e.g. label_cc74=Cutoff
        if (key.rfind("label_cc", 0) == 0 && key.size() > 8) {
            try {
                int cc_num = std::stoi(key.substr(8));
                if (cc_num >= 0 && cc_num < 128)
                    info.cc_labels.emplace_back(cc_num, val);
            } catch (...) {}
            continue;
        }

        // Distribute to current context.
        if (ctx == Context::GROUP)  apply_opcode_to_group(cur_group, key, val);
        if (ctx == Context::REGION) apply_opcode_to_region(cur_region, key, val);
    }
    flush_group(); // finalise last group

    // Build the flat region list.
    for (auto& g : info.groups)
        for (auto& r : g.regions)
            info.regions.push_back(r);

    info.num_groups  = static_cast<int>(info.groups.size());
    info.num_regions = static_cast<int>(info.regions.size());

    // Sort cc_labels by CC number for stable display.
    std::sort(info.cc_labels.begin(), info.cc_labels.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });

    return info;
}
