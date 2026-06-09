/**
 * Vst3TransportContext.cpp -- ProcessContext management implementation.
 * =====================================================================
 * Correctly populates all musically relevant fields in the ProcessContext
 * struct so time-based plugins (delays, LFOs, arpeggiators) sync to the DAW.
 *
 * ProcessContext flags used:
 *   kPlaying               -- transport is rolling
 *   kCycleActive           -- loop is active
 *   kRecording             -- armed and recording
 *   kTempoValid            -- tempo field is valid
 *   kTimeSigValid          -- timeSig fields are valid
 *   kProjectTimeMusicValid -- projectTimeMusic field is valid
 *   kBarPositionValid      -- barPositionMusic field is valid
 *   kCycleValid            -- cycleStart/EndMusic fields are valid
 *   kSystemTimeValid       -- systemTime is valid (populated with 0; host-specific)
 */

#include "Vst3TransportContext.h"

#ifdef HAVE_VST3_SDK

#include <cmath>  // floor

using namespace Steinberg;
using namespace Steinberg::Vst;

// ── Constructor ───────────────────────────────────────────────────────────────

Vst3TransportContext::Vst3TransportContext() {
    ctx_.sampleRate = static_cast<float>(sample_rate_);
    ctx_.tempo      = tempo_bpm_;
    ctx_.timeSigNumerator   = 4;
    ctx_.timeSigDenominator = 4;
    beats_per_bar_ = 4.0;

    // Permanently mark the fields we always populate as valid.
    ctx_.state = ProcessContext::kTempoValid
               | ProcessContext::kTimeSigValid
               | ProcessContext::kProjectTimeMusicValid
               | ProcessContext::kBarPositionValid;
}

// ── Configuration ─────────────────────────────────────────────────────────────

void Vst3TransportContext::set_sample_rate(double sr) {
    sample_rate_    = sr;
    ctx_.sampleRate = static_cast<float>(sr);
    update_musical_time(); // beat positions depend on sample rate
}

void Vst3TransportContext::set_tempo(double bpm) {
    tempo_bpm_  = bpm;
    ctx_.tempo  = bpm;
    update_musical_time();
}

void Vst3TransportContext::set_time_signature(int32 numerator, int32 denominator) {
    ctx_.timeSigNumerator   = numerator;
    ctx_.timeSigDenominator = denominator;
    beats_per_bar_ = static_cast<double>(numerator);
    update_musical_time();
}

// ── Transport state ───────────────────────────────────────────────────────────

void Vst3TransportContext::set_playing(bool playing) noexcept {
    set_flag(ProcessContext::kPlaying, playing);
}

void Vst3TransportContext::set_cycling(bool cycling) noexcept {
    set_flag(ProcessContext::kCycleActive, cycling);
    // The kCycleValid flag indicates cycleStartMusic / cycleEndMusic are meaningful.
    set_flag(ProcessContext::kCycleValid, cycling);
}

void Vst3TransportContext::set_recording(bool recording) noexcept {
    set_flag(ProcessContext::kRecording, recording);
}

void Vst3TransportContext::set_cycle_range(double start_beats, double end_beats) noexcept {
    ctx_.cycleStartMusic = start_beats;
    ctx_.cycleEndMusic   = end_beats;
}

// ── Position control ──────────────────────────────────────────────────────────

void Vst3TransportContext::advance(int32 num_samples) noexcept {
    ctx_.projectTimeSamples += static_cast<int64>(num_samples);
    update_musical_time();
}

void Vst3TransportContext::set_sample_position(int64 sample_pos) noexcept {
    ctx_.projectTimeSamples = sample_pos;
    update_musical_time();
}

void Vst3TransportContext::reset() noexcept {
    ctx_.projectTimeSamples = 0;
    ctx_.continousTimeSamples = 0;
    update_musical_time();
}

// ── Internal: recalculate musical time from sample position ──────────────────

void Vst3TransportContext::update_musical_time() noexcept {
    if (sample_rate_ <= 0.0 || tempo_bpm_ <= 0.0) return;

    // Time in seconds since project start.
    double time_secs = static_cast<double>(ctx_.projectTimeSamples) / sample_rate_;

    // Project time in quarter-note beats.
    // 1 beat = 60 / tempo_bpm seconds.
    ctx_.projectTimeMusic = time_secs * (tempo_bpm_ / 60.0);

    // Bar position: beat index of the current bar's downbeat.
    // One bar = timeSigNumerator quarter notes.
    if (beats_per_bar_ > 0.0) {
        double bar_index       = std::floor(ctx_.projectTimeMusic / beats_per_bar_);
        ctx_.barPositionMusic  = bar_index * beats_per_bar_;
    }
}

// ── Internal: set / clear a single flag bit ───────────────────────────────────

void Vst3TransportContext::set_flag(ProcessContext::StatesAndFlags flag, bool on) noexcept {
    if (on)
        ctx_.state |=  static_cast<uint32>(flag);
    else
        ctx_.state &= ~static_cast<uint32>(flag);
}

#endif // HAVE_VST3_SDK
