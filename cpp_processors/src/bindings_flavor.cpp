/*
 * bindings_flavor.cpp  --  pybind11 bindings for MasteringFlavorProcessor
 * =========================================================================
 *
 * Exposes two objects to Python:
 *
 *   daw_processors.MasteringFlavor          — int enum (0/1/2)
 *   daw_processors.MasteringFlavorProcessor — offline DSP processor
 *
 * Python usage (in mastering_export_worker.py)::
 *
 *     proc = dp.MasteringFlavorProcessor(44100.0)
 *     proc.set_flavor(1)                       # ANALOG_WARMTH
 *     l_out, r_out = proc.process_block(l_in, r_in)
 *
 * Wiring
 * ------
 * Declared as:
 *
 *     void bind_flavor(py::module_& m);
 *
 * Called from PYBIND11_MODULE (bindings.cpp) after all other bindings.
 */

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <stdexcept>
#include <cstring>

#include "MasteringFlavorProcessor.h"

namespace py = pybind11;


void bind_flavor(py::module_& m)
{
    // ── Enum ──────────────────────────────────────────────────────────────────
    py::enum_<MasteringFlavor>(m, "MasteringFlavor",
        "Mastering color applied once to the raw mix before all export targets.")
        .value("TRANSPARENT",   MasteringFlavor::TRANSPARENT,
               "Pass-through — no DSP applied.")
        .value("ANALOG_WARMTH", MasteringFlavor::ANALOG_WARMTH,
               "Padé tanh soft-saturation + high-shelf EQ (−1 dB @ 12 kHz).")
        .value("CLUB_FESTIVAL", MasteringFlavor::CLUB_FESTIVAL,
               "Low-shelf boost (+2 dB @ 50 Hz) + VCA compressor.")
        .export_values();


    // ── Class ─────────────────────────────────────────────────────────────────
    py::class_<MasteringFlavorProcessor>(m, "MasteringFlavorProcessor",
        "Offline mastering color processor.\n\n"
        "Construct once per export run, call set_flavor(), then process_block() "
        "in BLOCK_SIZE chunks.  Call reset() between targets if the same instance "
        "is reused.")

        .def(py::init<float>(),
             py::arg("sample_rate") = 44100.0f,
             "Construct with the project sample rate in Hz.")

        .def("set_flavor",
             &MasteringFlavorProcessor::set_flavor,
             py::arg("flavor"),
             "Select flavor: 0=TRANSPARENT, 1=ANALOG_WARMTH, 2=CLUB_FESTIVAL.\n"
             "Out-of-range values silently fall back to TRANSPARENT.")

        .def("reset",
             &MasteringFlavorProcessor::reset,
             "Reset all filter and compressor state to zero.")

        .def("process_block",
             [](MasteringFlavorProcessor& self,
                py::array_t<float, py::array::c_style> left,
                py::array_t<float, py::array::c_style> right)
             -> std::pair<py::array_t<float>, py::array_t<float>>
             {
                 const py::buffer_info bl = left.request();
                 const py::buffer_info br = right.request();

                 if (bl.ndim != 1 || br.ndim != 1)
                     throw std::invalid_argument(
                         "MasteringFlavorProcessor.process_block: "
                         "both arrays must be 1-D float32.");

                 const int n = static_cast<int>(bl.shape[0]);
                 if (n != static_cast<int>(br.shape[0]))
                     throw std::invalid_argument(
                         "MasteringFlavorProcessor.process_block: "
                         "left and right arrays must have equal length.");

                 // Copy input into fresh output arrays so the input is unmodified.
                 auto out_l = py::array_t<float>(n);
                 auto out_r = py::array_t<float>(n);
                 std::memcpy(out_l.mutable_data(), bl.ptr, static_cast<size_t>(n) * sizeof(float));
                 std::memcpy(out_r.mutable_data(), br.ptr, static_cast<size_t>(n) * sizeof(float));

                 {
                     py::gil_scoped_release release;
                     self.process(out_l.mutable_data(), out_r.mutable_data(), n);
                 }

                 return { std::move(out_l), std::move(out_r) };
             },
             py::arg("left"), py::arg("right"),
             R"doc(
process_block(left, right) -> (out_left, out_right)

Apply the current mastering flavor to a block of stereo audio.

Args:
    left  : 1-D contiguous float32 numpy array (left channel samples).
    right : 1-D contiguous float32 numpy array (right channel, same length).

Returns:
    Tuple (out_left, out_right) — two new float32 arrays.  The input
    arrays are not modified.

The GIL is released during C++ processing so other Python threads
remain unblocked.
)doc");
}
