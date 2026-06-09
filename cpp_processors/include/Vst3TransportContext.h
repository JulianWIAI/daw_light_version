/**
 * Vst3TransportContext.h -- VST3 ProcessContext transport/tempo manager.
 * ======================================================================
 * Maintains and updates the Steinberg::Vst::ProcessContext struct that is
 * passed to every VST3 plugin's process() call.
 *
 * Plugins use ProcessContext to:
 *   - Sync LFOs, delays, and arpeggios to project tempo
 *   - Advance internal beat counters (projectTimeMusic)
 *   - Query playback state (playing, looping, recording)
 *   - Obtain the global sample position for sample-accurate timestamping
 *
 * Usage (per audio block):
 *   // Setup (once at session start or transport change):
 *   transport.set_sample_rate(44100.0);
 *   transport.set_tempo(120.0);
 *   transport.set_time_signature(4, 4);
 *
 *   // Main loop:
 *   transport.set_playing(true);
 *   for (each block) {
 *       ProcessData data = { ... };
 *       data.processContext = &transport.get_context();
 *       plugin->process(data);
 *       transport.advance(block_size);    // advance sample counter
 *   }
 *
 * Beat position calculation:
 *   projectTimeMusic (in quarter-note beats) =
 *       (projectTimeSamples / sampleRate) * (tempo / 60.0)
 *
 * Bar position:
 *   barPositionMusic (in quarter-note beats at the bar start) =
 *       floor(projectTimeMusic / timeSigNumerator) * timeSigNumerator
 */

#pragma once

#ifdef HAVE_VST3_SDK

#include "pluginterfaces/vst/ivstprocesscontext.h"  // ProcessContext, StatesAndFlags

#include <cstdint>

class Vst3TransportContext {
public:
    Vst3TransportContext();

    // ── Configuration ─────────────────────────────────────────────────────────

    // Set the playback sample rate.  Must be called before any advance() calls.
    void set_sample_rate(double sr);

    // Set the current project tempo in beats per minute.
    void set_tempo(double bpm);

    // Set the time signature (e.g. 4, 4 or 3, 4 or 7, 8).
    void set_time_signature(Steinberg::int32 numerator, Steinberg::int32 denominator);

    // ── Transport state ───────────────────────────────────────────────────────

    void set_playing   (bool playing)    noexcept;
    void set_cycling   (bool cycling)    noexcept;  // loop active
    void set_recording (bool recording)  noexcept;

    // Set the loop / cycle range in quarter-note beats.
    void set_cycle_range(double start_beats, double end_beats) noexcept;

    // ── Position control ──────────────────────────────────────────────────────

    // Advance the internal sample counter by num_samples.
    // Call ONCE per audio block, AFTER passing the context to the plugin.
    void advance(Steinberg::int32 num_samples) noexcept;

    // Jump to a specific sample position (e.g. on transport seek / loop wrap).
    void set_sample_position(Steinberg::int64 sample_pos) noexcept;

    // Reset to the very beginning (sample 0, beat 0).
    void reset() noexcept;

    // ── Access ────────────────────────────────────────────────────────────────

    // Return a const reference to the ProcessContext — pass &get_context() to
    // ProcessData::processContext each block.
    const Steinberg::Vst::ProcessContext& get_context() const noexcept { return ctx_; }

    // Current tempo in BPM.
    double get_tempo()           const noexcept { return tempo_bpm_; }

    // Current sample position.
    Steinberg::int64 get_sample_position() const noexcept { return ctx_.projectTimeSamples; }

    // Current beat position in quarter notes.
    double get_beat_position()   const noexcept { return ctx_.projectTimeMusic; }

private:
    // Recalculate all time-derived fields from the current sample position.
    void update_musical_time() noexcept;

    // Set or clear a state flag bit in ctx_.state.
    void set_flag(Steinberg::Vst::ProcessContext::StatesAndFlags flag, bool on) noexcept;

    Steinberg::Vst::ProcessContext ctx_{};   // the struct passed to process()

    double sample_rate_ = 44100.0;
    double tempo_bpm_   = 120.0;
    double beats_per_bar_ = 4.0;  // time_sig_numerator (kept as double for division)
};

#endif // HAVE_VST3_SDK
