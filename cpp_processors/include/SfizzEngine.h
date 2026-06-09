/**
 * SfizzEngine.h -- SFZ v1/v2 polyphonic instrument engine (sfizz wrapper).
 * =============================================================================
 * Wraps the sfizz C API to provide full SFZ playback inside the DAW audio graph.
 *
 * Features provided by sfizz:
 *   - Complete SFZ v1 / most of v2 opcode support
 *   - Multi-velocity layers and round-robin sample cycling
 *   - Disk-streaming for large .wav/.flac sample files
 *   - Low-latency polyphonic synthesis via SIMD-accelerated DSP
 *
 * The pImpl pattern keeps sfizz headers out of the pybind11 translation unit
 * so the binding compiler does not need to see sfizz's dependency tree.
 *
 * Thread model:
 *   render()            -- audio thread only (NOT thread-safe with other calls)
 *   note_on/off/cc/...  -- any thread (sfizz queues events internally)
 *   load_sfz()          -- call from the main thread, NOT during render()
 *
 * Metadata:
 *   get_metadata() delegates to SfzParser::parse() for GUI display.
 *   No sfizz internal introspection is required.
 */

#pragma once

#include "SfzParser.h"    // SfzInstrumentInfo, SfzRegionInfo, etc.
#include <memory>
#include <string>

class SfizzEngine {
public:
    // Construct with the expected sample rate and maximum block size.
    // Both can be changed later via set_sample_rate() / set_block_size().
    explicit SfizzEngine(float sample_rate = 44100.0f, int block_size = 512);
    ~SfizzEngine();

    // Non-copyable (sfizz owns exclusive synth state).
    SfizzEngine(const SfizzEngine&)            = delete;
    SfizzEngine& operator=(const SfizzEngine&) = delete;

    // ── Setup ─────────────────────────────────────────────────────────────────

    // Set the playback sample rate.  Call before the first render() or after a
    // device change.  Resets internal sfizz state.
    void set_sample_rate(float sr);

    // Set the maximum block size sfizz should pre-allocate for.
    void set_block_size(int block_size);

    // ── Instrument loading ────────────────────────────────────────────────────

    // Load an .sfz file from disk.  Returns true on success.
    // Any currently playing notes are cut; the synth is fully reset.
    bool load_sfz(const std::string& path);

    // True after a successful load_sfz() call.
    bool is_loaded() const noexcept;

    // Return parsed instrument metadata (regions, key ranges, CC labels).
    // Delegates to SfzParser; returns empty struct if nothing is loaded.
    SfzInstrumentInfo get_metadata() const;

    // ── MIDI event input ──────────────────────────────────────────────────────
    // `delay` is the sample offset within the current audio block (0 = first sample).

    void note_on (int delay, int note, int velocity,    int channel = 0);
    void note_off(int delay, int note, int velocity,    int channel = 0);

    // MIDI control change.  cc_value is 0-127.
    void control_change(int delay, int cc, int cc_value, int channel = 0);

    // Pitch wheel.  pitch is -8192 (full down) to +8191 (full up).
    void pitch_wheel(int delay, int pitch, int channel = 0);

    // Channel aftertouch.  pressure is 0-127.
    void aftertouch(int delay, int pressure, int channel = 0);

    // Immediately silence all playing notes (sends note-off to all voices).
    void all_notes_off(int delay = 0);

    // ── Audio rendering ───────────────────────────────────────────────────────

    // Render `num_samples` frames into the caller-supplied stereo float buffers.
    // Buffers must be at least `num_samples` floats each.
    // Call ONLY from the audio thread.  Outputs silence if no instrument is loaded.
    void render(float* left, float* right, int num_samples);

private:
    struct Impl;                      // sfizz_synth_t and metadata hidden here
    std::unique_ptr<Impl> impl_;

    float sample_rate_ = 44100.0f;
    int   block_size_  = 512;
    bool  loaded_      = false;
};
