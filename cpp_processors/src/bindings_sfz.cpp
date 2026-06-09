/**
 * bindings_sfz.cpp -- pybind11 bindings for SFZ metadata + SfizzEngine.
 * =======================================================================
 * Called from the main PYBIND11_MODULE block in bindings.cpp via:
 *   void bind_sfz(py::module_& m);
 *
 * Exposed to Python:
 *   daw_processors.SfzKeyRange      -- lo/hi MIDI key range struct
 *   daw_processors.SfzVelRange      -- lo/hi velocity range struct
 *   daw_processors.SfzRegionInfo    -- one SFZ region (sample, ranges, etc.)
 *   daw_processors.SfzGroupInfo     -- group with list of regions
 *   daw_processors.SfzInstrumentInfo-- full instrument metadata
 *   daw_processors.SfzParser        -- static parse(path) → SfzInstrumentInfo
 *   daw_processors.SfizzEngine      -- real-time SFZ playback engine
 *
 * GIL handling:
 *   render() releases the GIL (pure C++/sfizz audio, no Python objects touched).
 *   load_sfz() is blocking I/O — GIL released so the GUI stays responsive.
 *   MIDI event calls (note_on etc.) are fast non-blocking queues; GIL kept.
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>       // std::vector ↔ list, std::pair ↔ tuple
#include <pybind11/numpy.h>     // py::array_t for bulk buffer access

#include "SfzParser.h"
#include "SfizzEngine.h"

namespace py = pybind11;

void bind_sfz(py::module_& m) {

    // ── SfzKeyRange ───────────────────────────────────────────────────────────

    py::class_<SfzKeyRange>(m, "SfzKeyRange",
        "MIDI key range for one SFZ region (lo/hi note numbers 0-127).")
        .def(py::init<>())
        .def_readwrite("lo", &SfzKeyRange::lo, "Lowest MIDI note (inclusive).")
        .def_readwrite("hi", &SfzKeyRange::hi, "Highest MIDI note (inclusive).")
        .def("__repr__", [](const SfzKeyRange& r) {
            return "<SfzKeyRange lo=" + std::to_string(r.lo)
                 + " hi=" + std::to_string(r.hi) + ">";
        });

    // ── SfzVelRange ───────────────────────────────────────────────────────────

    py::class_<SfzVelRange>(m, "SfzVelRange",
        "MIDI velocity range for one SFZ region (0-127).")
        .def(py::init<>())
        .def_readwrite("lo", &SfzVelRange::lo)
        .def_readwrite("hi", &SfzVelRange::hi)
        .def("__repr__", [](const SfzVelRange& r) {
            return "<SfzVelRange lo=" + std::to_string(r.lo)
                 + " hi=" + std::to_string(r.hi) + ">";
        });

    // ── SfzRegionInfo ─────────────────────────────────────────────────────────

    py::class_<SfzRegionInfo>(m, "SfzRegionInfo",
        "Metadata for one <region> in an SFZ instrument.")
        .def(py::init<>())
        .def_readwrite("key_range",    &SfzRegionInfo::key_range,
                       "MIDI key range this region responds to.")
        .def_readwrite("vel_range",    &SfzRegionInfo::vel_range,
                       "MIDI velocity range this region responds to.")
        .def_readwrite("sample",       &SfzRegionInfo::sample,
                       "Relative path of the audio sample file (as in the SFZ).")
        .def_readwrite("volume",       &SfzRegionInfo::volume,
                       "Region volume in dB (from the volume opcode).")
        .def_readwrite("pan",          &SfzRegionInfo::pan,
                       "Region pan position -100 (left) to +100 (right).")
        .def_readwrite("seq_length",   &SfzRegionInfo::seq_length,
                       "Round-robin cycle length (seq_length opcode).")
        .def_readwrite("seq_position", &SfzRegionInfo::seq_position,
                       "Position within the round-robin cycle (1-based).")
        .def_readwrite("group",        &SfzRegionInfo::group,
                       "Group index (from the group opcode, 0 = ungrouped).");

    // ── SfzGroupInfo ──────────────────────────────────────────────────────────

    py::class_<SfzGroupInfo>(m, "SfzGroupInfo",
        "One <group> block with its child regions and default opcodes.")
        .def(py::init<>())
        .def_readwrite("key_range", &SfzGroupInfo::key_range,
                       "Default key range for child regions.")
        .def_readwrite("vel_range", &SfzGroupInfo::vel_range,
                       "Default velocity range for child regions.")
        .def_readwrite("volume",    &SfzGroupInfo::volume)
        .def_readwrite("pan",       &SfzGroupInfo::pan)
        .def_readwrite("regions",   &SfzGroupInfo::regions,
                       "List[SfzRegionInfo] — child regions of this group.");

    // ── SfzInstrumentInfo ─────────────────────────────────────────────────────

    py::class_<SfzInstrumentInfo>(m, "SfzInstrumentInfo",
        "Full instrument metadata parsed from an .sfz file.\n\n"
        "Contains both a grouped view (groups) and a flat view (regions)\n"
        "for easy GUI rendering.")
        .def(py::init<>())
        .def_readwrite("name",        &SfzInstrumentInfo::name,
                       "Instrument name (file stem of the .sfz).")
        .def_readwrite("path",        &SfzInstrumentInfo::path,
                       "Absolute path to the .sfz file.")
        .def_readwrite("num_regions", &SfzInstrumentInfo::num_regions,
                       "Total number of regions across all groups.")
        .def_readwrite("num_groups",  &SfzInstrumentInfo::num_groups,
                       "Number of top-level <group> blocks.")
        .def_readwrite("groups",      &SfzInstrumentInfo::groups,
                       "List[SfzGroupInfo] — hierarchical view.")
        .def_readwrite("regions",     &SfzInstrumentInfo::regions,
                       "List[SfzRegionInfo] — flat view of all regions.")
        .def_readwrite("cc_labels",   &SfzInstrumentInfo::cc_labels,
                       "List[Tuple[int, str]] — (CC number, label) pairs.");

    // ── SfzParser ─────────────────────────────────────────────────────────────

    py::class_<SfzParser>(m, "SfzParser",
        "Lightweight SFZ v1/v2 text parser (GUI metadata only, no audio).")
        .def_static("parse", &SfzParser::parse,
                    py::arg("sfz_path"),
                    py::call_guard<py::gil_scoped_release>(),
                    "Parse an .sfz file and return an SfzInstrumentInfo.\n"
                    "The GIL is released for the file-IO duration.\n"
                    "Returns an empty SfzInstrumentInfo on failure.");

    // ── SfizzEngine ───────────────────────────────────────────────────────────

    py::class_<SfizzEngine>(m, "SfizzEngine",
        "Real-time SFZ polyphonic instrument engine (sfizz wrapper).\n\n"
        "Instantiate once per track.  Call load_sfz() before note_on().\n"
        "render() must be called exclusively from the audio thread.")
        .def(py::init<float, int>(),
             py::arg("sample_rate") = 44100.0f,
             py::arg("block_size")  = 512,
             "Create an SfizzEngine for the given sample rate and block size.")

        // Setup
        .def("set_sample_rate", &SfizzEngine::set_sample_rate, py::arg("sr"),
             "Set the playback sample rate.  Resets sfizz state.")
        .def("set_block_size",  &SfizzEngine::set_block_size,  py::arg("block_size"),
             "Set the maximum block size for sfizz pre-allocation.")

        // Instrument loading
        .def("load_sfz", &SfizzEngine::load_sfz,
             py::arg("path"),
             py::call_guard<py::gil_scoped_release>(),  // file I/O: release GIL
             "Load an SFZ file from disk.  Returns True on success.")
        .def("is_loaded",     &SfizzEngine::is_loaded,
             "True after a successful load_sfz() call.")
        .def("get_metadata",  &SfizzEngine::get_metadata,
             "Return SfzInstrumentInfo for the loaded instrument.")

        // MIDI
        .def("note_on",  &SfizzEngine::note_on,
             py::arg("delay"), py::arg("note"), py::arg("velocity"),
             py::arg("channel") = 0,
             "Queue a note-on event at the given sample delay within the block.")
        .def("note_off", &SfizzEngine::note_off,
             py::arg("delay"), py::arg("note"), py::arg("velocity"),
             py::arg("channel") = 0)
        .def("control_change", &SfizzEngine::control_change,
             py::arg("delay"), py::arg("cc"), py::arg("cc_value"),
             py::arg("channel") = 0,
             "MIDI CC: cc_value is 0-127.")
        .def("pitch_wheel", &SfizzEngine::pitch_wheel,
             py::arg("delay"), py::arg("pitch"), py::arg("channel") = 0,
             "Pitch wheel: pitch is -8192 (full down) to +8191 (full up).")
        .def("aftertouch", &SfizzEngine::aftertouch,
             py::arg("delay"), py::arg("pressure"), py::arg("channel") = 0,
             "Channel aftertouch: pressure 0-127.")
        .def("all_notes_off", &SfizzEngine::all_notes_off,
             py::arg("delay") = 0,
             "Silence all playing voices.")

        // Rendering — GIL released for pure-audio DSP.
        .def("render",
             [](SfizzEngine& self, int num_samples) -> py::tuple {
                 py::array_t<float> left_arr (num_samples);
                 py::array_t<float> right_arr(num_samples);
                 auto left_buf  = left_arr.mutable_unchecked<1>();
                 auto right_buf = right_arr.mutable_unchecked<1>();
                 {
                     py::gil_scoped_release rel;
                     self.render(&left_buf(0), &right_buf(0), num_samples);
                 }
                 return py::make_tuple(left_arr, right_arr);
             },
             py::arg("num_samples"),
             "Render num_samples frames and return (left_array, right_array).\n"
             "Call from the audio thread only.  GIL is released during DSP.");
}
