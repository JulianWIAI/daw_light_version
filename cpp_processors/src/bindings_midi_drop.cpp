/*
 * bindings_midi_drop.cpp  --  pybind11 bindings for the MIDI drop-import API
 * =============================================================================
 *
 * Exposes three objects to Python:
 *
 *   daw_processors.MidiNoteEvent       — per-event struct (one note-on/-off)
 *   daw_processors.MidiTrackPayload    — per-track payload (name + events)
 *   TimelineEngine.importMultiTrackMidi() — async kick-off method (added to
 *                                           the existing TimelineEngine binding)
 *
 * The two structs are fully mutable from Python so that the Python-side parser
 * (midi_drop_importer.py) can build them incrementally with attribute writes,
 * mirroring the natural "cpp_p.name = track_name" style.
 *
 * Wiring
 * ------
 * This function is declared as:
 *
 *     void bind_midi_drop(py::module_& m, py::class_<TimelineEngine>& tl_cls);
 *
 * Call it from PYBIND11_MODULE (bindings.cpp) after the TimelineEngine class
 * is defined:
 *
 *     extern void bind_midi_drop(py::module_&, py::class_<TimelineEngine>&);
 *     bind_midi_drop(m, timeline_cls);
 *
 * where `timeline_cls` is the py::class_<TimelineEngine> local variable
 * already present in bindings.cpp.
 */

#include <pybind11/pybind11.h>
#include <pybind11/functional.h>   // std::function argument support
#include <pybind11/stl.h>          // std::vector ↔ Python list auto-conversion

#include "MidiDropImporter.h"      // MidiNoteEvent, MidiTrackPayload, PreparedImportBatch
#include "TimelineEngine.h"        // TimelineEngine (must already be bound in m)

namespace py = pybind11;


void bind_midi_drop(py::module_& m, py::class_<TimelineEngine>& tl_cls)
{
    // ── MidiNoteEvent ────────────────────────────────────────────────────────
    //
    // Python builds these in midi_drop_importer._dispatch_to_bridge():
    //
    //     ev = dp.MidiNoteEvent()
    //     ev.abs_frame = int(seconds * sample_rate)
    //     ev.msg_type  = 0x90
    //     ev.note      = 60
    //     ev.velocity  = 100
    //     ev.channel   = 0

    py::class_<MidiNoteEvent>(m, "MidiNoteEvent",
        "One raw MIDI note event with a pre-converted absolute sample-frame position.")

        .def(py::init<>())

        .def_readwrite("abs_frame", &MidiNoteEvent::abs_frame,
            "Absolute sample-frame position on the timeline (int64).")
        .def_readwrite("msg_type",  &MidiNoteEvent::msg_type,
            "0x90 = note-on, 0x80 = note-off.")
        .def_readwrite("note",      &MidiNoteEvent::note,
            "MIDI note number 0-127.")
        .def_readwrite("velocity",  &MidiNoteEvent::velocity,
            "Velocity 0-127.  Note-off events carry 0.")
        .def_readwrite("channel",   &MidiNoteEvent::channel,
            "MIDI channel 0-15.")

        .def("__repr__", [](const MidiNoteEvent& ev) {
            return "<MidiNoteEvent frame=" + std::to_string(ev.abs_frame)
                 + " type=0x" + (ev.msg_type == 0x90 ? "90" : "80")
                 + " note="   + std::to_string(ev.note)
                 + " vel="    + std::to_string(ev.velocity)
                 + " ch="     + std::to_string(ev.channel) + ">";
        });


    // ── MidiTrackPayload ─────────────────────────────────────────────────────
    //
    // Python builds these in midi_drop_importer._dispatch_to_bridge():
    //
    //     p = dp.MidiTrackPayload()
    //     p.name          = "Piano"
    //     p.track_index   = 0
    //     p.gm_program_id = 0          # Acoustic Grand Piano
    //     p.sfz_path      = "system/defaults/default_piano.sfz"
    //     p.events        = [ev1, ev2, ...]   # list of MidiNoteEvent

    py::class_<MidiTrackPayload>(m, "MidiTrackPayload",
        "All data needed to create one instrument track in the C++ engine.")

        .def(py::init<>())

        .def_readwrite("name",          &MidiTrackPayload::name,
            "Track name from the .mid file, or 'Track N'.")
        .def_readwrite("track_index",   &MidiTrackPayload::track_index,
            "0-based track index inside the .mid file.")
        .def_readwrite("gm_program_id", &MidiTrackPayload::gm_program_id,
            "GM program number 0-127, or 128 for drums/Channel-10.")
        .def_readwrite("sfz_path",      &MidiTrackPayload::sfz_path,
            "Relative path to the default SFZ template for this GM group.")
        .def_readwrite("events",        &MidiTrackPayload::events,
            "List of MidiNoteEvent objects, sorted by abs_frame.")

        .def("__repr__", [](const MidiTrackPayload& p) {
            return "<MidiTrackPayload name='" + p.name
                 + "' gm=" + std::to_string(p.gm_program_id)
                 + " events=" + std::to_string(p.events.size()) + ">";
        });


    // ── TimelineEngine additions ─────────────────────────────────────────────
    //
    // These three methods are defined in TimelineEngine.cpp and documented in
    // TimelineEngine.h.  They are appended to the existing py::class_ object
    // that bindings.cpp already created — pybind11 supports incremental
    // .def() additions across multiple .cpp files via the class handle.

    tl_cls

        .def("importMultiTrackMidi",
             &TimelineEngine::importMultiTrackMidi,
             py::arg("payloads"),
             py::arg("on_done") = py::none(),
             py::call_guard<py::gil_scoped_release>(),
             R"doc(
importMultiTrackMidi(payloads, on_done=None)

Asynchronously import a list of parsed MIDI tracks.

Returns immediately.  The C++ background thread copies the payload vector
before this call returns so the Python list may be garbage-collected safely.

Args:
    payloads: list[MidiTrackPayload]  — Per-track data from the Python parser.
    on_done:  Optional[Callable[[bool], None]]  — Called on the C++ thread
              when import completes.  Use a Qt signal or
              QMetaObject.invokeMethod to update the GUI from this callback.

Thread-safety:
    May be called from any thread.  The actual heavy lifting runs on a
    private background thread; see MidiDropImporter.h for details.
)doc")

        .def("is_import_busy",
             &TimelineEngine::is_import_busy,
             R"doc(
is_import_busy() -> bool

True while the background MIDI import thread is still running.
Lock-free: only performs an atomic load.
)doc")

        .def("check_import_ready",
             &TimelineEngine::check_import_ready,
             py::call_guard<py::gil_scoped_release>(),
             R"doc(
check_import_ready() -> bool

Poll for import completion from the audio callback or a Python timer.

Returns True the first time after a successful import finishes, then
resets to False until the next import completes.

This is designed to be called once per audio block (~512 samples) from
the sounddevice callback.  It performs zero heap allocations.
)doc");


    // ── Static GM routing helper (convenience for Python) ────────────────────
    //
    // Exposes gm_to_sfz_path() so Python tests can verify the routing table
    // without constructing a full MidiDropImporter.

    m.def("gm_to_sfz_path",
          &MidiDropImporter::gm_to_sfz_path,
          py::arg("gm_id"),
          R"doc(
gm_to_sfz_path(gm_id: int) -> str

Map a General MIDI program ID (0-127) or drum sentinel (128) to the
default SFZ template path for that instrument group.

Groups:
    0-7   → system/defaults/default_piano.sfz
    24-31 → system/defaults/default_guitar.sfz
    32-39 → system/defaults/default_bass.sfz
    40-47 → system/defaults/default_strings.sfz
    56-63 → system/defaults/default_brass.sfz
    80-95 → system/defaults/default_synth.sfz
    128   → system/defaults/default_drums.sfz
    (all others) → system/defaults/default_piano.sfz
)doc");
}
