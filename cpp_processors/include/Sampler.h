/*
 * Sampler.h  --  Polyphonic sample-playback instrument engine
 * ============================================================
 * Architecture overview
 * ---------------------
 * The Sampler holds one audio sample (the "patch") loaded entirely into a
 * std::vector<float> in RAM.  Up to SAMPLER_MAX_VOICES voices can play the
 * same sample simultaneously at different pitches (and different points in
 * the sample).  No disk I/O occurs in the real-time thread.
 *
 * PITCH SHIFTING  (per voice)
 *   Each note-on computes a fractional read step:
 *     step = 2^((midi_note - root_note) / 12)  *  (file_sr / host_sr)
 *   The file_sr / host_sr factor corrects for files recorded at a different
 *   sample rate than the host.  Linear interpolation between adjacent samples
 *   removes the "staircase" artefacts caused by fractional positions.
 *
 * ADSR ENVELOPE  (per voice)
 *   Four stages modulate the voice's amplitude over time:
 *
 *   ATTACK   (note-on → peak)
 *     env rises from 0 to 1 at a constant rate = 1 / attack_samples.
 *
 *   DECAY    (peak → sustain)
 *     env falls from 1 to sustain_ at rate = (1 - sustain) / decay_samples.
 *
 *   SUSTAIN  (held while key is down)
 *     env is fixed at sustain_.  The read position continues to advance
 *     through the sample.  If the sample ends before note-off, the voice
 *     becomes idle naturally.
 *
 *   RELEASE  (note-off → silence)
 *     env falls from its current level to 0 at rate = level / release_samples.
 *     Using the current level (not 1.0) prevents clicks when note-off arrives
 *     during attack or decay.
 *
 * VOICE STEALING
 *   When all 8 voices are active and a new note-on arrives, the engine:
 *     1. Takes a voice already in RELEASE (lowest remaining level).
 *     2. If none, takes the voice with the lowest envelope level.
 *   This gives the perceptually quietest steal.
 *
 * THREAD SAFETY NOTE
 *   note_on() and note_off() modify voice state; process() reads and modifies
 *   it too.  Both must NOT run concurrently.  In the Python DAW context this
 *   is satisfied by calling note events and process() sequentially inside the
 *   same audio callback.  For multi-threaded hosts, guard with a mutex at the
 *   Python layer.
 *
 * Memory policy
 * -------------
 * process() performs zero heap allocations.  All voice state is stored in a
 * fixed-size std::array.  The sample buffer is a std::vector allocated only
 * during load_sample() (outside the real-time thread).
 */

#pragma once

#include <vector>
#include <array>
#include <cmath>
#include <algorithm>
#include <limits>

#ifndef MAX_BLOCK_SIZE
#define MAX_BLOCK_SIZE 4096
#endif

/* Number of simultaneous voices the engine can produce. */
static constexpr int SAMPLER_MAX_VOICES = 8;

/* ─── ADSR stage enumeration ─────────────────────────────────────────────── */

enum class VoiceStage : int {
    IDLE    = 0,   /* Silent — may be reused for a new note.        */
    ATTACK  = 1,   /* Envelope rising  0 → 1.                       */
    DECAY   = 2,   /* Envelope falling 1 → sustain.                 */
    SUSTAIN = 3,   /* Envelope held at sustain level.               */
    RELEASE = 4,   /* Envelope falling current → 0 after note-off.  */
};

/* ─── Single polyphonic voice ─────────────────────────────────────────────── */

struct SamplerVoice {
    bool       active       = false;              /* Currently playing.          */
    int        midi_note    = 60;                 /* MIDI pitch 0..127.          */
    float      velocity     = 1.f;                /* Key velocity 0..1.          */

    double     read_pos     = 0.0;                /* Fractional sample position. */
    double     step         = 1.0;                /* Read-position advance/sample.*/

    VoiceStage stage        = VoiceStage::IDLE;
    float      env_level    = 0.f;                /* Current envelope value 0..1.*/
    float      attack_rate  = 0.f;                /* Inc/sample during ATTACK.   */
    float      decay_rate   = 0.f;                /* Dec/sample during DECAY.    */
    float      release_rate = 0.f;                /* Dec/sample during RELEASE.  */
};

/* ─── Sampler engine ─────────────────────────────────────────────────────── */

class Sampler {
public:
    /* Construction / lifecycle. */
    explicit Sampler(float sample_rate);
    void prepare(float sample_rate);   /* Call when the host sample rate changes. */
    void reset();                       /* Stop all voices; keep the loaded sample. */

    /* ── Sample loading (called from the GUI / Python thread, NOT real-time) ──
     *
     * data     : flat float32 array — mono or stereo interleaved, normalised [-1, +1].
     *            For stereo: [L0, R0, L1, R1, ...].
     * n_total  : total number of floats in data (frames × channels).
     * file_sr  : sample rate of the source audio file in Hz.
     * channels : 1 = mono, 2 = stereo.
     */
    void load_sample(const float* data, int n_total, float file_sr, int channels);

    /* ── ADSR parameter setters ──
     * Changes take effect on the next note_on(); in-flight voices are unaffected.
     */
    void set_attack_ms (float ms);     /* 0 .. 5000 ms.  */
    void set_decay_ms  (float ms);     /* 0 .. 5000 ms.  */
    void set_sustain   (float level);  /* 0 .. 1.        */
    void set_release_ms(float ms);     /* 0 .. 10000 ms. */

    /* ── Root note ── */
    void set_root_note(int midi_note); /* 0..127, default 60 (C4). */

    /* ── MIDI triggers (call from audio callback — see thread-safety note) ──
     * note_on  : start a new voice (or steal the quietest if all 8 are busy).
     * note_off : switch the matching voice into RELEASE.
     */
    void note_on (int midi_note, float velocity);  /* velocity 0..1 */
    void note_off(int midi_note);

    /* ── Audio rendering ──
     * Adds the sampler's audio into left/right.  The caller is responsible for
     * zeroing or filling the buffers before this call if needed.
     * num_samples must be ≤ MAX_BLOCK_SIZE.
     */
    void process(float* left, float* right, int num_samples);

    /* ── Status queries (GUI polling) ── */
    bool sample_loaded()     const noexcept { return sample_loaded_; }
    int  sample_num_frames() const noexcept { return sample_num_frames_; }
    int  active_voice_count() const noexcept;

private:
    float sample_rate_;

    /* ── Sample data (allocated only in load_sample, outside RT thread) ── */
    std::vector<float> sample_data_;   /* flat float32 buffer.     */
    int    sample_channels_    = 1;    /* 1 or 2.                  */
    double sample_file_sr_     = 44100.0;
    int    sample_num_frames_  = 0;    /* total frames (not floats).*/
    bool   sample_loaded_      = false;

    /* ── Voice pool (fixed size — no allocation in RT thread) ── */
    std::array<SamplerVoice, SAMPLER_MAX_VOICES> voices_{};

    /* ── ADSR parameters ── */
    float attack_ms_  = 5.f;
    float decay_ms_   = 100.f;
    float sustain_    = 0.8f;
    float release_ms_ = 300.f;

    /* ── Root note ── */
    int root_note_ = 60;

    /* ── Private helpers ── */

    /* Find an idle voice index; steals the quietest on overflow. */
    int _find_free_voice() const;

    /* Initialise a voice struct with the given note and current ADSR settings. */
    void _init_voice(SamplerVoice& v, int midi_note, float velocity);

    /* Advance the ADSR state machine for one sample; returns envelope value. */
    float _update_env(SamplerVoice& v);

    /* Linear interpolation read from sample_data_ at a fractional frame pos.
     * channel: 0 = left/mono, 1 = right (clamped if mono). */
    float _read_lerp(double pos, int channel) const noexcept;
};
