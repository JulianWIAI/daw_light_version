/*
 * MidiDropImporter.h  --  Async Multi-Track MIDI Drag-and-Drop Import
 * ====================================================================
 *
 * This module owns the background import pipeline that fires when the user
 * drops a .mid file onto the timeline.  It converts a list of Python-assembled
 * MidiTrackPayload objects into live C++ instrument tracks without blocking
 * either the Qt GUI thread or the real-time audio callback.
 *
 *
 * Data flow
 * ---------
 *
 *   [Python GUI thread]
 *        mido parse → list[MidiTrackPayloadPy]
 *        pybind11 conversion → std::vector<MidiTrackPayload>
 *        TimelineEngine::importMultiTrackMidi(payloads)   ← returns immediately
 *              │
 *              │  copies vector onto std::thread
 *              ▼
 *   [MidiDropImporter background thread]
 *        Phase 1 — Data prep (zero locks)
 *          • validate / fill sfz_path via gm_to_sfz_path()
 *          • sort each event vector ascending by abs_frame
 *          • (optional) SFZ file-existence check / pre-warm
 *        Phase 2 — Atomic track injection
 *          • engine.add_instrument_track()   ← brief _tracks_mutex
 *          • engine.add_midi_event() × N     ← brief _tracks_mutex per call
 *          • engine.sort_midi_events()
 *        Phase 3 — Lock-free notification
 *          • enqueue PreparedImportBatch under _result_mutex
 *          • _ready_flag.store(true, release)
 *              │
 *              ▼
 *   [PortAudio / audio callback thread]
 *        check_import_ready()
 *          → _ready_flag.exchange(false, acquire) == true?
 *          → dequeue PreparedImportBatch (single lock, O(1))
 *          → return true  →  GUI notified via MidiLogic signal
 *
 *
 * Thread-safety invariants
 * ------------------------
 *  1. Phase 1 holds NO locks.  It is safe to do blocking disk I/O here.
 *  2. Phase 2 calls only TimelineEngine's public GUI-thread-safe API.
 *     Each call takes _tracks_mutex for an O(1) push, then releases it.
 *     The audio callback acquires _tracks_mutex at most once per 11 ms
 *     (512-sample block at 44 100 Hz) and is never blocked for more than
 *     one push_back.
 *  3. The _ready_flag store uses memory_order_release; the audio thread's
 *     exchange uses memory_order_acquire.  This creates a happens-before
 *     edge: all track/event additions performed before the store are
 *     guaranteed visible to the audio thread once check_import_ready()
 *     returns true.
 *  4. check_import_ready() performs zero heap allocations.  The only lock
 *     it takes is _result_mutex for a single queue::pop() — far below any
 *     audio-dropout threshold.
 */

#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>


// =============================================================================
// Primitive MIDI event record
// =============================================================================

/**
 * One raw MIDI note event from an imported .mid file.
 *
 * Python converts absolute tick positions to sample-frame units using the
 * file's BPM and the engine's sample rate before populating this struct —
 * so the C++ side never needs to know about ticks or tempo.
 */
struct MidiNoteEvent {
    int64_t  abs_frame = 0;   /**< Absolute sample-frame position on the timeline. */
    uint8_t  msg_type  = 0;   /**< 0x90 = note-on, 0x80 = note-off.                */
    uint8_t  note      = 0;   /**< MIDI note number 0-127.                          */
    uint8_t  velocity  = 0;   /**< Velocity 0-127.  Note-off events carry 0.        */
    uint8_t  channel   = 0;   /**< MIDI channel 0-15.                               */
};

// =============================================================================
// Per-track import payload
// =============================================================================

/**
 * All the data needed to create one instrument track in the C++ engine.
 *
 * Python assembles this from mido-parsed data, resolves the sfz_path, and
 * passes a vector of these to ``TimelineEngine::importMultiTrackMidi()``.
 */
struct MidiTrackPayload {
    std::string               name;           /**< Track name (or "Track N").         */
    int                       track_index  = 0; /**< 0-based index in the .mid file.  */
    int                       gm_program_id= 0; /**< GM program 0-127, or 128=drums.  */
    std::string               sfz_path;       /**< Default SFZ template path.         */
    std::vector<MidiNoteEvent> events;         /**< Sorted ascending by abs_frame.    */
};

// =============================================================================
// Import result — passed from the loader thread to the audio callback
// =============================================================================

/**
 * Scalar result dropped into the lock-free notification queue.
 * The audio callback drains this to learn that new tracks are available.
 */
struct PreparedImportBatch {
    int  track_count = 0;
    bool success     = false;
};

// =============================================================================
// Forward declaration
// =============================================================================

class TimelineEngine;

// =============================================================================
// MidiDropImporter
// =============================================================================

/**
 * Manages the lifecycle of a background MIDI import operation.
 *
 * One instance lives inside ``TimelineEngine`` as a private member.
 * The engine exposes ``importMultiTrackMidi()`` / ``is_import_busy()`` /
 * ``check_import_ready()`` as thin forwarding methods to this class.
 *
 * See the file-level comment for the full data-flow and thread-safety
 * description.
 */
class MidiDropImporter {
public:
    /**
     * Construct bound to *engine*.  The engine must outlive this object.
     */
    explicit MidiDropImporter(TimelineEngine& engine);

    /**
     * Destructor.  Detaches any running import thread (safe — the thread
     * only uses the engine's public API and the atomic _busy flag; both
     * remain valid until the engine is destroyed).
     */
    ~MidiDropImporter();

    /* Non-copyable — owns a std::thread and std::atomic members. */
    MidiDropImporter(const MidiDropImporter&)            = delete;
    MidiDropImporter& operator=(const MidiDropImporter&) = delete;

    // ── Initiation  (GUI / Python thread) ────────────────────────────────────

    /**
     * Kick off an asynchronous import.  Returns immediately.
     *
     * A deep copy of *payloads* is made before this function returns, so
     * the caller's vector is safe to mutate or destroy right away.
     *
     * If an import is already in flight the call is a no-op (the caller can
     * poll is_busy() and retry, or queue requests at the Python level).
     *
     * @param payloads  Per-track data assembled by Python / mido.
     * @param on_done   Optional callback fired on the importer thread after
     *                  success or failure.  Receives true=success.
     */
    void import(
        const std::vector<MidiTrackPayload>& payloads,
        std::function<void(bool)>            on_done = nullptr
    );

    /**
     * True while the background thread is still running.
     * Lock-free: only performs an atomic load.
     */
    bool is_busy() const noexcept;

    // ── Audio-thread notification  (realtime audio callback) ─────────────────

    /**
     * Poll for import completion.  Must be called from the audio callback
     * (or from any thread that consumes the MIDI playback pipeline).
     *
     * Designed to be called once per audio block (~512 samples).
     *
     * @return true the FIRST time after a successful import has finished.
     *         The caller should use this edge to schedule playback of the
     *         newly imported tracks (e.g., reset the transport to beat 0).
     *
     * Thread-safety: only an atomic exchange + one tiny lock held for a
     * single queue::pop().  Zero heap allocations.
     */
    bool check_import_ready();

    // ── GM instrument routing  (static — no engine state required) ───────────

    /**
     * Map a General MIDI program ID (0-127) or the drum sentinel (128)
     * to the corresponding default SFZ template path.
     *
     * IDs not listed in a named group (chromatic perc, organs, ensemble,
     * reed, pipe, ethnic, sound effects) fall through to the piano catch-all.
     *
     * @param gm_id  GM program number 0-127, or 128 for drums.
     * @return       Relative path string.  Never null.
     */
    static const char* gm_to_sfz_path(int gm_id) noexcept;

private:
    // ── Background worker ─────────────────────────────────────────────────────

    /**
     * Entry point for the background std::thread.
     *
     * Executes Phase 1 (data prep), Phase 2 (track injection), and
     * Phase 3 (lock-free notification) as described in the file header.
     */
    void _run_import(
        std::vector<MidiTrackPayload> payloads,   // moved in — no copy
        std::function<void(bool)>     on_done
    );

    // ── Members ───────────────────────────────────────────────────────────────

    TimelineEngine& _engine;

    std::thread       _import_thread;
    std::atomic<bool> _busy  { false };

    // Lock-free single-producer / single-consumer notification.
    // See file-level comment for the full memory-ordering story.
    std::atomic<bool>                _ready_flag  { false };
    std::mutex                       _result_mutex;
    std::queue<PreparedImportBatch>  _result_queue;
};
