/**
 * bindings_telemetry.cpp
 *
 * pybind11 bindings for TelemetryAnalyzer and TelemetryFrame.
 * Called from the main PYBIND11_MODULE entry point in bindings.cpp.
 *
 * Exposed to Python as daw_processors.TelemetryFrame and
 * daw_processors.TelemetryAnalyzer.
 *
 * All array fields (bands, chroma, waveform) are returned as zero-copy
 * numpy arrays backed by the TelemetryFrame copy held by Python.
 * push() releases the GIL so the audio thread is not blocked during binding.
 * get_frame() also releases the GIL so Python can call it from a timer.
 */

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include "TelemetryAnalyzer.h"

namespace py = pybind11;

void bind_telemetry(py::module_& m) {

    // ── TelemetryFrame ───────────────────────────────────────────────────────
    py::class_<TelemetryFrame>(m, "TelemetryFrame")
        .def(py::init<>())
        .def_readonly("rms",       &TelemetryFrame::rms)
        .def_readonly("harmonic",  &TelemetryFrame::harmonic)
        .def_readonly("percussive",&TelemetryFrame::percussive)
        .def_readonly("tick",      &TelemetryFrame::tick)
        // Fixed-size C arrays exposed as 1-D numpy arrays.
        // py::array_t copies the data from the frame struct, so Python owns
        // the buffer independently and the frame can be overwritten safely.
        .def_property_readonly("bands", [](const TelemetryFrame& f) {
            return py::array_t<float>(
                {7},                       // shape
                {sizeof(float)},           // strides
                f.bands                    // data pointer (copied)
            );
        })
        .def_property_readonly("chroma", [](const TelemetryFrame& f) {
            return py::array_t<float>(
                {12},
                {sizeof(float)},
                f.chroma
            );
        })
        .def_property_readonly("waveform", [](const TelemetryFrame& f) {
            return py::array_t<float>(
                {TELEMETRY_WAVE_POINTS},
                {sizeof(float)},
                f.waveform
            );
        });

    // ── TelemetryAnalyzer ────────────────────────────────────────────────────
    py::class_<TelemetryAnalyzer>(m, "TelemetryAnalyzer")
        .def(py::init<int>(), py::arg("sample_rate") = 44100,
             "Create a telemetry analyzer.  Call start() before pushing audio.")
        .def("start", &TelemetryAnalyzer::start,
             "Start the background DSP thread.")
        .def("stop",  &TelemetryAnalyzer::stop,
             "Stop the background DSP thread (blocks until exit).")
        // push: releases GIL so the audio callback thread can continue while
        // pybind11 processes the numpy array argument.
        .def("push",
            [](TelemetryAnalyzer& self, py::array_t<float, py::array::c_style> arr) {
                auto  buf = arr.request();
                const float* ptr = static_cast<const float*>(buf.ptr);
                const int    n   = static_cast<int>(buf.size);
                py::gil_scoped_release no_gil;
                self.push(ptr, n);
            },
            py::arg("mono_samples"),
            "Push a mono float32 numpy array.  Non-blocking, safe from any thread.")
        // get_frame: releases GIL so the 30-FPS polling timer is not held up.
        .def("get_frame",
            [](const TelemetryAnalyzer& self) {
                py::gil_scoped_release no_gil;
                return self.get_frame();
            },
            "Return a copy of the latest TelemetryFrame (always instant).");
}
