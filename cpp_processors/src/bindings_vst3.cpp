/**
 * bindings_vst3.cpp -- pybind11 bindings for VST3 hosting extensions.
 * ====================================================================
 * Compiled only when HAVE_VST3_SDK is defined (CMakeLists.txt sets this
 * when VST3_SDK_ROOT points to a valid SDK directory).
 *
 * Called from bindings.cpp via:
 *   void bind_vst3_extensions(py::module_& m);
 *
 * Exposed to Python:
 *   daw_processors.Vst3PluginState       -- processor + controller byte blobs
 *   daw_processors.Vst3StateManager      -- static save() / restore() / serialize()
 *   daw_processors.Vst3AutomationQueue   -- add_point() / clear() / to_process_data()
 *   daw_processors.Vst3TransportContext  -- tempo / time-sig / advance()
 *
 * Python usage example (state):
 *   state  = dp.Vst3StateManager.save(component_ptr, controller_ptr)
 *   bytes_ = dp.Vst3StateManager.serialize(state)       # store in project
 *   state2 = dp.Vst3StateManager.deserialize(bytes_)    # restore from project
 *   dp.Vst3StateManager.restore(component_ptr, controller_ptr, state2)
 *
 * Python usage example (automation):
 *   queue = dp.Vst3AutomationQueue()
 *   queue.add_point(param_id=0x10001, sample_offset=0,   value=0.0)
 *   queue.add_point(param_id=0x10001, sample_offset=256, value=1.0)
 *   # pass queue into your C++ process() call each block, then:
 *   queue.clear()
 *
 * Python usage example (transport):
 *   t = dp.Vst3TransportContext()
 *   t.set_sample_rate(44100.0)
 *   t.set_tempo(128.0)
 *   t.set_time_signature(4, 4)
 *   t.set_playing(True)
 *   # Per block:
 *   t.advance(512)
 *   print(t.beat_position)   # current beat in quarter notes
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>     // vector<uint8_t> ↔ bytes

namespace py = pybind11;

#ifdef HAVE_VST3_SDK

#include "Vst3StateManager.h"
#include "Vst3AutomationQueue.h"
#include "Vst3TransportContext.h"

void bind_vst3_extensions(py::module_& m) {

    // ── Vst3PluginState ───────────────────────────────────────────────────────

    py::class_<Vst3PluginState>(m, "Vst3PluginState",
        "Complete VST3 plugin state snapshot: processor bytes + controller bytes.")
        .def(py::init<>())
        .def_readwrite("processor",   &Vst3PluginState::processor,
                       "bytes — IComponent::getState() output.")
        .def_readwrite("controller",  &Vst3PluginState::controller,
                       "bytes — IEditController::getState() output.")
        .def("is_empty", &Vst3PluginState::is_empty,
             "True if both processor and controller buffers are empty.");

    // ── Vst3StateManager ──────────────────────────────────────────────────────
    // Methods take raw integer pointers from Python (IComponent*, IEditController*).
    // The Python side obtains these from its existing VST3 host infrastructure.

    py::class_<Vst3StateManager>(m, "Vst3StateManager",
        "Static helpers for saving and restoring VST3 plugin state.\n\n"
        "Pass raw C++ interface pointers obtained from your VST3 host.")
        .def_static("serialize",
                    &Vst3StateManager::serialize,
                    py::arg("state"),
                    "Pack a Vst3PluginState into a flat bytes object for project storage.")
        .def_static("deserialize",
                    &Vst3StateManager::deserialize,
                    py::arg("data"),
                    "Unpack bytes (from serialize()) back into a Vst3PluginState.");
        // Note: save() and restore() take COM interface pointers that cannot be
        // directly constructed from Python — call them from C++ host glue code
        // and exchange only the Vst3PluginState / bytes with Python.

    // ── Vst3AutomationQueue ───────────────────────────────────────────────────

    py::class_<Vst3AutomationQueue>(m, "Vst3AutomationQueue",
        "Sample-accurate VST3 parameter automation queue.\n\n"
        "Implements IParameterChanges / IParamValueQueue for one audio block.\n"
        "Pass its pointer to ProcessData::inputParameterChanges.\n\n"
        "Lifecycle per block:\n"
        "  queue.clear()\n"
        "  queue.add_point(param_id, sample_offset, value)  # 0..N times\n"
        "  process_data.inputParameterChanges = queue.get_ptr()\n"
        "  plugin.process(process_data)\n")
        .def(py::init<>())
        .def("add_point",
             [](Vst3AutomationQueue& self,
                uint32_t param_id, int32_t sample_offset, double value) {
                 self.add_point(
                     static_cast<Steinberg::Vst::ParamID>(param_id),
                     static_cast<Steinberg::int32>(sample_offset),
                     static_cast<Steinberg::Vst::ParamValue>(value));
             },
             py::arg("param_id"),
             py::arg("sample_offset"),
             py::arg("value"),
             "Add a breakpoint for param_id at sample_offset.\n"
             "value is normalized [0.0, 1.0] as required by VST3.")
        .def("clear", &Vst3AutomationQueue::clear,
             "Remove all breakpoints.  Call at the start of every audio block.")
        .def("parameter_count", &Vst3AutomationQueue::getParameterCount,
             "Number of parameters with at least one breakpoint this block.");

    // ── Vst3TransportContext ──────────────────────────────────────────────────

    py::class_<Vst3TransportContext>(m, "Vst3TransportContext",
        "Manages the VST3 ProcessContext for transport and tempo sync.\n\n"
        "Update state before each audio block, then pass get_context_ptr()\n"
        "to ProcessData::processContext.")
        .def(py::init<>())

        // Configuration
        .def("set_sample_rate", &Vst3TransportContext::set_sample_rate,
             py::arg("sr"), "Set the session sample rate.")
        .def("set_tempo", &Vst3TransportContext::set_tempo,
             py::arg("bpm"), "Set the project tempo in BPM.")
        .def("set_time_signature", &Vst3TransportContext::set_time_signature,
             py::arg("numerator"), py::arg("denominator"),
             "Set the time signature (e.g. 4, 4 or 3, 4).")

        // Transport state
        .def("set_playing",   &Vst3TransportContext::set_playing,   py::arg("playing"))
        .def("set_cycling",   &Vst3TransportContext::set_cycling,   py::arg("cycling"),
             "Enable/disable loop mode.")
        .def("set_recording", &Vst3TransportContext::set_recording, py::arg("recording"))
        .def("set_cycle_range", &Vst3TransportContext::set_cycle_range,
             py::arg("start_beats"), py::arg("end_beats"),
             "Set the loop range in quarter-note beats.")

        // Position
        .def("advance",
             [](Vst3TransportContext& self, int num_samples) {
                 self.advance(static_cast<Steinberg::int32>(num_samples));
             },
             py::arg("num_samples"),
             "Advance the internal sample counter by num_samples.  "
             "Call once per block after passing the context to the plugin.")
        .def("set_sample_position",
             [](Vst3TransportContext& self, int64_t pos) {
                 self.set_sample_position(static_cast<Steinberg::int64>(pos));
             },
             py::arg("sample_pos"), "Jump to an absolute sample position.")
        .def("reset", &Vst3TransportContext::reset,
             "Reset to sample 0 / beat 0.")

        // Read-only properties
        .def_property_readonly("tempo",
             &Vst3TransportContext::get_tempo,
             "Current tempo in BPM.")
        .def_property_readonly("sample_position",
             [](const Vst3TransportContext& self) {
                 return static_cast<int64_t>(self.get_sample_position());
             },
             "Current project position in samples.")
        .def_property_readonly("beat_position",
             &Vst3TransportContext::get_beat_position,
             "Current project position in quarter-note beats.");
}

#else // HAVE_VST3_SDK not defined

// Provide stub so bindings.cpp can always call bind_vst3_extensions().
void bind_vst3_extensions(py::module_& m) {
    // VST3 SDK not configured at build time — export only an informational constant.
    m.attr("VST3_EXTENSIONS_AVAILABLE") = false;
}

#endif // HAVE_VST3_SDK
