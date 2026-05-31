/*
 * TimelineEngine.h  --  C++ Real-Time Audio / MIDI Timeline Engine
 * =================================================================
 * Central scheduling and mixing engine for Crystal DAW.
 *
 * Design goals
 * ------------
 * 1. Frame-accurate playhead  — an std::atomic<int64_t> sample-frame counter is
 *    the single source of truth for transport position.  The 60 Hz Qt UI timer
 *    can read current_frame() / current_beat() from any thread with zero locks.
 *
 * 2. Zero-allocation audio path  — process_block_into() uses pre-allocated
 *    scratch buffers (members of this class) so no heap operations occur
 *    inside the sounddevice audio callback.
 *
 * 3. Audio-track mixing in C++  — raw PCM clips stored in float32 vectors are
 *    read, volume/pan-scaled, and summed into the stereo output buffer entirely
 *    in native code.  This replaces the previous pygame-based AudioFilePlayer.
 *
 * 4. MIDI event scheduling  — MIDI note events are stored in C++ sorted by
 *    absolute sample-frame.  process_block_into() deposits any events that fall
 *    inside the current block into a pending queue.  Python's refresh timer (or
 *    the sounddevice callback) drains the queue and forwards events to FluidSynth.
 *    This keeps the GIL out of the hot audio path.
 *
 * 5. Sampler-per-track instrument rendering  — each INSTRUMENT track optionally
 *    owns a Sampler instance.  process_block_into() renders its audio directly
 *    into the mix bus in C++, bypassing Python entirely.
 *
 * Thread-safety model
 * -------------------
 * ┌────────────────────┬──────────────────────────────────────────────────┐
 * │ Thread             │ Allowed calls                                    │
 * ├────────────────────┼──────────────────────────────────────────────────┤
 * │ Audio callback     │ process_block_into(), pop_midi_events(),          │
 * │                    │ current_frame(), current_beat(), is_playing()     │
 * │ GUI / Python       │ Everything else (guarded by _tracks_mutex)       │
 * └────────────────────┴──────────────────────────────────────────────────┘
 *
 * process_block_into() takes _tracks_mutex once per call to snapshot routing
 * parameters, then releases it before the inner sample loop.  This ensures
 * the audio callback never waits for slow GUI operations.
 */

#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "DspHelpers.h"   // MAX_BLOCK_SIZE
#include "Sampler.h"      // polyphonic sample-playback instrument

// =============================================================================
// MIDI event record
// =============================================================================

/**
 * A single MIDI note event stored on the C++ timeline.
 * Events are sorted ascending by frame inside each track.
 */
struct TimelineMidiEvent {
    int64_t  frame;      /**< Absolute sample-frame position on the timeline. */
    uint8_t  type;       /**< 0x90 = note-on, 0x80 = note-off.                */
    uint8_t  channel;    /**< MIDI channel 0-15.                               */
    uint8_t  note;       /**< MIDI note number 0-127.                          */
    uint8_t  velocity;   /**< Velocity 0-127 (0 also means note-off).          */
};

// =============================================================================
// Audio clip record
// =============================================================================

/**
 * One pre-loaded audio clip on an AUDIO track.
 * Left/right PCM data are stored as separate float32 vectors to allow SIMD
 * processing without deinterleaving.  Mono sources copy left → right.
 */
struct TimelineAudioClip {
    int64_t  start_frame;  /**< Timeline position (samples from project start). */
    int64_t  num_frames;   /**< Total number of sample frames in the clip.       */
    std::vector<float> left;   /**< Left-channel PCM, normalised ±1.             */
    std::vector<float> right;  /**< Right-channel PCM, same length as left.      */
    std::string        path;   /**< Original file path (informational only).     */
};

// =============================================================================
// Track type
// =============================================================================

enum class TimelineTrackType : int {
    AUDIO      = 0,   /**< Contains TimelineAudioClip objects.                 */
    INSTRUMENT = 1,   /**< Contains MIDI events + optional Sampler instance.   */
};

// =============================================================================
// Track record
// =============================================================================

/**
 * One mixer track.  Accessed by both the GUI thread (routing changes) and the
 * audio callback (process_block_into).  All writes go through _tracks_mutex.
 */
struct TimelineTrack {
    int               id   = 0;
    TimelineTrackType type = TimelineTrackType::AUDIO;

    /* Routing — applied by process_block_into after per-track rendering. */
    float volume = 1.0f;   /**< Output gain multiplier (0 = mute, 2 = +6 dB). */
    float pan    = 0.0f;   /**< Stereo position -1 (L) … 0 (C) … +1 (R).      */
    bool  muted  = false;
    bool  soloed = false;

    /* AUDIO track payload — set via load_audio_clip(). */
    std::vector<TimelineAudioClip> audio_clips;

    /* INSTRUMENT track payload. */
    std::vector<TimelineMidiEvent> midi_events; /**< Sorted by frame.          */
    std::unique_ptr<Sampler>       sampler;     /**< Null until load_sample(). */
};

// =============================================================================
// Pending MIDI event (output queue drained by Python after each block)
// =============================================================================

/**
 * A MIDI event that fired during the most recent process_block_into() call.
 * Python drains pop_midi_events() and forwards these to FluidSynth (or any
 * other synthesizer) without the C++ side needing to know about it.
 */
struct PendingMidiEvent {
    int  channel;   /**< MIDI channel 0-15.                  */
    int  note;      /**< MIDI note number 0-127.              */
    int  velocity;  /**< Velocity 0-127 (0 = note-off).      */
    bool is_on;     /**< True = note-on, false = note-off.   */
};

// =============================================================================
// TimelineEngine
// =============================================================================

class TimelineEngine {
public:
    /** Maximum audio block size — matches MAX_BLOCK_SIZE from DspHelpers.h. */
    static constexpr int MAX_BLOCK = MAX_BLOCK_SIZE;   // 4096

    // -------------------------------------------------------------------------
    // Construction
    // -------------------------------------------------------------------------

    /** Create a new engine.  sample_rate and bpm can be changed later. */
    explicit TimelineEngine(int sample_rate = 44100, double bpm = 120.0);

    /** Destructor — stops playback and frees all track data. */
    ~TimelineEngine();

    /* Non-copyable — owns unique_ptr and atomic members. */
    TimelineEngine(const TimelineEngine&)            = delete;
    TimelineEngine& operator=(const TimelineEngine&) = delete;

    // -------------------------------------------------------------------------
    // Transport control  (all GUI-thread safe)
    // -------------------------------------------------------------------------

    /** Start playback from the given sample frame. */
    void play(int64_t from_frame = 0);

    /** Stop playback.  Playhead position is preserved. */
    void stop();

    /** Jump to an absolute sample frame without changing play/stop state. */
    void seek(int64_t frame);

    /** Configure loop region.  start_frame must be < end_frame. */
    void set_loop(bool enabled, int64_t start_frame, int64_t end_frame);

    // -------------------------------------------------------------------------
    // Thread-safe playhead queries  (safe from ANY thread, O(1))
    // -------------------------------------------------------------------------

    /** Current sample-frame position.  Advances atomically during playback. */
    int64_t current_frame()  const noexcept;

    /** Current beat position derived from current_frame() and BPM. */
    double  current_beat()   const noexcept;

    /** True while the transport is running. */
    bool    is_playing()     const noexcept;

    // -------------------------------------------------------------------------
    // Host settings
    // -------------------------------------------------------------------------

    void   set_bpm        (double bpm);
    void   set_sample_rate(int    sr);
    double bpm()          const noexcept;
    int    sample_rate()  const noexcept;

    // -------------------------------------------------------------------------
    // Track management  (GUI thread)
    // -------------------------------------------------------------------------

    /** Add an AUDIO track.  Returns the new track's unique ID. */
    int  add_audio_track();

    /** Add an INSTRUMENT track.  Returns the new track's unique ID. */
    int  add_instrument_track();

    /** Remove a track by ID.  Frees all clips, MIDI events, and Sampler. */
    void remove_track(int id);

    /** Total number of registered tracks. */
    int  track_count() const;

    // -------------------------------------------------------------------------
    // Per-track routing  (GUI thread)
    // -------------------------------------------------------------------------

    void  set_track_volume(int id, float volume);
    void  set_track_pan   (int id, float pan);
    void  set_track_mute  (int id, bool  muted);
    void  set_track_solo  (int id, bool  soloed);

    float get_track_volume(int id) const;
    float get_track_pan   (int id) const;
    bool  get_track_mute  (int id) const;
    bool  get_track_solo  (int id) const;

    // -------------------------------------------------------------------------
    // Audio clip management  (AUDIO tracks, GUI thread)
    // -------------------------------------------------------------------------

    /**
     * Append one pre-loaded PCM clip to an AUDIO track.
     * @param track_id    Target track ID.
     * @param left        Left-channel samples (float32, normalised ±1).
     * @param right       Right-channel samples.  Pass empty to use left (mono).
     * @param start_frame Absolute position on the timeline in sample frames.
     * @param path        Original file path for display only (may be empty).
     */
    void load_audio_clip(int track_id,
                         const std::vector<float>& left,
                         const std::vector<float>& right,
                         int64_t start_frame,
                         const std::string& path = "");

    /** Remove all clips from an AUDIO track. */
    void clear_audio_clips(int track_id);

    // -------------------------------------------------------------------------
    // MIDI event management  (INSTRUMENT tracks, GUI thread)
    // -------------------------------------------------------------------------

    /**
     * Add one MIDI event to an INSTRUMENT track.
     * @param track_id  Target track ID.
     * @param frame_pos Absolute sample-frame position.
     * @param type      0x90 = note-on, 0x80 = note-off.
     * @param channel   MIDI channel 0-15.
     * @param note      MIDI note 0-127.
     * @param velocity  0-127.
     */
    void add_midi_event(int track_id, int64_t frame_pos,
                        uint8_t type, uint8_t channel,
                        uint8_t note, uint8_t velocity);

    /** Remove all MIDI events from an INSTRUMENT track. */
    void clear_midi_events(int track_id);

    /**
     * Re-sort the MIDI event list of one track by frame position.
     * Call this after a batch of add_midi_event() calls to ensure correct order.
     */
    void sort_midi_events(int track_id);

    // -------------------------------------------------------------------------
    // Sampler loading  (INSTRUMENT tracks, GUI thread)
    // -------------------------------------------------------------------------

    /**
     * Load a PCM sample into the Sampler owned by an INSTRUMENT track.
     * Creates the Sampler if it does not yet exist.
     *
     * @param track_id        Target track ID.
     * @param left            Left-channel samples.
     * @param right           Right-channel samples (empty = mono → copy left).
     * @param file_sample_rate Sample rate of the source file in Hz.
     * @param midi_root_note  MIDI note that plays the sample at its original pitch.
     * @return True if the sample loaded successfully.
     */
    bool load_sample(int track_id,
                     const std::vector<float>& left,
                     const std::vector<float>& right,
                     int file_sample_rate,
                     int midi_root_note = 60);

    // -------------------------------------------------------------------------
    // Pending MIDI event queue  (audio callback → Python)
    // -------------------------------------------------------------------------

    /**
     * Drain and return all MIDI events deposited by the most recent
     * process_block_into() call.  Python calls this from the sounddevice
     * callback (or from a 50 Hz refresh timer) and forwards the events to
     * FluidSynth.  Thread-safe: swaps a small vector under a spinlock.
     */
    std::vector<PendingMidiEvent> pop_midi_events();

    // -------------------------------------------------------------------------
    // Main audio processing call  (audio callback thread)
    // -------------------------------------------------------------------------

    /**
     * Mix all active tracks into the caller-provided stereo output buffers.
     *
     * This function is designed to be called from a sounddevice (PortAudio)
     * audio callback.  It:
     *   1. Reads audio clips for every AUDIO track into the output.
     *   2. Fires any MIDI events inside the block window through each track's
     *      Sampler and deposits them in the pending queue for Python to drain.
     *   3. Applies per-track volume + linear pan law.
     *   4. Advances _current_frame atomically by n_samples.
     *   5. Wraps the playhead if loop mode is active.
     *
     * Zero heap allocations — all scratch space is pre-allocated in the
     * class constructor.  n_samples must be ≤ MAX_BLOCK (4096).
     *
     * @param out_left   Pre-allocated float buffer of length n_samples (filled).
     * @param out_right  Pre-allocated float buffer of length n_samples (filled).
     * @param n_samples  Number of frames to render.
     */
    void process_block_into(float* out_left, float* out_right, int n_samples);

private:
    // ── Internal helpers ──────────────────────────────────────────────────────

    TimelineTrack*       _find_track(int id);
    const TimelineTrack* _find_track(int id) const;
    bool _any_soloed() const;   // call with _tracks_mutex held

    /** Render an AUDIO track's clips into _scratch_left / _scratch_right. */
    void _render_audio_track(const TimelineTrack& t, int64_t block_start, int n);

    /** Render an INSTRUMENT track (fire MIDI → Sampler; render Sampler audio). */
    void _render_instrument_track(TimelineTrack& t, int64_t block_start, int n);

    // ── State ─────────────────────────────────────────────────────────────────

    int    _sample_rate;
    double _bpm;

    /* Frame counter — read atomically by the GUI thread, written by audio callback. */
    std::atomic<int64_t> _current_frame { 0 };
    std::atomic<bool>    _is_playing    { false };

    /* Loop region — written by GUI thread, read by audio callback. */
    int64_t _loop_start_frame { 0 };
    int64_t _loop_end_frame   { 0 };
    bool    _loop_enabled     { false };
    mutable std::mutex _loop_mutex;   /* guards loop fields only */

    /* Track list — all mutations go through _tracks_mutex. */
    std::vector<TimelineTrack> _tracks;
    mutable std::mutex         _tracks_mutex;
    int _next_track_id { 1 };

    /* Pending MIDI events deposited by process_block_into(), drained by Python. */
    std::vector<PendingMidiEvent> _pending_midi;
    std::mutex                    _pending_midi_mutex;

    /* Per-block scratch buffers — one pair for the single track being rendered
     * at any moment.  Pre-allocated so process_block_into() is allocation-free. */
    float _scratch_left [MAX_BLOCK_SIZE];
    float _scratch_right[MAX_BLOCK_SIZE];
};
