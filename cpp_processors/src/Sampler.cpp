/*
 * Sampler.cpp  --  Implementation of the polyphonic sample-playback engine.
 *
 * See Sampler.h for the full architecture overview, ADSR design notes,
 * voice-stealing strategy, and thread-safety requirements.
 */

#include "Sampler.h"
#include <cstring>   // std::memset
#include <cassert>

// ─────────────────────────────────────────────────────────────────────────────
// Construction / lifecycle
// ─────────────────────────────────────────────────────────────────────────────

Sampler::Sampler(float sample_rate)
    : sample_rate_(sample_rate)
{
    /* voices_ is default-constructed (all fields zero / IDLE) via std::array. */
}

void Sampler::prepare(float sample_rate)
{
    sample_rate_ = sample_rate;
    reset();
}

void Sampler::reset()
{
    /* Silence every voice without touching the loaded sample buffer. */
    for (auto& v : voices_) {
        v.active    = false;
        v.stage     = VoiceStage::IDLE;
        v.env_level = 0.f;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Sample loading  (non-real-time — may allocate)
// ─────────────────────────────────────────────────────────────────────────────

void Sampler::load_sample(const float* data, int n_total, float file_sr, int channels)
{
    /* Validate inputs before touching state. */
    if (!data || n_total <= 0 || file_sr <= 0.f || (channels != 1 && channels != 2)) {
        sample_loaded_ = false;
        return;
    }

    /* Deep-copy into the internal buffer. */
    sample_data_.assign(data, data + n_total);
    sample_channels_   = channels;
    sample_file_sr_    = static_cast<double>(file_sr);
    sample_num_frames_ = n_total / channels;  /* frames = total floats / channels */
    sample_loaded_     = true;

    /* Any active voices that pointed into the old buffer must be silenced. */
    reset();
}

// ─────────────────────────────────────────────────────────────────────────────
// ADSR parameter setters
// ─────────────────────────────────────────────────────────────────────────────

void Sampler::set_attack_ms (float ms) { attack_ms_  = ms;    }
void Sampler::set_decay_ms  (float ms) { decay_ms_   = ms;    }
void Sampler::set_sustain   (float lvl){ sustain_     = lvl;   }
void Sampler::set_release_ms(float ms) { release_ms_ = ms;    }

void Sampler::set_root_note(int midi_note)
{
    root_note_ = midi_note;
    /* Root-note changes do not affect already-playing voices — they use the
     * step value baked in at note_on() time. */
}

// ─────────────────────────────────────────────────────────────────────────────
// MIDI triggers
// ─────────────────────────────────────────────────────────────────────────────

void Sampler::note_on(int midi_note, float velocity)
{
    if (!sample_loaded_) return;

    /* Find a voice slot; steals the quietest if all 8 are busy. */
    int idx = _find_free_voice();
    _init_voice(voices_[idx], midi_note, velocity);
}

void Sampler::note_off(int midi_note)
{
    /* Find the active voice matching this MIDI note and enter RELEASE. */
    for (auto& v : voices_) {
        if (v.active && v.midi_note == midi_note &&
            v.stage != VoiceStage::RELEASE && v.stage != VoiceStage::IDLE)
        {
            v.stage = VoiceStage::RELEASE;
            /* release_rate is computed from the *current* envelope level so
             * that note-off during attack or decay doesn't cause a click. */
            const float release_samples = (release_ms_ / 1000.f) * sample_rate_;
            v.release_rate = (release_samples > 0.f)
                           ? v.env_level / release_samples
                           : v.env_level;  /* instant if release_ms == 0 */
            break;  /* only the first matching voice — pedantic but correct */
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Audio rendering  (real-time — zero heap allocations)
// ─────────────────────────────────────────────────────────────────────────────

void Sampler::process(float* left, float* right, int num_samples)
{
    /* process() ADDS to the caller's buffer (mixer model).
     * The caller zeros / fills the buffer before invoking the sampler. */

    if (!sample_loaded_) return;

    for (auto& v : voices_) {
        if (!v.active) continue;

        for (int i = 0; i < num_samples; ++i) {
            /* Advance the ADSR state machine for this sample tick. */
            const float env = _update_env(v);

            if (!v.active) break;  /* voice went IDLE inside _update_env */

            /* Bounds check: if read position is past the sample end, retire. */
            if (static_cast<int>(v.read_pos) >= sample_num_frames_) {
                v.active = false;
                v.stage  = VoiceStage::IDLE;
                break;
            }

            /* Read left (or mono) channel, then right channel (clamped to left
             * for mono sources so the sampler always produces stereo output). */
            const float l_samp = _read_lerp(v.read_pos, 0);
            const float r_samp = _read_lerp(v.read_pos, (sample_channels_ == 2) ? 1 : 0);

            /* Amplitude = velocity × envelope level. */
            const float gain = v.velocity * env;

            left [i] += l_samp * gain;
            right[i] += r_samp * gain;

            /* Advance the fractional read position by the pitch-shifted step. */
            v.read_pos += v.step;
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Status query
// ─────────────────────────────────────────────────────────────────────────────

int Sampler::active_voice_count() const noexcept
{
    int n = 0;
    for (const auto& v : voices_) {
        if (v.active) ++n;
    }
    return n;
}

// ─────────────────────────────────────────────────────────────────────────────
// Private helpers
// ─────────────────────────────────────────────────────────────────────────────

int Sampler::_find_free_voice() const
{
    /* Pass 1: grab any idle slot. */
    for (int i = 0; i < SAMPLER_MAX_VOICES; ++i) {
        if (!voices_[i].active || voices_[i].stage == VoiceStage::IDLE) {
            return i;
        }
    }

    /* Pass 2: steal the RELEASE voice with the lowest envelope level. */
    int   best_idx   = -1;
    float best_level = std::numeric_limits<float>::max();
    for (int i = 0; i < SAMPLER_MAX_VOICES; ++i) {
        if (voices_[i].stage == VoiceStage::RELEASE &&
            voices_[i].env_level < best_level) {
            best_level = voices_[i].env_level;
            best_idx   = i;
        }
    }
    if (best_idx != -1) return best_idx;

    /* Pass 3: steal the globally quietest voice of any stage. */
    best_level = std::numeric_limits<float>::max();
    for (int i = 0; i < SAMPLER_MAX_VOICES; ++i) {
        if (voices_[i].env_level < best_level) {
            best_level = voices_[i].env_level;
            best_idx   = i;
        }
    }
    return (best_idx != -1) ? best_idx : 0;  /* fallback: always return valid index */
}

void Sampler::_init_voice(SamplerVoice& v, int midi_note, float velocity)
{
    v.active    = true;
    v.midi_note = midi_note;
    v.velocity  = velocity;
    v.read_pos  = 0.0;
    v.stage     = VoiceStage::ATTACK;
    v.env_level = 0.f;

    /* Pitch step: resampling ratio that maps the source sample to the correct
     * pitch.  The file_sr / host_sr factor corrects for sample-rate mismatch. */
    const double semitone_ratio = std::pow(2.0, (midi_note - root_note_) / 12.0);
    v.step = semitone_ratio * (sample_file_sr_ / static_cast<double>(sample_rate_));

    /* Pre-compute per-voice ADSR rates from the current global settings. */
    const float attack_samples  = (attack_ms_  / 1000.f) * sample_rate_;
    const float decay_samples   = (decay_ms_   / 1000.f) * sample_rate_;

    v.attack_rate  = (attack_samples  > 0.f) ? (1.f / attack_samples)              : 1.f;
    v.decay_rate   = (decay_samples   > 0.f) ? ((1.f - sustain_) / decay_samples)  : (1.f - sustain_);
    v.release_rate = 0.f;  /* Set properly at note_off() time. */
}

float Sampler::_update_env(SamplerVoice& v)
{
    switch (v.stage) {
        case VoiceStage::ATTACK:
            v.env_level += v.attack_rate;
            if (v.env_level >= 1.f) {
                v.env_level = 1.f;
                v.stage     = VoiceStage::DECAY;
            }
            break;

        case VoiceStage::DECAY:
            v.env_level -= v.decay_rate;
            if (v.env_level <= sustain_) {
                v.env_level = sustain_;
                v.stage     = VoiceStage::SUSTAIN;
            }
            break;

        case VoiceStage::SUSTAIN:
            /* Level is constant; the read position continues to advance. */
            v.env_level = sustain_;
            break;

        case VoiceStage::RELEASE:
            v.env_level -= v.release_rate;
            if (v.env_level <= 0.f) {
                v.env_level = 0.f;
                v.active    = false;
                v.stage     = VoiceStage::IDLE;
            }
            break;

        case VoiceStage::IDLE:
        default:
            v.env_level = 0.f;
            v.active    = false;
            break;
    }

    return v.env_level;
}

float Sampler::_read_lerp(double pos, int channel) const noexcept
{
    /* Integer and fractional parts of the read position (in frames). */
    const int    frame0 = static_cast<int>(pos);
    const float  frac   = static_cast<float>(pos - frame0);
    const int    frame1 = frame0 + 1;

    /* Guard: past-end reads return silence. */
    if (frame0 >= sample_num_frames_) return 0.f;

    /* Clamp channel index to valid range (handles mono → stereo routing). */
    const int ch = (channel < sample_channels_) ? channel : 0;

    const float s0 = sample_data_[frame0 * sample_channels_ + ch];

    /* Linear interpolation: blend s0 → s1 unless we are at the last frame. */
    if (frame1 < sample_num_frames_) {
        const float s1 = sample_data_[frame1 * sample_channels_ + ch];
        return s0 + frac * (s1 - s0);
    }

    return s0;  /* last frame: no interpolation partner */
}
