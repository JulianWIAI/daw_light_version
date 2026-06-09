/**
 * bindings_ds.cpp -- pybind11 bindings for the Decent Sampler engine.
 * ====================================================================
 * Called from bindings.cpp via:
 *   void bind_ds(py::module_& m);
 *
 * Exposed to Python (all under daw_processors.*):
 *   DsZoneData           -- per-zone metadata struct (mirrors Python DsSampleZone)
 *   DecentSamplerEngine  -- polyphonic sampler with ADSR, pitch-shift, render
 *   CppBusInfo           -- VST3 bus metadata (name, channel count, type)
 *   Vst3BusManager       -- multi-bus topology query + activation
 *
 * GIL notes:
 *   render()     -- GIL released for the full DSP block (pure C++, no Python objects).
 *   load_zones() -- GIL released; may do blocking disk I/O for WAV files.
 *   note_on/off  -- fast lock-free queue push; GIL kept (no blocking work).
 *   Vst3BusManager::load_topology() -- main thread only; GIL kept.
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>       // std::vector <-> list, std::string <-> str
#include <pybind11/numpy.h>     // py::array_t for audio buffer returns

#include "DecentSamplerEngine.h"
#include "Vst3BusManager.h"

namespace py = pybind11;

void bind_ds(py::module_& m) {

    // ── DsZoneData ────────────────────────────────────────────────────────────

    py::class_<DsZoneData>(m, "DsZoneData",
        "Per-zone sample metadata for DecentSamplerEngine.load_zones().\n\n"
        "Create one struct per DsSampleZone from dspreset_parser and pass\n"
        "the list to DecentSamplerEngine.load_zones().  All paths must be\n"
        "absolute (use DsInstrumentInfo.base_dir + zone.path).")
        .def(py::init<>())
        // File and mapping.
        .def_readwrite("path",         &DsZoneData::path,
                       "Absolute path to the WAV file.")
        .def_readwrite("root_note",    &DsZoneData::root_note,
                       "MIDI note the sample was recorded at (pitch reference).")
        .def_readwrite("lo_note",      &DsZoneData::lo_note,
                       "Lowest MIDI note this zone covers (inclusive).")
        .def_readwrite("hi_note",      &DsZoneData::hi_note,
                       "Highest MIDI note this zone covers (inclusive).")
        .def_readwrite("lo_vel",       &DsZoneData::lo_vel,
                       "Lowest velocity trigger (0-127).")
        .def_readwrite("hi_vel",       &DsZoneData::hi_vel,
                       "Highest velocity trigger (0-127).")
        // Volume / pan.
        .def_readwrite("volume_db",    &DsZoneData::volume_db,
                       "Volume trim in dB (0 = unity gain).")
        .def_readwrite("pan",          &DsZoneData::pan,
                       "Stereo pan: -100 (full left) to +100 (full right).")
        // ADSR envelope.
        .def_readwrite("attack",       &DsZoneData::attack,
                       "Attack time in seconds.")
        .def_readwrite("decay",        &DsZoneData::decay,
                       "Decay time in seconds.")
        .def_readwrite("sustain",      &DsZoneData::sustain,
                       "Sustain level 0.0 (silence) to 1.0 (full).")
        .def_readwrite("release",      &DsZoneData::release,
                       "Release time in seconds.")
        // Loop.
        .def_readwrite("loop_enabled", &DsZoneData::loop_enabled,
                       "True to loop the sample between loop_start and loop_end.")
        .def_readwrite("loop_start",   &DsZoneData::loop_start,
                       "Loop start position in sample frames.")
        .def_readwrite("loop_end",     &DsZoneData::loop_end,
                       "Loop end position in sample frames (-1 = end of file).")
        // Round-robin.
        .def_readwrite("seq_position", &DsZoneData::seq_position,
                       "1-based position in the round-robin cycle.")
        .def_readwrite("seq_length",   &DsZoneData::seq_length,
                       "Total number of round-robin alternatives in this group.")
        .def_readwrite("trigger",      &DsZoneData::trigger,
                       "Trigger type: 'attack', 'release', 'first', or 'legato'.")
        .def("__repr__", [](const DsZoneData& z) {
            return "<DsZoneData root=" + std::to_string(z.root_note)
                 + " lo=" + std::to_string(z.lo_note)
                 + " hi=" + std::to_string(z.hi_note)
                 + " path='" + z.path + "'>";
        });

    // ── DecentSamplerEngine ───────────────────────────────────────────────────

    py::class_<DecentSamplerEngine>(m, "DecentSamplerEngine",
        "Polyphonic Decent Sampler audio engine.\n\n"
        "Accepts zone metadata from dspreset_parser (via load_zones()), loads\n"
        "WAV files from disk, and renders polyphonic audio with per-voice ADSR\n"
        "envelopes and pitch-shift via playback-rate scaling.\n\n"
        "Instantiate once per DS instrument track.  Always call load_zones()\n"
        "before note_on().  render() must be called from the audio thread only.")
        .def(py::init<float, int>(),
             py::arg("sample_rate") = 44100.0f,
             py::arg("block_size")  = 512,
             "Create engine for the given sample rate and maximum block size.")

        // Setup
        .def("set_sample_rate", &DecentSamplerEngine::set_sample_rate,
             py::arg("sr"),
             "Change the playback sample rate (call before load_zones).")
        .def("set_block_size",  &DecentSamplerEngine::set_block_size,
             py::arg("block_size"),
             "Change the maximum block size.")

        // Instrument loading
        .def("load_zones",
             &DecentSamplerEngine::load_zones,
             py::arg("zones"),
             py::call_guard<py::gil_scoped_release>(),
             "Load a list of DsZoneData structs.\n"
             "Reads each WAV file from disk (GIL released).\n"
             "Returns True if at least one zone loaded successfully.")
        .def("is_loaded",   &DecentSamplerEngine::is_loaded,
             "True after a successful load_zones() call.")
        .def("zone_count",  &DecentSamplerEngine::zone_count,
             "Number of zones currently loaded.")

        // MIDI
        .def("note_on",
             &DecentSamplerEngine::note_on,
             py::arg("channel"), py::arg("note"), py::arg("velocity"),
             "Trigger a note-on.  channel is ignored (DS is single-channel).")
        .def("note_off",
             &DecentSamplerEngine::note_off,
             py::arg("channel"), py::arg("note"), py::arg("velocity"),
             "Release a held note.")
        .def("all_notes_off",
             &DecentSamplerEngine::all_notes_off,
             py::arg("channel") = 0,
             "Silence all playing voices.")

        // Parameter control
        .def("set_parameter",
             &DecentSamplerEngine::set_parameter,
             py::arg("name"), py::arg("value"),
             "Apply a named DS parameter (e.g. 'ENV_ATTACK', 'MASTER_VOLUME').")

        // Rendering — GIL released for pure DSP.
        .def("render",
             [](DecentSamplerEngine& self, int num_samples) -> py::tuple {
                 py::array_t<float> left_arr (num_samples);
                 py::array_t<float> right_arr(num_samples);
                 auto l = left_arr .mutable_unchecked<1>();
                 auto r = right_arr.mutable_unchecked<1>();
                 {
                     py::gil_scoped_release rel;
                     self.render(&l(0), &r(0), num_samples);
                 }
                 return py::make_tuple(left_arr, right_arr);
             },
             py::arg("num_samples"),
             "Render num_samples frames → (left_array, right_array) float32.\n"
             "Call from the audio thread only.  GIL released during DSP.");

    // ── CppBusInfo ────────────────────────────────────────────────────────────

    py::class_<CppBusInfo>(m, "CppBusInfo",
        "Metadata for one VST3 audio bus (name, channel count, type).")
        .def(py::init<>())
        .def_readwrite("name",          &CppBusInfo::name,
                       "Human-readable bus name from the plugin.")
        .def_readwrite("channel_count", &CppBusInfo::channel_count,
                       "Number of audio channels (1=mono, 2=stereo, 6=5.1…).")
        .def_readwrite("bus_type",      &CppBusInfo::bus_type,
                       "'main' or 'aux'.")
        .def_readwrite("default_active",&CppBusInfo::default_active,
                       "Whether this bus should be active by default.")
        .def("__repr__", [](const CppBusInfo& b) {
            return "<CppBusInfo '" + b.name
                 + "' ch=" + std::to_string(b.channel_count)
                 + " type=" + b.bus_type + ">";
        });

    // ── Vst3BusManager ────────────────────────────────────────────────────────

    py::class_<Vst3BusManager>(m, "Vst3BusManager",
        "Query and configure the bus topology of a loaded VST3 plugin.\n\n"
        "Pass a raw IComponent pointer (as an integer) from your C++ VST3 host\n"
        "to load_topology().  Then query input_bus_count() / output_bus_count()\n"
        "and activate the desired buses with set_bus_active().\n\n"
        "When built without HAVE_VST3_SDK this class compiles as a no-op stub.")
        .def(py::init<>())

        // Topology query
        .def("load_topology",
             [](Vst3BusManager& self, uint64_t ptr) {
                 return self.load_topology(static_cast<uintptr_t>(ptr));
             },
             py::arg("component_ptr"),
             "Query bus topology from an IComponent* (passed as integer).\n"
             "Returns True if at least one output bus was found.")
        .def("input_bus_count",  &Vst3BusManager::input_bus_count,
             "Number of audio input buses.")
        .def("output_bus_count", &Vst3BusManager::output_bus_count,
             "Number of audio output buses.")
        .def("get_input_buses",  &Vst3BusManager::get_input_buses,
             "Return List[CppBusInfo] for every input bus.")
        .def("get_output_buses", &Vst3BusManager::get_output_buses,
             "Return List[CppBusInfo] for every output bus.")
        .def("total_output_channels", &Vst3BusManager::total_output_channels,
             "Sum of channel counts across all active output buses.")

        // Activation
        .def("set_bus_active",
             &Vst3BusManager::set_bus_active,
             py::arg("is_input"), py::arg("index"), py::arg("active"),
             "Activate or deactivate one bus.  is_input=True for input buses.")
        .def("activate",   &Vst3BusManager::activate,
             "Activate all buses at their default state.")
        .def("deactivate", &Vst3BusManager::deactivate,
             "Deactivate all buses.");
}
