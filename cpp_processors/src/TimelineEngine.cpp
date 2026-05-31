/*
 * TimelineEngine.cpp  --  Crystal DAW C++ Timeline Engine Implementation
 * =======================================================================
 * See TimelineEngine.h for the full architecture description.
 *
 * Key implementation notes
 * ------------------------
 * • process_block_into() is the only function called from the audio callback
 *   thread.  Every other public method is called from the Python / GUI thread.
 *
 * • _tracks_mutex is acquired ONCE per process_block_into() call to copy the
 *   routing parameters of active tracks into local variables; the mutex is
 *   released before the inner sample loop.  This prevents the audio callback
 *   from being blocked by slow GUI writes.
 *
 * • _pending_midi_mutex protects the small vector of pending MIDI events.
 *   process_block_into() appends to it; pop_midi_events() swaps it out.  The
 *   critical section is very short in both cases.
 *
 * • Sampler::process() ADDS into the destination buffers.  We must therefore
 *   zero _scratch_left/_scratch_right before calling it.
 */

#include "TimelineEngine.h"

#include <algorithm>
#include <cassert>
#include <cstring>
#include <stdexcept>

// =============================================================================
// Construction / destruction
// =============================================================================

TimelineEngine::TimelineEngine(int sample_rate, double bpm)
    : _sample_rate(sample_rate)
    , _bpm(std::max(20.0, std::min(300.0, bpm)))
{
    /* Pre-zero scratch buffers so the very first block is silence, not garbage. */
    std::fill(_scratch_left,  _scratch_left  + MAX_BLOCK_SIZE, 0.0f);
    std::fill(_scratch_right, _scratch_right + MAX_BLOCK_SIZE, 0.0f);
}

TimelineEngine::~TimelineEngine()
{
    /* Stop the transport so no audio callback can enter process_block_into
     * after the tracks vector starts being destroyed. */
    _is_playing.store(false, std::memory_order_seq_cst);
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    _tracks.clear();   // destroys all unique_ptr<Sampler> objects
}

// =============================================================================
// Transport
// =============================================================================

void TimelineEngine::play(int64_t from_frame)
{
    /* Set the playhead first, then enable the flag so process_block_into()
     * never sees an inconsistent state. */
    _current_frame.store(std::max(int64_t{0}, from_frame),
                         std::memory_order_relaxed);
    _is_playing.store(true, std::memory_order_release);
}

void TimelineEngine::stop()
{
    _is_playing.store(false, std::memory_order_release);
}

void TimelineEngine::seek(int64_t frame)
{
    _current_frame.store(std::max(int64_t{0}, frame),
                         std::memory_order_relaxed);
}

void TimelineEngine::set_loop(bool enabled, int64_t start_frame, int64_t end_frame)
{
    std::lock_guard<std::mutex> lk(_loop_mutex);
    _loop_enabled     = enabled;
    _loop_start_frame = start_frame;
    _loop_end_frame   = std::max(start_frame + 1, end_frame);
}

// =============================================================================
// Thread-safe playhead queries
// =============================================================================

int64_t TimelineEngine::current_frame() const noexcept
{
    return _current_frame.load(std::memory_order_relaxed);
}

double TimelineEngine::current_beat() const noexcept
{
    /* samples_per_beat = (60 / BPM) * sample_rate */
    const double spb = (60.0 / _bpm) * static_cast<double>(_sample_rate);
    return static_cast<double>(_current_frame.load(std::memory_order_relaxed)) / spb;
}

bool TimelineEngine::is_playing() const noexcept
{
    return _is_playing.load(std::memory_order_relaxed);
}

// =============================================================================
// Host settings
// =============================================================================

void TimelineEngine::set_bpm(double bpm)
{
    _bpm = std::max(20.0, std::min(300.0, bpm));
}

void TimelineEngine::set_sample_rate(int sr)
{
    _sample_rate = sr;
    /* Inform all Sampler instances so pitch calculations remain correct. */
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    for (auto& t : _tracks) {
        if (t.sampler) {
            t.sampler->prepare(static_cast<float>(sr));
        }
    }
}

double TimelineEngine::bpm()         const noexcept { return _bpm;         }
int    TimelineEngine::sample_rate() const noexcept { return _sample_rate; }

// =============================================================================
// Track management
// =============================================================================

int TimelineEngine::add_audio_track()
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    const int id = _next_track_id++;
    TimelineTrack t;
    t.id   = id;
    t.type = TimelineTrackType::AUDIO;
    _tracks.push_back(std::move(t));
    return id;
}

int TimelineEngine::add_instrument_track()
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    const int id = _next_track_id++;
    TimelineTrack t;
    t.id   = id;
    t.type = TimelineTrackType::INSTRUMENT;
    _tracks.push_back(std::move(t));
    return id;
}

void TimelineEngine::remove_track(int id)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    _tracks.erase(
        std::remove_if(_tracks.begin(), _tracks.end(),
                       [id](const TimelineTrack& t){ return t.id == id; }),
        _tracks.end());
}

int TimelineEngine::track_count() const
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    return static_cast<int>(_tracks.size());
}

// =============================================================================
// Internal find-by-id helpers
// =============================================================================

TimelineTrack* TimelineEngine::_find_track(int id)
{
    for (auto& t : _tracks)
        if (t.id == id) return &t;
    return nullptr;
}

const TimelineTrack* TimelineEngine::_find_track(int id) const
{
    for (const auto& t : _tracks)
        if (t.id == id) return &t;
    return nullptr;
}

// =============================================================================
// Per-track routing
// =============================================================================

void TimelineEngine::set_track_volume(int id, float v)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    if (auto* t = _find_track(id)) t->volume = std::max(0.0f, v);
}

void TimelineEngine::set_track_pan(int id, float p)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    if (auto* t = _find_track(id)) t->pan = std::max(-1.0f, std::min(1.0f, p));
}

void TimelineEngine::set_track_mute(int id, bool m)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    if (auto* t = _find_track(id)) t->muted = m;
}

void TimelineEngine::set_track_solo(int id, bool s)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    if (auto* t = _find_track(id)) t->soloed = s;
}

float TimelineEngine::get_track_volume(int id) const
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    const auto* t = _find_track(id);
    return t ? t->volume : 1.0f;
}

float TimelineEngine::get_track_pan(int id) const
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    const auto* t = _find_track(id);
    return t ? t->pan : 0.0f;
}

bool TimelineEngine::get_track_mute(int id) const
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    const auto* t = _find_track(id);
    return t ? t->muted : false;
}

bool TimelineEngine::get_track_solo(int id) const
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    const auto* t = _find_track(id);
    return t ? t->soloed : false;
}

// =============================================================================
// Audio clip management
// =============================================================================

void TimelineEngine::load_audio_clip(int track_id,
                                      const std::vector<float>& left,
                                      const std::vector<float>& right,
                                      int64_t start_frame,
                                      const std::string& path)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    auto* t = _find_track(track_id);
    if (!t || t->type != TimelineTrackType::AUDIO) return;

    TimelineAudioClip clip;
    clip.start_frame = start_frame;
    clip.num_frames  = static_cast<int64_t>(left.size());
    clip.left        = left;
    /* Mono source: duplicate left into right so process() can always read both. */
    clip.right = right.empty() ? left : right;
    clip.path  = path;
    t->audio_clips.push_back(std::move(clip));
}

void TimelineEngine::clear_audio_clips(int track_id)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    if (auto* t = _find_track(track_id)) t->audio_clips.clear();
}

// =============================================================================
// MIDI event management
// =============================================================================

void TimelineEngine::add_midi_event(int track_id, int64_t frame_pos,
                                     uint8_t type, uint8_t channel,
                                     uint8_t note, uint8_t velocity)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    auto* t = _find_track(track_id);
    if (!t || t->type != TimelineTrackType::INSTRUMENT) return;

    TimelineMidiEvent ev;
    ev.frame    = frame_pos;
    ev.type     = type;
    ev.channel  = channel;
    ev.note     = note;
    ev.velocity = velocity;
    t->midi_events.push_back(ev);
}

void TimelineEngine::clear_midi_events(int track_id)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    if (auto* t = _find_track(track_id)) t->midi_events.clear();
}

void TimelineEngine::sort_midi_events(int track_id)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    if (auto* t = _find_track(track_id)) {
        std::stable_sort(t->midi_events.begin(), t->midi_events.end(),
                         [](const TimelineMidiEvent& a, const TimelineMidiEvent& b){
                             return a.frame < b.frame;
                         });
    }
}

// =============================================================================
// Sampler loading
// =============================================================================

bool TimelineEngine::load_sample(int track_id,
                                  const std::vector<float>& left,
                                  const std::vector<float>& right,
                                  int file_sample_rate,
                                  int midi_root_note)
{
    std::lock_guard<std::mutex> lk(_tracks_mutex);
    auto* t = _find_track(track_id);
    if (!t || t->type != TimelineTrackType::INSTRUMENT) return false;

    /* Create the Sampler if this is the first load call for this track. */
    if (!t->sampler) {
        t->sampler = std::make_unique<Sampler>(
            static_cast<float>(_sample_rate));
    }
    t->sampler->set_root_note(midi_root_note);

    /* Build an interleaved stereo buffer that Sampler::load_sample() expects.
     * Format: [L0, R0, L1, R1, ...]  or  [S0, S1, ...]  for mono. */
    const auto& r = right.empty() ? left : right;
    const int   channels = right.empty() ? 1 : 2;
    const size_t n_frames = left.size();
    const size_t n_total  = n_frames * static_cast<size_t>(channels);

    std::vector<float> interleaved(n_total);
    if (channels == 1) {
        /* Mono: copy left directly. */
        std::copy(left.begin(), left.end(), interleaved.begin());
    } else {
        /* Stereo: interleave L and R. */
        for (size_t i = 0; i < n_frames; ++i) {
            interleaved[i * 2    ] = left[i];
            interleaved[i * 2 + 1] = r[i];
        }
    }

    t->sampler->load_sample(interleaved.data(),
                            static_cast<int>(n_total),
                            static_cast<float>(file_sample_rate),
                            channels);
    return t->sampler->sample_loaded();
}

// =============================================================================
// Pending MIDI queue
// =============================================================================

std::vector<PendingMidiEvent> TimelineEngine::pop_midi_events()
{
    std::vector<PendingMidiEvent> out;
    std::lock_guard<std::mutex> lk(_pending_midi_mutex);
    out.swap(_pending_midi);   // O(1) swap; out now owns the data
    return out;
}

// =============================================================================
// Internal: solo check
// =============================================================================

bool TimelineEngine::_any_soloed() const
{
    for (const auto& t : _tracks)
        if (t.soloed) return true;
    return false;
}

// =============================================================================
// Internal: render an AUDIO track into _scratch_left / _scratch_right
// =============================================================================

void TimelineEngine::_render_audio_track(const TimelineTrack& t,
                                          int64_t block_start, int n)
{
    /* Zero the scratch buffers before accumulating clips. */
    std::fill(_scratch_left,  _scratch_left  + n, 0.0f);
    std::fill(_scratch_right, _scratch_right + n, 0.0f);

    for (const auto& clip : t.audio_clips) {
        /* Determine the overlap between [block_start, block_start+n) and
         * [clip.start_frame, clip.start_frame + clip.num_frames). */
        const int64_t clip_end    = clip.start_frame + clip.num_frames;
        const int64_t overlap_beg = std::max(block_start,  clip.start_frame);
        const int64_t overlap_end = std::min(block_start + n, clip_end);

        if (overlap_beg >= overlap_end) continue;  // no overlap

        const int64_t out_offset  = overlap_beg - block_start;  // where to write
        const int64_t clip_offset = overlap_beg - clip.start_frame;  // where to read
        const int64_t count       = overlap_end - overlap_beg;

        /* Accumulate PCM data into the scratch buffers. */
        for (int64_t s = 0; s < count; ++s) {
            _scratch_left [out_offset + s] += clip.left [clip_offset + s];
            _scratch_right[out_offset + s] += clip.right[clip_offset + s];
        }
    }
}

// =============================================================================
// Internal: render an INSTRUMENT track (MIDI dispatch + Sampler audio)
// =============================================================================

void TimelineEngine::_render_instrument_track(TimelineTrack& t,
                                               int64_t block_start, int n)
{
    /* Sampler::process() ADDS audio; zero the scratch buffers first so we start
     * from silence rather than accumulating into leftover data. */
    std::fill(_scratch_left,  _scratch_left  + n, 0.0f);
    std::fill(_scratch_right, _scratch_right + n, 0.0f);

    /* Fire MIDI events that fall inside [block_start, block_start + n). */
    for (const auto& ev : t.midi_events) {
        if (ev.frame <  block_start)     continue;
        if (ev.frame >= block_start + n) break;   // events are sorted ascending

        const bool is_on = (ev.type == 0x90) && (ev.velocity > 0);

        /* Drive the built-in Sampler so the instrument renders its audio. */
        if (t.sampler) {
            if (is_on) {
                t.sampler->note_on(ev.note,
                                   static_cast<float>(ev.velocity) / 127.0f);
            } else {
                t.sampler->note_off(ev.note);
            }
        }

        /* Deposit the event in the pending queue so Python can forward it to
         * FluidSynth or any other synthesizer without holding the audio lock. */
        {
            std::lock_guard<std::mutex> lk(_pending_midi_mutex);
            _pending_midi.push_back({
                static_cast<int>(ev.channel),
                static_cast<int>(ev.note),
                static_cast<int>(ev.velocity),
                is_on
            });
        }
    }

    /* Render one block of Sampler audio (adds into zeroed scratch buffers). */
    if (t.sampler) {
        t.sampler->process(_scratch_left, _scratch_right, n);
    }
}

// =============================================================================
// process_block_into  —  the hot audio path
// =============================================================================

void TimelineEngine::process_block_into(float* out_left, float* out_right,
                                         int n_samples)
{
    /* Clamp to the maximum supported block size. */
    n_samples = std::min(n_samples, MAX_BLOCK_SIZE);

    /* Zero the output buffers; we accumulate all tracks into them. */
    std::fill(out_left,  out_left  + n_samples, 0.0f);
    std::fill(out_right, out_right + n_samples, 0.0f);

    /* Exit early if transport is stopped — output stays silent. */
    if (!_is_playing.load(std::memory_order_acquire)) return;

    const int64_t block_start = _current_frame.load(std::memory_order_relaxed);

    /* Snapshot loop settings under their own lightweight lock. */
    bool    loop_en  = false;
    int64_t loop_beg = 0;
    int64_t loop_end = 0;
    {
        std::lock_guard<std::mutex> llk(_loop_mutex);
        loop_en  = _loop_enabled;
        loop_beg = _loop_start_frame;
        loop_end = _loop_end_frame;
    }

    /* ── Per-track rendering ──────────────────────────────────────────────── */
    {
        std::lock_guard<std::mutex> lk(_tracks_mutex);
        const bool any_solo = _any_soloed();

        for (auto& track : _tracks) {
            /* Skip silent or non-soloed tracks without touching the output. */
            if (track.muted)               continue;
            if (any_solo && !track.soloed) continue;

            /* Render the track into _scratch_left / _scratch_right. */
            if (track.type == TimelineTrackType::AUDIO) {
                _render_audio_track(track, block_start, n_samples);
            } else {
                _render_instrument_track(track, block_start, n_samples);
            }

            /* Linear pan law:
             *   pan =  0.0 → left_gain = volume, right_gain = volume
             *   pan = -1.0 → left_gain = volume, right_gain = 0
             *   pan = +1.0 → left_gain = 0,      right_gain = volume
             */
            const float left_gain  = track.volume * (track.pan <= 0.0f
                                                      ? 1.0f
                                                      : 1.0f - track.pan);
            const float right_gain = track.volume * (track.pan >= 0.0f
                                                      ? 1.0f
                                                      : 1.0f + track.pan);

            /* Accumulate this track's audio into the master output. */
            for (int s = 0; s < n_samples; ++s) {
                out_left [s] += _scratch_left [s] * left_gain;
                out_right[s] += _scratch_right[s] * right_gain;
            }
        }
    } /* _tracks_mutex released here — all remaining work is lock-free. */

    /* ── Advance the atomic playhead ─────────────────────────────────────── */
    int64_t new_frame = block_start + static_cast<int64_t>(n_samples);

    if (loop_en && new_frame >= loop_end) {
        /* Wrap around to the loop start.  Partial blocks near the boundary
         * are handled: the overshoot is discarded for simplicity (audible as
         * a sub-block gap of ≤ one block at the wrap point). */
        new_frame = loop_beg;
    }

    _current_frame.store(new_frame, std::memory_order_relaxed);
}
