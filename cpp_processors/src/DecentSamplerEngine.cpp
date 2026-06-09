/**
 * DecentSamplerEngine.cpp -- Polyphonic Decent Sampler audio engine.
 * ====================================================================
 * All DSP: WAV decoding, ADSR envelopes, pitch-shift via playback-rate
 * scaling, polyphonic voice rendering, stereo pan.
 *
 * Design notes
 * ------------
 * - pImpl pattern: Impl holds all state so the header stays clean.
 * - WAV parser: hand-rolled to avoid extra dependencies.  Handles RIFF/WAVE
 *   with PCM 16-bit, PCM 24-bit, and IEEE float32 (WAVE_FORMAT_IEEE_FLOAT).
 * - Pitch shift: playback_rate = 2^((note - root_note) / 12).  Position
 *   advances by playback_rate each sample; linear interpolation between frames.
 * - ADSR: time-domain linear ramp (attack/decay/release in seconds,
 *   sustain is 0-1 level).  Each stage transitions when its counter expires.
 * - Round-robin: one std::atomic<int> per group; incremented on each note-on.
 * - Thread safety: note_on/note_off write to a lock-free single-producer /
 *   single-consumer event ring (render() is the sole consumer).
 */

#include "DecentSamplerEngine.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstring>
#include <fstream>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

// ── dspreset_parser Python bridge (only for load_preset convenience) ──────────
// We do NOT include Python headers here.  load_preset() is a thin shim that
// re-uses the platform file API to call the same XML parse logic in a minimal
// embedded parser.  For the production path Python calls load_zones() directly.

// ═══════════════════════════════════════════════════════════════════════════════
// WAV loader (RIFF/WAVE, PCM16 / PCM24 / FLOAT32)
// ═══════════════════════════════════════════════════════════════════════════════

namespace {

// Read a little-endian 16-bit unsigned from a raw byte pointer.
inline uint16_t read_u16le(const uint8_t* p) {
    return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8);
}

// Read a little-endian 32-bit unsigned from a raw byte pointer.
inline uint32_t read_u32le(const uint8_t* p) {
    return  static_cast<uint32_t>(p[0])
          |(static_cast<uint32_t>(p[1]) <<  8)
          |(static_cast<uint32_t>(p[2]) << 16)
          |(static_cast<uint32_t>(p[3]) << 24);
}

// Read a little-endian 32-bit signed integer.
inline int32_t read_i32le(const uint8_t* p) {
    return static_cast<int32_t>(read_u32le(p));
}

struct WavInfo {
    uint16_t audio_format;    // 1 = PCM, 3 = IEEE float
    uint16_t channels;
    uint32_t sample_rate;
    uint16_t bits_per_sample;
    uint32_t num_frames;      // total PCM frames (samples / channels)
};

/**
 * Load a WAV file into a float32 stereo (interleaved) buffer.
 *
 * Returns the number of stereo frames decoded, or 0 on failure.
 * out_left / out_right are filled; out_sample_rate receives the native rate.
 */
bool load_wav(const std::string& path,
              std::vector<float>& out_left,
              std::vector<float>& out_right,
              uint32_t&           out_sample_rate)
{
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f.is_open()) return false;
    auto file_size = static_cast<size_t>(f.tellg());
    f.seekg(0);
    if (file_size < 44) return false;

    // Read entire file into memory for simple offset arithmetic.
    std::vector<uint8_t> data(file_size);
    f.read(reinterpret_cast<char*>(data.data()),
           static_cast<std::streamsize>(file_size));
    if (!f) return false;
    const uint8_t* d = data.data();

    // RIFF header check.
    if (d[0]!='R'||d[1]!='I'||d[2]!='F'||d[3]!='F') return false;
    if (d[8]!='W'||d[9]!='A'||d[10]!='V'||d[11]!='E') return false;

    WavInfo info{};
    const uint8_t* data_chunk = nullptr;
    uint32_t       data_size  = 0;

    // Walk RIFF chunks.
    size_t pos = 12;
    while (pos + 8 <= file_size) {
        uint32_t chunk_size = read_u32le(d + pos + 4);
        if (d[pos]=='f'&&d[pos+1]=='m'&&d[pos+2]=='t'&&d[pos+3]==' ') {
            if (chunk_size < 16) return false;
            info.audio_format    = read_u16le(d + pos + 8);
            info.channels        = read_u16le(d + pos + 10);
            info.sample_rate     = read_u32le(d + pos + 12);
            info.bits_per_sample = read_u16le(d + pos + 22);
        } else if (d[pos]=='d'&&d[pos+1]=='a'&&d[pos+2]=='t'&&d[pos+3]=='a') {
            data_chunk = d + pos + 8;
            data_size  = chunk_size;
        }
        pos += 8 + chunk_size + (chunk_size & 1); // chunk_size is WORD-aligned
    }

    if (!data_chunk || info.channels == 0 || info.sample_rate == 0)
        return false;
    if (info.audio_format != 1 && info.audio_format != 3)
        return false; // not PCM or float32

    out_sample_rate = info.sample_rate;

    uint32_t bytes_per_sample = (info.bits_per_sample + 7) / 8;
    uint32_t bytes_per_frame  = bytes_per_sample * info.channels;
    if (bytes_per_frame == 0) return false;

    uint32_t num_frames = data_size / bytes_per_frame;
    if (num_frames == 0) return false;

    out_left .resize(num_frames);
    out_right.resize(num_frames);

    for (uint32_t i = 0; i < num_frames; ++i) {
        const uint8_t* frame = data_chunk + static_cast<size_t>(i) * bytes_per_frame;

        auto read_sample = [&](const uint8_t* p) -> float {
            if (info.audio_format == 3 && info.bits_per_sample == 32) {
                float v; std::memcpy(&v, p, 4); return v;
            }
            if (info.bits_per_sample == 16) {
                int16_t s; std::memcpy(&s, p, 2);
                return s / 32768.0f;
            }
            if (info.bits_per_sample == 24) {
                int32_t s = static_cast<int32_t>(p[0])
                          |(static_cast<int32_t>(p[1]) << 8)
                          |(static_cast<int32_t>(p[2]) << 16);
                if (s & 0x800000) s |= 0xFF000000; // sign extend
                return s / 8388608.0f;
            }
            return 0.0f;
        };

        if (info.channels == 1) {
            float v     = read_sample(frame);
            out_left[i]  = v;
            out_right[i] = v;
        } else {
            out_left [i] = read_sample(frame);
            out_right[i] = read_sample(frame + bytes_per_sample);
        }
    }

    return true;
}

} // anonymous namespace


// ═══════════════════════════════════════════════════════════════════════════════
// Loaded sample (one zone's audio data)
// ═══════════════════════════════════════════════════════════════════════════════

struct LoadedSample {
    DsZoneData  meta;               // Original zone metadata.
    std::vector<float> left;        // Decoded float32 left channel.
    std::vector<float> right;       // Decoded float32 right channel.
    uint32_t    file_sample_rate;   // Native sample rate of the WAV.
    float       gain_linear;        // Pre-computed linear volume gain.
    float       pan_l, pan_r;       // Pre-computed stereo pan coefficients.

    int frames() const noexcept {
        return static_cast<int>(left.size());
    }
};


// ═══════════════════════════════════════════════════════════════════════════════
// ADSR state machine
// ═══════════════════════════════════════════════════════════════════════════════

enum class AdsrStage { Idle, Attack, Decay, Sustain, Release };

struct AdsrState {
    AdsrStage stage   = AdsrStage::Idle;
    float     level   = 0.0f;   // current amplitude 0-1
    float     inc     = 0.0f;   // per-sample delta for current stage
    int       counter = 0;      // samples remaining in current stage

    void start(float attack_s, float decay_s, float sustain,
               float release_s, float sr) {
        stage = AdsrStage::Attack;
        level = 0.0f;
        int atk = std::max(1, static_cast<int>(attack_s  * sr));
        inc     = 1.0f / static_cast<float>(atk);
        counter = atk;
        // Store decay/sustain/release for later stages.
        _decay_s   = decay_s;
        _sustain   = sustain;
        _release_s = release_s;
        _sr        = sr;
    }

    void release() {
        if (stage == AdsrStage::Idle) return;
        stage = AdsrStage::Release;
        int rel = std::max(1, static_cast<int>(_release_s * _sr));
        inc     = -level / static_cast<float>(rel);
        counter = rel;
    }

    // Advance one sample; returns the current amplitude (0 = done).
    float tick() {
        switch (stage) {
        case AdsrStage::Attack:
            level += inc;
            if (--counter <= 0 || level >= 1.0f) {
                level = 1.0f;
                stage = AdsrStage::Decay;
                int dec = std::max(1, static_cast<int>(_decay_s * _sr));
                inc     = (_sustain - 1.0f) / static_cast<float>(dec);
                counter = dec;
            }
            break;
        case AdsrStage::Decay:
            level += inc;
            if (--counter <= 0 || level <= _sustain) {
                level = _sustain;
                stage = AdsrStage::Sustain;
                inc   = 0.0f;
            }
            break;
        case AdsrStage::Sustain:
            break;
        case AdsrStage::Release:
            level += inc;
            if (--counter <= 0 || level <= 0.0f) {
                level = 0.0f;
                stage = AdsrStage::Idle;
            }
            break;
        case AdsrStage::Idle:
            return 0.0f;
        }
        return level;
    }

    bool is_idle() const noexcept { return stage == AdsrStage::Idle; }

private:
    float _decay_s   = 0.1f;
    float _sustain   = 1.0f;
    float _release_s = 0.3f;
    float _sr        = 44100.0f;
};


// ═══════════════════════════════════════════════════════════════════════════════
// Voice
// ═══════════════════════════════════════════════════════════════════════════════

struct Voice {
    bool     active       = false;
    int      note         = 0;
    int      velocity     = 0;
    int      sample_idx   = -1;   // Index into Impl::samples_
    double   position     = 0.0;  // Current read position in the sample (fractional).
    double   rate         = 1.0;  // Playback rate (pitch shift factor).
    AdsrState adsr;

    // Global multipliers from set_parameter().
    float    attack_mul   = 1.0f;
    float    decay_mul    = 1.0f;
    float    release_mul  = 1.0f;
    float    sustain_add  = 0.0f; // additive (unused currently, kept for symmetry)
};


// ═══════════════════════════════════════════════════════════════════════════════
// Lock-free MIDI event ring (single-producer, single-consumer)
// ═══════════════════════════════════════════════════════════════════════════════

struct MidiEvent {
    enum class Type : uint8_t { NoteOn, NoteOff, AllOff } type;
    uint8_t note;
    uint8_t velocity;
};

static constexpr int EVENT_RING_SIZE = 256;

struct EventRing {
    std::array<MidiEvent, EVENT_RING_SIZE> ring;
    std::atomic<int> write_head{0};
    std::atomic<int> read_head{0};

    bool push(MidiEvent ev) {
        int w = write_head.load(std::memory_order_relaxed);
        int next = (w + 1) % EVENT_RING_SIZE;
        if (next == read_head.load(std::memory_order_acquire))
            return false; // ring full
        ring[w] = ev;
        write_head.store(next, std::memory_order_release);
        return true;
    }

    bool pop(MidiEvent& out) {
        int r = read_head.load(std::memory_order_relaxed);
        if (r == write_head.load(std::memory_order_acquire))
            return false;
        out = ring[r];
        read_head.store((r + 1) % EVENT_RING_SIZE, std::memory_order_release);
        return true;
    }
};


// ═══════════════════════════════════════════════════════════════════════════════
// pImpl implementation
// ═══════════════════════════════════════════════════════════════════════════════

struct DecentSamplerEngine::Impl {
    float sr          = 44100.0f;
    int   block_size  = 512;

    std::vector<LoadedSample> samples_;
    std::array<Voice, DecentSamplerEngine::MAX_VOICES> voices_;

    // RR counters per zone group_index (using seq_length as group boundary).
    std::vector<int> rr_counters_;

    // Global parameter multipliers applied at note-on.
    float p_attack_mul  = 1.0f;
    float p_decay_mul   = 1.0f;
    float p_sustain     = -1.0f;  // -1 = use zone default
    float p_release_mul = 1.0f;
    float p_master_gain = 1.0f;   // linear

    EventRing events_;

    // -----------------------------------------------------------------

    void clear_voices() {
        for (auto& v : voices_) v.active = false;
    }

    // Find a free voice; steal the oldest active one if all are busy.
    Voice* alloc_voice() {
        for (auto& v : voices_)
            if (!v.active) return &v;
        // Steal: find earliest-started (largest position).
        Voice* victim = &voices_[0];
        double max_pos = -1.0;
        for (auto& v : voices_) {
            if (v.position > max_pos) { max_pos = v.position; victim = &v; }
        }
        return victim;
    }

    // Find the best matching zone for (note, velocity) considering RR.
    int find_zone(int note, int velocity) {
        // Collect all candidate zones.
        struct Candidate { int idx; int seq_pos; int seq_len; };
        std::vector<Candidate> candidates;
        for (int i = 0; i < static_cast<int>(samples_.size()); ++i) {
            const auto& m = samples_[i].meta;
            if (note < m.lo_note || note > m.hi_note) continue;
            if (velocity < m.lo_vel || velocity > m.hi_vel) continue;
            candidates.push_back({i, m.seq_position, m.seq_length});
        }
        if (candidates.empty()) return -1;

        // Group by seq_length to handle RR.
        // For simplicity: use a counter per unique (lo_note, hi_note, seq_length) group.
        // Since groups have the same note range and seq_length, we hash by that.
        const int seq_len = candidates[0].seq_len;
        int       rr_key  = candidates[0].idx; // use first candidate idx as key
        if (rr_key >= static_cast<int>(rr_counters_.size()))
            rr_counters_.resize(rr_key + 1, 0);

        int rr_pos = (rr_counters_[rr_key] % seq_len) + 1; // 1-based
        ++rr_counters_[rr_key];

        // Pick the zone at the RR position.
        for (const auto& c : candidates) {
            if (c.seq_pos == rr_pos) return c.idx;
        }
        // Fallback: first candidate.
        return candidates[0].idx;
    }

    void process_note_on(int note, int velocity) {
        int zone_idx = find_zone(note, velocity);
        if (zone_idx < 0) return;

        const LoadedSample& s  = samples_[zone_idx];
        const DsZoneData&   m  = s.meta;
        Voice* v = alloc_voice();

        v->active     = true;
        v->note       = note;
        v->velocity   = velocity;
        v->sample_idx = zone_idx;
        v->position   = 0.0;

        // Pitch shift: each semitone = 2^(1/12) ratio.
        double semitones = static_cast<double>(note - m.root_note);
        // Also account for potential sample-rate difference.
        double sr_ratio  = s.file_sample_rate > 0
                         ? static_cast<double>(s.file_sample_rate) / sr
                         : 1.0;
        v->rate = sr_ratio * std::pow(2.0, semitones / 12.0);

        float atk  = m.attack  * p_attack_mul;
        float dec  = m.decay   * p_decay_mul;
        float sus  = (p_sustain >= 0.0f) ? p_sustain : m.sustain;
        float rel  = m.release * p_release_mul;
        v->adsr.start(atk, dec, sus, rel, sr);
    }

    void process_note_off(int note) {
        for (auto& v : voices_) {
            if (v.active && v.note == note)
                v.adsr.release();
        }
    }

    void process_all_off() {
        for (auto& v : voices_) {
            if (v.active)
                v.adsr.release();
        }
    }

    void flush_events() {
        MidiEvent ev;
        while (events_.pop(ev)) {
            switch (ev.type) {
            case MidiEvent::Type::NoteOn:
                process_note_on(ev.note, ev.velocity);
                break;
            case MidiEvent::Type::NoteOff:
                process_note_off(ev.note);
                break;
            case MidiEvent::Type::AllOff:
                process_all_off();
                break;
            }
        }
    }

    // Render one voice into stereo accumulators.
    void render_voice(Voice& v, float* left, float* right, int n) {
        if (!v.active || v.sample_idx < 0) return;
        const LoadedSample& s = samples_[v.sample_idx];
        if (s.frames() == 0) { v.active = false; return; }

        float vel_gain = v.velocity / 127.0f;
        const int loop_start = s.meta.loop_start;
        const int loop_end   = (s.meta.loop_end < 0 || s.meta.loop_end >= s.frames())
                               ? (s.frames() - 1) : s.meta.loop_end;
        const bool looping   = s.meta.loop_enabled
                               && loop_end > loop_start
                               && loop_end < s.frames();

        for (int i = 0; i < n; ++i) {
            int    pos0 = static_cast<int>(v.position);
            float  frac = static_cast<float>(v.position - pos0);

            if (pos0 >= s.frames() - 1) {
                if (looping) {
                    v.position = static_cast<double>(loop_start)
                                + (v.position - static_cast<double>(loop_end));
                    pos0 = static_cast<int>(v.position);
                    frac = static_cast<float>(v.position - pos0);
                } else {
                    v.active = false;
                    break;
                }
            }

            int pos1 = pos0 + 1;
            if (pos1 >= s.frames()) pos1 = pos0;

            // Linear interpolation.
            float lsample = s.left [pos0] + frac * (s.left [pos1] - s.left [pos0]);
            float rsample = s.right[pos0] + frac * (s.right[pos1] - s.right[pos0]);

            float env = v.adsr.tick();
            if (v.adsr.is_idle()) { v.active = false; }

            float gain = env * vel_gain * s.gain_linear * p_master_gain;
            left [i] += lsample * gain * s.pan_l;
            right[i] += rsample * gain * s.pan_r;

            v.position += v.rate;

            // Loop boundary check inside the loop (for short loops).
            if (looping && static_cast<int>(v.position) >= loop_end) {
                v.position = static_cast<double>(loop_start)
                            + (v.position - static_cast<double>(loop_end));
            }
        }
    }
};


// ═══════════════════════════════════════════════════════════════════════════════
// DecentSamplerEngine public methods
// ═══════════════════════════════════════════════════════════════════════════════

DecentSamplerEngine::DecentSamplerEngine(float sr, int block_size)
    : impl_(std::make_unique<Impl>())
    , sample_rate_(sr)
    , block_size_(block_size)
{
    impl_->sr         = sr;
    impl_->block_size = block_size;
    impl_->clear_voices();
}

DecentSamplerEngine::~DecentSamplerEngine() = default;

void DecentSamplerEngine::set_sample_rate(float sr) {
    sample_rate_ = sr;
    impl_->sr    = sr;
}

void DecentSamplerEngine::set_block_size(int bs) {
    block_size_       = bs;
    impl_->block_size = bs;
}

bool DecentSamplerEngine::load_zones(const std::vector<DsZoneData>& zones) {
    impl_->clear_voices();
    impl_->samples_.clear();
    impl_->rr_counters_.clear();
    loaded_ = false;

    int ok = 0;
    for (const auto& z : zones) {
        LoadedSample ls;
        ls.meta = z;

        uint32_t file_sr = 0;
        if (!load_wav(z.path, ls.left, ls.right, file_sr)) {
            continue; // skip bad files silently
        }
        ls.file_sample_rate = file_sr;

        // Pre-compute linear gain from dB volume.
        ls.gain_linear = std::pow(10.0f, z.volume_db / 20.0f);

        // Pre-compute stereo pan (constant-power law).
        float pan_norm = std::max(-1.0f, std::min(1.0f, z.pan / 100.0f));
        float angle    = (pan_norm + 1.0f) * 0.25f * 3.14159265f; // 0 … π/2
        ls.pan_l = std::cos(angle);
        ls.pan_r = std::sin(angle);

        impl_->samples_.push_back(std::move(ls));
        ++ok;
    }

    loaded_ = ok > 0;
    return loaded_;
}

bool DecentSamplerEngine::load_preset(const std::string& /*path*/) {
    // load_preset via path requires calling the Python XML parser.
    // This C++ class does not embed an XML parser; the Python dspreset_engine
    // factory calls parse_dspreset() + load_zones() directly.
    // Stub: return false; Python side always uses load_zones().
    return false;
}

bool DecentSamplerEngine::is_loaded() const noexcept { return loaded_; }

int  DecentSamplerEngine::zone_count() const noexcept {
    return static_cast<int>(impl_->samples_.size());
}

void DecentSamplerEngine::note_on(int /*channel*/, int note, int velocity) {
    MidiEvent ev;
    ev.type     = MidiEvent::Type::NoteOn;
    ev.note     = static_cast<uint8_t>(std::max(0, std::min(127, note)));
    ev.velocity = static_cast<uint8_t>(std::max(0, std::min(127, velocity)));
    impl_->events_.push(ev);
}

void DecentSamplerEngine::note_off(int /*channel*/, int note, int /*velocity*/) {
    MidiEvent ev;
    ev.type     = MidiEvent::Type::NoteOff;
    ev.note     = static_cast<uint8_t>(std::max(0, std::min(127, note)));
    ev.velocity = 0;
    impl_->events_.push(ev);
}

void DecentSamplerEngine::all_notes_off(int /*channel*/) {
    MidiEvent ev;
    ev.type     = MidiEvent::Type::AllOff;
    ev.note     = 0;
    ev.velocity = 0;
    impl_->events_.push(ev);
}

void DecentSamplerEngine::set_parameter(const std::string& name, float value) {
    if      (name == "ENV_ATTACK")    impl_->p_attack_mul  = std::max(0.0f, value * 2.0f);
    else if (name == "ENV_DECAY")     impl_->p_decay_mul   = std::max(0.0f, value * 2.0f);
    else if (name == "ENV_SUSTAIN")   impl_->p_sustain     = std::max(0.0f, std::min(1.0f, value));
    else if (name == "ENV_RELEASE")   impl_->p_release_mul = std::max(0.0f, value * 2.0f);
    else if (name == "MASTER_VOLUME") {
        // value expected in dB (-60 to +12).
        impl_->p_master_gain = std::pow(10.0f, value / 20.0f);
    }
}

void DecentSamplerEngine::render(float* left, float* right, int num_samples) {
    // Zero output.
    std::fill(left,  left  + num_samples, 0.0f);
    std::fill(right, right + num_samples, 0.0f);

    if (!loaded_) return;

    // Flush MIDI events into voices.
    impl_->flush_events();

    // Render each active voice.
    for (auto& v : impl_->voices_) {
        if (!v.active) continue;
        impl_->render_voice(v, left, right, num_samples);
    }
}
