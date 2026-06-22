/*
 * MidiDropImporter.cpp
 * ====================
 * Implementation of the async multi-track MIDI import pipeline.
 *
 * See MidiDropImporter.h for the full architectural description.
 */

#include "MidiDropImporter.h"
#include "TimelineEngine.h"

#include <algorithm>   // std::sort
#include <stdexcept>
#include <utility>     // std::move


// =============================================================================
// GM Program ID → default SFZ template path
// =============================================================================

/*static*/ const char* MidiDropImporter::gm_to_sfz_path(int gm_id) noexcept {
    // Explicitly mapped ranges — ordered from least common to most common so
    // the typical Piano / Drums cases are hit with minimal branching.
    if (gm_id == 128)                      return "system/defaults/default_drums.sfz";   // drums sentinel
    else if (gm_id >= 80 && gm_id <= 95)   return "system/defaults/default_synth.sfz";   // Synth Lead + Pad
    else if (gm_id >= 56 && gm_id <= 63)   return "system/defaults/default_brass.sfz";   // Brass
    else if (gm_id >= 40 && gm_id <= 47)   return "system/defaults/default_strings.sfz"; // Strings
    else if (gm_id >= 32 && gm_id <= 39)   return "system/defaults/default_bass.sfz";    // Bass
    else if (gm_id >= 24 && gm_id <= 31)   return "system/defaults/default_guitar.sfz";  // Guitar
    else if (gm_id >= 0  && gm_id <= 7)    return "system/defaults/default_piano.sfz";   // Piano
    else {
        // DEFAULT catch-all — every unmapped GM range (8-23 Chromatic/Organ,
        // 48-55 Ensemble, 64-79 Reed/Pipe, 96-127 Ethnic/FX/Percussive) and
        // any out-of-range value (negative, >128) routes here.  Piano is the
        // guaranteed fallback; no naked track can fall through to silence.
        return "system/defaults/default_piano.sfz";
    }
}


// =============================================================================
// Construction / destruction
// =============================================================================

MidiDropImporter::MidiDropImporter(TimelineEngine& engine)
    : _engine(engine)
{}

MidiDropImporter::~MidiDropImporter() {
    // Detach rather than join so the destructor returns quickly even if the
    // import thread is still running.  The thread only accesses the engine
    // via its public API and the _busy flag — both of which survive until
    // TimelineEngine (which owns this object) is fully destroyed.
    if (_import_thread.joinable())
        _import_thread.detach();
}


// =============================================================================
// Public API
// =============================================================================

void MidiDropImporter::import(
    const std::vector<MidiTrackPayload>& payloads,
    std::function<void(bool)>            on_done
) {
    // One import at a time — callers poll is_busy() before calling again.
    if (_busy.load(std::memory_order_relaxed))
        return;

    // Eagerly copy the payload vector here on the calling (GUI/Python) thread
    // so the caller's memory is free to use immediately after this returns.
    std::vector<MidiTrackPayload> local_copy = payloads;

    // Join the previously completed thread (if any) to reclaim its stack.
    if (_import_thread.joinable())
        _import_thread.join();

    _busy.store(true, std::memory_order_release);

    _import_thread = std::thread(
        &MidiDropImporter::_run_import,
        this,
        std::move(local_copy),
        std::move(on_done)
    );
}

bool MidiDropImporter::is_busy() const noexcept {
    return _busy.load(std::memory_order_relaxed);
}

// -----------------------------------------------------------------------------
// check_import_ready()  —  called from the realtime audio callback
// -----------------------------------------------------------------------------

bool MidiDropImporter::check_import_ready() {
    // ── Step 1: atomic flag check (zero locks, zero allocations) ─────────────
    //
    // memory_order_acquire pairs with the release store at the bottom of
    // _run_import().  This establishes a happens-before edge:
    //
    //   All engine.add_instrument_track() / engine.add_midi_event() calls
    //   performed before the store are visible to the audio thread once
    //   this exchange returns true.
    //
    if (!_ready_flag.exchange(false, std::memory_order_acquire))
        return false;

    // ── Step 2: drain the result queue ───────────────────────────────────────
    //
    // The queue holds at most one element per import.  The lock is held for
    // a single queue::pop() — measured in nanoseconds, far below the
    // ~11 ms audio-block budget.
    {
        std::lock_guard<std::mutex> lk(_result_mutex);
        while (!_result_queue.empty())
            _result_queue.pop();
    }

    return true;
}


// =============================================================================
// Background import worker
// =============================================================================

void MidiDropImporter::_run_import(
    std::vector<MidiTrackPayload> payloads,   // already a local copy
    std::function<void(bool)>     on_done
) {
    bool success      = false;
    int  tracks_added = 0;

    try {
        // ── Phase 1: Data preparation  (zero locks) ───────────────────────────
        //
        // Everything in this phase is pure computation and (optional) disk I/O.
        // No engine mutex is touched here, so the audio callback runs freely.

        for (auto& p : payloads) {
            // Fill in the SFZ path if Python didn't provide one.
            if (p.sfz_path.empty())
                p.sfz_path = gm_to_sfz_path(p.gm_program_id);

            // Sort events ascending by absolute sample-frame position.
            // Python already sorts them, but we re-sort as a safety net in case
            // the caller omitted it or the order was non-deterministic.
            std::sort(
                p.events.begin(), p.events.end(),
                [](const MidiNoteEvent& a, const MidiNoteEvent& b) {
                    return a.abs_frame < b.abs_frame;
                }
            );
        }

        // ── Phase 2: Atomic track injection  (brief per-call mutex) ──────────
        //
        // add_instrument_track() and add_midi_event() each acquire
        // _tracks_mutex for an O(1) push_back / emplace_back, then release
        // it immediately.  We call them one-at-a-time so the audio callback
        // never waits more than a single vector push_back.
        //
        // The SFZ path stored in p.sfz_path is recorded on the track as
        // metadata.  Actual SFZ file loading (which involves disk I/O and
        // sfizz library initialisation) must be triggered from the Python
        // side via the existing SfizzEngine infrastructure AFTER this import
        // completes — the on_done callback is the right place to do that.

        for (const auto& p : payloads) {
            // Create one INSTRUMENT track per payload.
            int track_id = _engine.add_instrument_track();

            // Inject every note event.
            for (const auto& ev : p.events) {
                _engine.add_midi_event(
                    track_id,
                    ev.abs_frame,
                    ev.msg_type,
                    ev.channel,
                    ev.note,
                    ev.velocity
                );
            }

            // Sort the track's internal event list by abs_frame once, instead
            // of after every individual add_midi_event().
            _engine.sort_midi_events(track_id);

            ++tracks_added;
        }

        success = true;

    } catch (const std::exception& ex) {
        success = false;
        (void)ex;   // logged by caller if on_done checks the bool
    } catch (...) {
        success = false;
    }

    // ── Phase 3: Lock-free notification to the audio callback thread ──────────
    //
    // The result batch is enqueued BEFORE the release store.  This ordering
    // guarantees:
    //   a. The audio thread that calls check_import_ready() and sees the flag
    //      set will always find a result in the queue.
    //   b. All Phase-2 writes (tracks + events) are visible to the audio
    //      thread once it performs the acquire exchange.
    {
        std::lock_guard<std::mutex> lk(_result_mutex);
        _result_queue.push(PreparedImportBatch{ tracks_added, success });
    }

    // Release store: pairs with the acquire exchange in check_import_ready().
    _ready_flag.store(true, std::memory_order_release);

    // Clear the busy flag so callers can start the next import.
    _busy.store(false, std::memory_order_release);

    // Optional caller notification (may trigger Python-side SFZ loading).
    if (on_done)
        on_done(success);
}
