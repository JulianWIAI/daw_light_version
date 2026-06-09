/**
 * DecentSamplerEngine.h -- Polyphonic sampler engine for Decent Sampler (.dspreset) files.
 * ==========================================================================================
 * Accepts zone metadata from Python (via DsZoneData structs), loads the WAV files from
 * disk, and provides a real-time polyphonic render loop with per-voice ADSR envelopes and
 * linear pitch-shift via playback-rate scaling (2^((note - root_note) / 12)).
 *
 * No XML parsing lives here -- the Python dspreset_parser produces DsZoneData lists
 * which are passed to load_zones().  All audio DSP is implemented in this class.
 *
 * Supported WAV formats:
 *   - PCM 16-bit  (most common DS sample format)
 *   - PCM 24-bit  (high-res packs)
 *   - IEEE float32 (some export pipelines)
 *
 * Voice model:
 *   - Up to MAX_VOICES simultaneous voices (default 64).
 *   - Per-voice linear ADSR on amplitude.
 *   - Pitch-shift by adjusting sample playback rate (no interpolation beyond linear).
 *   - Round-robin sequencing: C++ tracks seq_counter per group so each note
 *     triggers the correct sequential zone.
 *
 * Thread safety:
 *   render()        -- audio thread only (NOT thread-safe with note_on/off)
 *   note_on/off     -- safe from any thread (uses a lock-free atomic queue)
 *   load_zones()    -- main thread only, not during render()
 *   set_parameter() -- safe from any thread
 */

#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>


// ── Per-zone metadata struct (mirrors Python DsSampleZone) ───────────────────

struct DsZoneData {
    std::string path;         // Absolute path to the WAV file.
    int   root_note   = 60;   // MIDI note the sample was recorded at.
    int   lo_note     = 0;    // Lowest MIDI note this zone covers.
    int   hi_note     = 127;  // Highest MIDI note this zone covers.
    int   lo_vel      = 0;    // Lowest velocity this zone covers.
    int   hi_vel      = 127;  // Highest velocity this zone covers.
    float volume_db   = 0.0f; // Volume trim in dB (0 = unity gain).
    float pan         = 0.0f; // Pan: -100 (L) … 0 (C) … +100 (R).
    // ADSR times in seconds / level 0-1.
    float attack      = 0.002f;
    float decay       = 0.1f;
    float sustain     = 1.0f;
    float release     = 0.3f;
    // Loop settings.
    bool  loop_enabled  = false;
    int   loop_start    = 0;   // Loop start frame (-1 = unused).
    int   loop_end      = -1;  // Loop end frame   (-1 = end of file).
    // Round-robin sequencing.
    int   seq_position  = 1;   // 1-based position in the RR cycle.
    int   seq_length    = 1;   // Total number of RR alternatives.
    // Trigger type (unused in render; kept for completeness).
    std::string trigger = "attack";
};


// ── Main engine class ─────────────────────────────────────────────────────────

class DecentSamplerEngine {
public:
    static constexpr int MAX_VOICES = 64;

    // Construct with the host sample rate and maximum block size.
    explicit DecentSamplerEngine(float sample_rate = 44100.0f,
                                  int   block_size  = 512);
    ~DecentSamplerEngine();

    // Non-copyable (owns large audio buffers).
    DecentSamplerEngine(const DecentSamplerEngine&)            = delete;
    DecentSamplerEngine& operator=(const DecentSamplerEngine&) = delete;

    // ── Setup ─────────────────────────────────────────────────────────────────

    void set_sample_rate(float sr);
    void set_block_size(int block_size);

    // ── Instrument loading ────────────────────────────────────────────────────

    /**
     * Load a list of DsZoneData structs.
     *
     * For each zone the WAV file is read from disk and decoded into a
     * float32 stereo buffer that lives in RAM for the lifetime of the preset.
     * Zones whose WAV files cannot be read are silently skipped.
     *
     * @returns true if at least one zone loaded successfully.
     */
    bool load_zones(const std::vector<DsZoneData>& zones);

    /** Load from a .dspreset file path (convenience wrapper; calls parse + load_zones). */
    bool load_preset(const std::string& path);

    /** True after at least one successful zone load. */
    bool is_loaded() const noexcept;

    /** Number of zones currently loaded. */
    int zone_count() const noexcept;

    // ── MIDI input ────────────────────────────────────────────────────────────

    // channel is ignored (DS presets are single-channel); kept for API symmetry.
    void note_on (int channel, int note, int velocity);
    void note_off(int channel, int note, int velocity);
    void all_notes_off(int channel = 0);

    // ── Parameter control ─────────────────────────────────────────────────────

    /**
     * Apply a named DS parameter change.
     *
     * Currently recognised parameters:
     *   "ENV_ATTACK"   -- global ADSR attack multiplier (0.0-1.0 → 0x-2x)
     *   "ENV_DECAY"    -- global ADSR decay  multiplier
     *   "ENV_SUSTAIN"  -- global ADSR sustain level (0-1 direct)
     *   "ENV_RELEASE"  -- global ADSR release multiplier
     *   "MASTER_VOLUME"-- output gain in dB (-60 to +12)
     *
     * Unrecognised parameter names are silently ignored.
     */
    void set_parameter(const std::string& name, float value);

    // ── Audio rendering ───────────────────────────────────────────────────────

    /**
     * Render num_samples frames of stereo audio into the caller-supplied buffers.
     * Buffers must be at least num_samples floats each.
     * Call ONLY from the audio thread.  Output is silence if nothing is loaded.
     */
    void render(float* left, float* right, int num_samples);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;

    float sample_rate_ = 44100.0f;
    int   block_size_  = 512;
    bool  loaded_      = false;
};
