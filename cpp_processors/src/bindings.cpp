#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include <stdexcept>
#include <utility>
#include <cstring>

#include "BrickwallLimiter.h"
#include "MultibandCompressor.h"
#include "DynamicEQ.h"
#include "DeEsser.h"
#include "TransientShaper.h"
#include "GateExpander.h"

// Spatial / time-based effects
#include "DelayEcho.h"
#include "Flanger.h"
#include "Phaser.h"
#include "StereoImager.h"

// Harmonic & character processors
#include "Saturation.h"
#include "Overdrive.h"
#include "Bitcrusher.h"
#include "Exciter.h"

// Advanced utilities & specialty filters
#include "PitchCorrector.h"
#include "PitchShifter.h"
#include "AutoFilter.h"

// Sampler instrument
#include "Sampler.h"

// Offline export mix bus + WAV writer
#include "OfflineExporter.h"

// Real-time C++ timeline / transport engine
#include "TimelineEngine.h"

// Waveform peak generator for the arrange-view audio-clip display
#include "WaveformGenerator.h"

// Gaussian velocity humanizer (GaussianRng + TimingWeightFunction)
#include "GaussianRng.h"
#include "TimingWeightFunction.h"
#include "VelocityHumanizer.h"

// Real-time loudness automation (RmsAnalyzer + EnvelopeFollower + PID + gain interpolation)
#include "LoudnessAutomation.h"

// Spectral panning & frequency masking resolution (FFT analyser + masking manager)
#include "SpectralAnalyzer.h"
#include "SpectralMaskingManager.h"
#include "SpectralPanningProcessor.h"

// Grid snap system (GridDefinition + QuantizeEngine + TimelineRuler + GridSnapper)
#include "GridDefinition.h"
#include "QuantizeEngine.h"
#include "TimelineRuler.h"
#include "GridSnapper.h"

// Precision audio-loop boundary scheduler
#include "AudioLoopScheduler.h"

// Offline mastering: automation curve interpolator + stereo mix bus
#include "AutomationProcessor.h"
#include "FullProjectRenderer.h"

// Audition mode enum + per-mode loudness processor
#include "AuditionProcessor.h"
// Real-time master bus: track summing, audition routing, gain, peak metering
#include "MasterBus.h"

namespace py = pybind11;

// Forward declarations for the split binding modules.
void bind_sfz            (py::module_& m);
void bind_vst3_extensions(py::module_& m);
void bind_ds             (py::module_& m);

// ─────────────────────────────────────────────────────────────────────────────
// Generic process_block wrapper
// Returns a pair of new numpy arrays containing the processed audio so the
// caller does not need to pass mutable numpy arrays.
// ─────────────────────────────────────────────────────────────────────────────

template<typename Processor>
std::pair<py::array_t<float>, py::array_t<float>>
process_block_impl(Processor& proc,
                   py::array_t<float> left_in,
                   py::array_t<float> right_in)
{
    // Validate inputs.
    if (left_in.ndim() != 1 || right_in.ndim() != 1) {
        throw std::invalid_argument("Both channel arrays must be 1-D.");
    }
    const py::ssize_t n = left_in.shape(0);
    if (right_in.shape(0) != n) {
        throw std::invalid_argument("Left and right channel arrays must have the same length.");
    }
    if (n > MAX_BLOCK_SIZE) {
        throw std::invalid_argument("Block size exceeds MAX_BLOCK_SIZE (4096).");
    }

    // Allocate output arrays (new memory — caller owns these).
    py::array_t<float> out_l(n);
    py::array_t<float> out_r(n);

    // Get raw pointers to the output buffers and copy input data into them.
    // Using mutable_data() on the array_t directly gives a typed float* pointer
    // that is valid as long as out_l / out_r are in scope.
    float* ptr_l = out_l.mutable_data();
    float* ptr_r = out_r.mutable_data();

    const float* src_l = left_in.data();
    const float* src_r = right_in.data();

    for (py::ssize_t i = 0; i < n; ++i) {
        ptr_l[i] = src_l[i];
        ptr_r[i] = src_r[i];
    }

    proc.process(ptr_l, ptr_r, static_cast<int>(n));

    return {out_l, out_r};
}

// ─────────────────────────────────────────────────────────────────────────────
// Module definition
// ─────────────────────────────────────────────────────────────────────────────

PYBIND11_MODULE(daw_processors, m) {
    m.doc() = "Real-time C++ dynamics and spatial processors for a Python DAW (pybind11 bindings).";

    // ── BandConfig struct ─────────────────────────────────────────────────────
    py::class_<BandConfig>(m, "BandConfig")
        .def(py::init<>())
        .def_readwrite("threshold_db",  &BandConfig::threshold_db)
        .def_readwrite("ratio",         &BandConfig::ratio)
        .def_readwrite("attack_ms",     &BandConfig::attack_ms)
        .def_readwrite("release_ms",    &BandConfig::release_ms)
        .def_readwrite("makeup_db",     &BandConfig::makeup_db)
        .def_readwrite("muted",         &BandConfig::muted)
        .def_readwrite("soloed",        &BandConfig::soloed)
        .def("__repr__", [](const BandConfig& b) {
            return "<BandConfig thr=" + std::to_string(b.threshold_db)
                 + " ratio=" + std::to_string(b.ratio) + ">";
        });

    // ── DynEQBand struct ──────────────────────────────────────────────────────
    py::class_<DynEQBand>(m, "DynEQBand")
        .def(py::init<>())
        .def_readwrite("freq_hz",        &DynEQBand::freq_hz)
        .def_readwrite("q",              &DynEQBand::q)
        .def_readwrite("static_gain_db", &DynEQBand::static_gain_db)
        .def_readwrite("threshold_db",   &DynEQBand::threshold_db)
        .def_readwrite("ratio",          &DynEQBand::ratio)
        .def_readwrite("attack_ms",      &DynEQBand::attack_ms)
        .def_readwrite("release_ms",     &DynEQBand::release_ms)
        .def_readwrite("enabled",        &DynEQBand::enabled)
        .def("__repr__", [](const DynEQBand& b) {
            return "<DynEQBand freq=" + std::to_string(b.freq_hz)
                 + " static_gain=" + std::to_string(b.static_gain_db) + "dB>";
        });

    // ── GateState enum ────────────────────────────────────────────────────────
    py::enum_<GateExpander::GateState>(m, "GateState")
        .value("CLOSED",  GateExpander::GateState::CLOSED)
        .value("OPENING", GateExpander::GateState::OPENING)
        .value("OPEN",    GateExpander::GateState::OPEN)
        .value("HOLDING", GateExpander::GateState::HOLDING)
        .value("CLOSING", GateExpander::GateState::CLOSING)
        .export_values();

    // ── DeEsser::Mode enum ────────────────────────────────────────────────────
    py::enum_<DeEsser::Mode>(m, "DeEsserMode")
        .value("WIDEBAND", DeEsser::Mode::WIDEBAND)
        .value("SPLIT",    DeEsser::Mode::SPLIT)
        .export_values();

    // ─────────────────────────────────────────────────────────────────────────
    // BrickwallLimiter
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<BrickwallLimiter>(m, "BrickwallLimiter")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Construct a BrickwallLimiter. sample_rate in Hz (e.g. 44100).")
        .def("prepare",       &BrickwallLimiter::prepare,      py::arg("sample_rate"))
        .def("reset",         &BrickwallLimiter::reset)
        .def("set_ceiling",   &BrickwallLimiter::set_ceiling,  py::arg("db"),
             "Set output ceiling in dBFS (e.g. -0.1).")
        .def("set_lookahead", &BrickwallLimiter::set_lookahead, py::arg("ms"),
             "Set look-ahead delay in milliseconds (max 20ms).")
        .def("set_attack",    &BrickwallLimiter::set_attack,   py::arg("ms"))
        .def("set_release",   &BrickwallLimiter::set_release,  py::arg("ms"))
        .def("process_block",
             [](BrickwallLimiter& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"),
             "Process a block. Returns (out_left, out_right) as new numpy arrays.");

    // ─────────────────────────────────────────────────────────────────────────
    // MultibandCompressor
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<MultibandCompressor>(m, "MultibandCompressor")
        .def(py::init<float>(), py::arg("sample_rate"))
        .def("prepare",        &MultibandCompressor::prepare,       py::arg("sample_rate"))
        .def("reset",          &MultibandCompressor::reset)
        .def("set_crossover",  &MultibandCompressor::set_crossover,
             py::arg("index"), py::arg("hz"),
             "Set crossover frequency. index: 0=low/low-mid, 1=low-mid/mid, 2=mid/high.")
        .def("set_band",       &MultibandCompressor::set_band,
             py::arg("index"), py::arg("config"),
             "Set per-band compressor parameters.")
        .def("process_block",
             [](MultibandCompressor& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ─────────────────────────────────────────────────────────────────────────
    // DynamicEQ
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<DynamicEQ>(m, "DynamicEQ")
        .def(py::init<float>(), py::arg("sample_rate"))
        .def("prepare",        &DynamicEQ::prepare,      py::arg("sample_rate"))
        .def("reset",          &DynamicEQ::reset)
        .def("set_num_bands",  &DynamicEQ::set_num_bands, py::arg("n"),
             "Set number of active bands (1 to 8).")
        .def("set_band",       &DynamicEQ::set_band,
             py::arg("index"), py::arg("band"),
             "Configure a DynEQBand at the given index.")
        .def("process_block",
             [](DynamicEQ& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ─────────────────────────────────────────────────────────────────────────
    // DeEsser
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<DeEsser>(m, "DeEsser")
        .def(py::init<float>(), py::arg("sample_rate"))
        .def("prepare",         &DeEsser::prepare,       py::arg("sample_rate"))
        .def("reset",           &DeEsser::reset)
        .def("set_frequency",   &DeEsser::set_frequency, py::arg("hz"),
             "Set sibilance detection centre frequency in Hz (default 7000).")
        .def("set_threshold",   &DeEsser::set_threshold, py::arg("db"))
        .def("set_ratio",       &DeEsser::set_ratio,     py::arg("ratio"))
        .def("set_attack",      &DeEsser::set_attack,    py::arg("ms"))
        .def("set_release",     &DeEsser::set_release,   py::arg("ms"))
        .def("set_split_mode",  &DeEsser::set_split_mode, py::arg("split"),
             "True = SPLIT mode (HF band only); False = WIDEBAND (full signal).")
        .def("process_block",
             [](DeEsser& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ─────────────────────────────────────────────────────────────────────────
    // TransientShaper
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<TransientShaper>(m, "TransientShaper")
        .def(py::init<float>(), py::arg("sample_rate"))
        .def("prepare",          &TransientShaper::prepare,         py::arg("sample_rate"))
        .def("reset",            &TransientShaper::reset)
        .def("set_attack_gain",  &TransientShaper::set_attack_gain,  py::arg("db"),
             "Gain applied to transient attack portion (−24 to +24 dB).")
        .def("set_sustain_gain", &TransientShaper::set_sustain_gain, py::arg("db"),
             "Gain applied to sustain body portion (−24 to +24 dB).")
        .def("set_fast_attack",  &TransientShaper::set_fast_attack,  py::arg("ms"))
        .def("set_fast_release", &TransientShaper::set_fast_release, py::arg("ms"))
        .def("set_slow_attack",  &TransientShaper::set_slow_attack,  py::arg("ms"))
        .def("set_slow_release", &TransientShaper::set_slow_release, py::arg("ms"))
        .def("process_block",
             [](TransientShaper& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ─────────────────────────────────────────────────────────────────────────
    // GateExpander
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<GateExpander>(m, "GateExpander")
        .def(py::init<float>(), py::arg("sample_rate"))
        .def("prepare",         &GateExpander::prepare,       py::arg("sample_rate"))
        .def("reset",           &GateExpander::reset)
        .def("set_threshold",   &GateExpander::set_threshold,  py::arg("db"),
             "Open threshold in dBFS (default −40).")
        .def("set_hysteresis",  &GateExpander::set_hysteresis, py::arg("db"),
             "Hysteresis between open and close thresholds (default 6 dB).")
        .def("set_ratio",       &GateExpander::set_ratio,      py::arg("r"),
             "Expansion ratio. 1.0 = hard gate; >1 = downward expander.")
        .def("set_attack",      &GateExpander::set_attack,     py::arg("ms"))
        .def("set_hold",        &GateExpander::set_hold,       py::arg("ms"))
        .def("set_release",     &GateExpander::set_release,    py::arg("ms"))
        .def("set_range",       &GateExpander::set_range,      py::arg("db"),
             "Minimum gain when gate is fully closed (default −80 dB).")
        .def("get_state",       &GateExpander::get_state,
             "Returns current GateState enum value.")
        .def("process_block",
             [](GateExpander& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ═════════════════════════════════════════════════════════════════════════
    // Spatial / Time-Based Effects
    // ═════════════════════════════════════════════════════════════════════════

    // ── DelayMode enum ────────────────────────────────────────────────────────
    py::enum_<DelayMode>(m, "DelayMode")
        .value("STEREO",   DelayMode::STEREO)
        .value("PINGPONG", DelayMode::PINGPONG)
        .value("TAPE",     DelayMode::TAPE)
        .export_values();

    // ── DelayDivision enum ────────────────────────────────────────────────────
    py::enum_<DelayDivision>(m, "DelayDivision")
        .value("QUARTER",       DelayDivision::QUARTER)
        .value("DOTTED_EIGHTH", DelayDivision::DOTTED_EIGHTH)
        .value("EIGHTH",        DelayDivision::EIGHTH)
        .export_values();

    // ─────────────────────────────────────────────────────────────────────────
    // DelayEcho
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<DelayEcho>(m, "DelayEcho")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Construct a BPM-synced stereo/ping-pong/tape delay.")
        .def("prepare",         &DelayEcho::prepare,       py::arg("sample_rate"))
        .def("reset",           &DelayEcho::reset)
        .def("set_bpm",         &DelayEcho::set_bpm,       py::arg("bpm"),
             "Host BPM for sync. Pass 0 to use set_delay_ms() instead.")
        .def("set_division",    &DelayEcho::set_division,  py::arg("div"),
             "Beat division: DelayDivision.QUARTER / DOTTED_EIGHTH / EIGHTH.")
        .def("set_delay_ms",    &DelayEcho::set_delay_ms,  py::arg("ms"),
             "Manual delay time in ms (used only when bpm == 0).")
        .def("set_feedback",    &DelayEcho::set_feedback,  py::arg("f"))
        .def("set_wet",         &DelayEcho::set_wet,       py::arg("w"))
        .def("set_hi_cut",      &DelayEcho::set_hi_cut,    py::arg("hz"))
        .def("set_lo_cut",      &DelayEcho::set_lo_cut,    py::arg("hz"))
        .def("set_mode",        &DelayEcho::set_mode,      py::arg("mode"),
             "DelayMode.STEREO / PINGPONG / TAPE.")
        .def("set_tape_rate",   &DelayEcho::set_tape_rate, py::arg("hz"))
        .def("set_tape_depth",  &DelayEcho::set_tape_depth,py::arg("ms"))
        .def("process_block",
             [](DelayEcho& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"),
             "Process a block. Returns (out_left, out_right) as new numpy arrays.");

    // ─────────────────────────────────────────────────────────────────────────
    // Flanger
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<Flanger>(m, "Flanger")
        .def(py::init<float>(), py::arg("sample_rate"))
        .def("prepare",            &Flanger::prepare,          py::arg("sample_rate"))
        .def("reset",              &Flanger::reset)
        .def("set_rate",           &Flanger::set_rate,         py::arg("hz"))
        .def("set_depth",          &Flanger::set_depth,        py::arg("ms"))
        .def("set_center",         &Flanger::set_center,       py::arg("ms"))
        .def("set_feedback",       &Flanger::set_feedback,     py::arg("f"))
        .def("set_wet",            &Flanger::set_wet,          py::arg("w"))
        .def("set_waveform",       &Flanger::set_waveform,     py::arg("w"),
             "0 = sine, 1 = triangle, 2 = square.")
        .def("set_stereo_width",   &Flanger::set_stereo_width, py::arg("w"),
             "0..1: L/R LFO phase offset. 0.5 = 90 degrees (natural stereo).")
        .def("process_block",
             [](Flanger& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ─────────────────────────────────────────────────────────────────────────
    // Phaser
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<Phaser>(m, "Phaser")
        .def(py::init<float>(), py::arg("sample_rate"))
        .def("prepare",            &Phaser::prepare,          py::arg("sample_rate"))
        .def("reset",              &Phaser::reset)
        .def("set_stages",         &Phaser::set_stages,       py::arg("n"),
             "Number of all-pass stages: 2, 4, 6, 8, or 12.")
        .def("set_rate",           &Phaser::set_rate,         py::arg("hz"))
        .def("set_depth",          &Phaser::set_depth,        py::arg("d"))
        .def("set_min_freq",       &Phaser::set_min_freq,     py::arg("hz"))
        .def("set_max_freq",       &Phaser::set_max_freq,     py::arg("hz"))
        .def("set_feedback",       &Phaser::set_feedback,     py::arg("f"))
        .def("set_wet",            &Phaser::set_wet,          py::arg("w"))
        .def("set_stereo_offset",  &Phaser::set_stereo_offset,py::arg("f"),
             "Stereo phase offset as a fraction of one cycle (0..1).")
        .def("process_block",
             [](Phaser& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ─────────────────────────────────────────────────────────────────────────
    // StereoImager
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<StereoImager>(m, "StereoImager")
        .def(py::init<float>(), py::arg("sample_rate"))
        .def("prepare",           &StereoImager::prepare,          py::arg("sample_rate"))
        .def("reset",             &StereoImager::reset)
        .def("set_width",         &StereoImager::set_width,         py::arg("w"),
             "Width: 0 = mono, 1.0 = original, 2.0 = doubled width.")
        .def("set_lf_mono_lock",  &StereoImager::set_lf_mono_lock,  py::arg("enabled"))
        .def("set_crossover_hz",  &StereoImager::set_crossover_hz,  py::arg("hz"))
        .def("get_correlation",   &StereoImager::get_correlation,
             "Returns smoothed Pearson correlation of the last processed block (-1..+1).")
        .def("process_block",
             [](StereoImager& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ═════════════════════════════════════════════════════════════════════════
    // Harmonic & Character Processors
    // ═════════════════════════════════════════════════════════════════════════

    // ── SatMode enum ──────────────────────────────────────────────────────────
    py::enum_<SatMode>(m, "SatMode")
        .value("TUBE", SatMode::TUBE)
        .value("TAPE", SatMode::TAPE)
        .export_values();

    // ─────────────────────────────────────────────────────────────────────────
    // Saturation
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<Saturation>(m, "Saturation")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Tape & Tube harmonic saturation processor.")
        .def("prepare",      &Saturation::prepare,     py::arg("sample_rate"))
        .def("reset",        &Saturation::reset)
        .def("set_mode",     &Saturation::set_mode,    py::arg("mode"),
             "0=TUBE (asymmetric even harmonics), 1=TAPE (symmetric + HF roll-off).")
        .def("set_drive",    &Saturation::set_drive,   py::arg("db"),
             "Drive in dB (0..40). Auto gain compensation applied.")
        .def("set_output",   &Saturation::set_output,  py::arg("db"),
             "Output trim in dB (-24..+12).")
        .def("set_bias",     &Saturation::set_bias,    py::arg("bias"),
             "Tube asymmetry bias (0..1). 0 = symmetric; higher = more 2nd harmonic.")
        .def("get_harm2",    &Saturation::get_harm2,
             "Smoothed peak level in the 880 Hz band (2nd harmonic meter), 0..1.")
        .def("get_harm3",    &Saturation::get_harm3,
             "Smoothed peak level in the 1320 Hz band (3rd harmonic meter), 0..1.")
        .def("get_harm4",    &Saturation::get_harm4,
             "Smoothed peak level in the 1760 Hz band (4th harmonic meter), 0..1.")
        .def("process_block",
             [](Saturation& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"),
             "Process a block. Returns (out_left, out_right) as new numpy arrays.");

    // ── DriveMode enum ────────────────────────────────────────────────────────
    py::enum_<DriveMode>(m, "DriveMode")
        .value("OVERDRIVE",  DriveMode::OVERDRIVE)
        .value("DISTORTION", DriveMode::DISTORTION)
        .value("FUZZ",       DriveMode::FUZZ)
        .export_values();

    // ─────────────────────────────────────────────────────────────────────────
    // Overdrive
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<Overdrive>(m, "Overdrive")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Distortion / Overdrive / Fuzz processor.")
        .def("prepare",         &Overdrive::prepare,       py::arg("sample_rate"))
        .def("reset",           &Overdrive::reset)
        .def("set_mode",        &Overdrive::set_mode,      py::arg("mode"),
             "0=OVERDRIVE (soft asymmetric), 1=DISTORTION (hard), 2=FUZZ (rectify).")
        .def("set_pregain",     &Overdrive::set_pregain,   py::arg("db"),
             "Pre-gain in dB (0..60) applied before the waveshaper.")
        .def("set_tone",        &Overdrive::set_tone,      py::arg("hz"),
             "Tone filter frequency in Hz (200..8000).")
        .def("set_tone_type",   &Overdrive::set_tone_type, py::arg("type"),
             "0=low-pass, 1=high-pass, 2=high-shelf +6 dB (tilt).")
        .def("set_output",      &Overdrive::set_output,    py::arg("db"),
             "Output trim in dB (-24..+6).")
        .def("process_block",
             [](Overdrive& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ─────────────────────────────────────────────────────────────────────────
    // Bitcrusher
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<Bitcrusher>(m, "Bitcrusher")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Bit-depth reduction and sample-rate decimation (lo-fi effect).")
        .def("prepare",              &Bitcrusher::prepare,            py::arg("sample_rate"))
        .def("reset",                &Bitcrusher::reset)
        .def("set_bit_depth",        &Bitcrusher::set_bit_depth,      py::arg("bits"),
             "Effective bit depth (1..24). 16 = CD quality, 8 = retro, 1 = extreme.")
        .def("set_sample_rate_hz",   &Bitcrusher::set_sample_rate_hz, py::arg("hz"),
             "Decimated sample rate in Hz (500..host_sr). Lower = more aliasing.")
        .def("set_wet",              &Bitcrusher::set_wet,            py::arg("w"),
             "Wet/dry mix: 0 = fully dry, 1 = fully crushed.")
        .def("set_dither",           &Bitcrusher::set_dither,         py::arg("enabled"),
             "Enable triangular-PDF dither noise before quantisation.")
        .def("process_block",
             [](Bitcrusher& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ─────────────────────────────────────────────────────────────────────────
    // Exciter
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<Exciter>(m, "Exciter")
        .def(py::init<float>(), py::arg("sample_rate"),
             "High-frequency harmonic exciter / air enhancer.")
        .def("prepare",           &Exciter::prepare,          py::arg("sample_rate"))
        .def("reset",             &Exciter::reset)
        .def("set_crossover_hz",  &Exciter::set_crossover_hz, py::arg("hz"),
             "LR4 crossover frequency in Hz (3000..12000). Only HF is excited.")
        .def("set_harmonics",     &Exciter::set_harmonics,    py::arg("amount"),
             "Harmonic generation drive (0..1). 0 = linear pass-through.")
        .def("set_air",           &Exciter::set_air,          py::arg("db"),
             "High-shelf boost at 8 kHz applied to the excited HF signal (0..12 dB).")
        .def("set_wet",           &Exciter::set_wet,          py::arg("w"),
             "Wet/dry blend: 0 = fully dry, 1 = fully excited signal.")
        .def("process_block",
             [](Exciter& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ═════════════════════════════════════════════════════════════════════════
    // Advanced Utilities & Specialty Filters
    // ═════════════════════════════════════════════════════════════════════════

    // ── ScaleType enum ────────────────────────────────────────────────────────
    py::enum_<ScaleType>(m, "ScaleType")
        .value("MAJOR",     ScaleType::MAJOR)
        .value("MINOR",     ScaleType::MINOR)
        .value("CHROMATIC", ScaleType::CHROMATIC)
        .export_values();

    // ─────────────────────────────────────────────────────────────────────────
    // PitchCorrector  (Auto-Tune style)
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<PitchCorrector>(m, "PitchCorrector")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Real-time Auto-Tune style pitch correction (YIN + OLA).")
        .def("prepare",           &PitchCorrector::prepare,          py::arg("sample_rate"))
        .def("reset",             &PitchCorrector::reset)
        .def("set_scale",         &PitchCorrector::set_scale,        py::arg("scale"),
             "Scale type: ScaleType.MAJOR=0, MINOR=1, CHROMATIC=2.")
        .def("set_root",          &PitchCorrector::set_root,         py::arg("root"),
             "Root note of scale: 0=C, 1=C#/Db … 11=B.")
        .def("set_retune_speed",  &PitchCorrector::set_retune_speed, py::arg("speed"),
             "Retune speed 0..1: 0=instant snap, 1=very slow glide.")
        .def("set_amount",        &PitchCorrector::set_amount,       py::arg("amount"),
             "Correction amount 0..1: 0=no correction, 1=full correction.")
        .def("set_output_gain",   &PitchCorrector::set_output_gain,  py::arg("db"),
             "Output trim in dB (-24..+12).")
        .def("get_detected_hz",   &PitchCorrector::get_detected_hz,
             "Returns last detected fundamental frequency in Hz (0 if unvoiced).")
        .def("get_target_hz",     &PitchCorrector::get_target_hz,
             "Returns last snapped target frequency in Hz.")
        .def("process_block",
             [](PitchCorrector& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"),
             "Process a block. Returns (out_left, out_right) as new numpy arrays.");

    // ─────────────────────────────────────────────────────────────────────────
    // PitchShifter  (manual shift + harmonizer)
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<PitchShifter>(m, "PitchShifter")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Manual pitch shifter with optional harmonizer voice.")
        .def("prepare",                &PitchShifter::prepare,              py::arg("sample_rate"))
        .def("reset",                  &PitchShifter::reset)
        .def("set_semitones",          &PitchShifter::set_semitones,        py::arg("semitones"),
             "Main pitch shift in semitones (-12..+12).")
        .def("set_cents",              &PitchShifter::set_cents,            py::arg("cents"),
             "Fine-tune in cents (-100..+100).")
        .def("set_harmonizer",         &PitchShifter::set_harmonizer,       py::arg("enabled"),
             "Enable/disable the parallel harmony voice.")
        .def("set_harmony_semitones",  &PitchShifter::set_harmony_semitones,py::arg("semi"),
             "Harmony interval relative to original pitch, in semitones (-24..+24).")
        .def("set_mix",                &PitchShifter::set_mix,              py::arg("mix"),
             "Harmony blend 0..1: 0 = harmony silent, 1 = equal level with main voice.")
        .def("set_output_gain",        &PitchShifter::set_output_gain,      py::arg("db"),
             "Output trim in dB (-24..+12).")
        .def("process_block",
             [](PitchShifter& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ── FilterMode enum ───────────────────────────────────────────────────────
    py::enum_<FilterMode>(m, "FilterMode")
        .value("LOWPASS",  FilterMode::LOWPASS)
        .value("HIGHPASS", FilterMode::HIGHPASS)
        .value("BANDPASS", FilterMode::BANDPASS)
        .export_values();

    // ── ModSource enum ────────────────────────────────────────────────────────
    py::enum_<ModSource>(m, "ModSource")
        .value("LFO",      ModSource::LFO)
        .value("ENVELOPE", ModSource::ENVELOPE)
        .value("BOTH",     ModSource::BOTH)
        .export_values();

    // ── LfoShape enum ─────────────────────────────────────────────────────────
    py::enum_<LfoShape>(m, "LfoShape")
        .value("SINE",     LfoShape::SINE)
        .value("TRIANGLE", LfoShape::TRIANGLE)
        .value("SQUARE",   LfoShape::SQUARE)
        .value("SAWUP",    LfoShape::SAWUP)
        .export_values();

    // ─────────────────────────────────────────────────────────────────────────
    // AutoFilter  (resonant multi-mode filter + LFO + envelope follower)
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<AutoFilter>(m, "AutoFilter")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Resonant multi-mode filter with LFO and/or envelope modulation.")
        .def("prepare",           &AutoFilter::prepare,          py::arg("sample_rate"))
        .def("reset",             &AutoFilter::reset)
        .def("set_filter_mode",   &AutoFilter::set_filter_mode,  py::arg("mode"),
             "Filter topology: FilterMode.LOWPASS=0, HIGHPASS=1, BANDPASS=2.")
        .def("set_cutoff_hz",     &AutoFilter::set_cutoff_hz,    py::arg("hz"),
             "Base cutoff frequency in Hz (20..20000).")
        .def("set_resonance",     &AutoFilter::set_resonance,    py::arg("q"),
             "Resonance / Q factor (0.5..12). High Q → self-oscillation.")
        .def("set_drive",         &AutoFilter::set_drive,        py::arg("drive"),
             "Pre-filter tanh saturation amount (0..1).")
        .def("set_mod_source",    &AutoFilter::set_mod_source,   py::arg("src"),
             "Modulation source: ModSource.LFO=0, ENVELOPE=1, BOTH=2.")
        .def("set_lfo_rate_hz",   &AutoFilter::set_lfo_rate_hz,  py::arg("hz"),
             "LFO rate in Hz (0.01..20).")
        .def("set_lfo_depth",     &AutoFilter::set_lfo_depth,    py::arg("depth"),
             "LFO depth 0..1 (controls how many octaves the cutoff sweeps).")
        .def("set_lfo_shape",     &AutoFilter::set_lfo_shape,    py::arg("shape"),
             "LFO waveform: LfoShape.SINE=0, TRIANGLE=1, SQUARE=2, SAWUP=3.")
        .def("set_env_attack_ms", &AutoFilter::set_env_attack_ms,py::arg("ms"),
             "Envelope follower attack time in ms (0.1..500).")
        .def("set_env_release_ms",&AutoFilter::set_env_release_ms,py::arg("ms"),
             "Envelope follower release time in ms (1..5000).")
        .def("set_env_depth",     &AutoFilter::set_env_depth,    py::arg("depth"),
             "Envelope follower depth 0..1 (louder signal → higher cutoff).")
        .def("set_wet",           &AutoFilter::set_wet,          py::arg("wet"),
             "Dry/wet blend: 0 = dry pass-through, 1 = fully filtered.")
        .def("process_block",
             [](AutoFilter& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"));

    // ═════════════════════════════════════════════════════════════════════════
    // Sampler Instrument
    // ═════════════════════════════════════════════════════════════════════════

    // ── VoiceStage enum ───────────────────────────────────────────────────────
    py::enum_<VoiceStage>(m, "VoiceStage")
        .value("IDLE",    VoiceStage::IDLE)
        .value("ATTACK",  VoiceStage::ATTACK)
        .value("DECAY",   VoiceStage::DECAY)
        .value("SUSTAIN", VoiceStage::SUSTAIN)
        .value("RELEASE", VoiceStage::RELEASE)
        .export_values();

    // ─────────────────────────────────────────────────────────────────────────
    // Sampler
    // ─────────────────────────────────────────────────────────────────────────
    py::class_<Sampler>(m, "Sampler")
        .def(py::init<float>(), py::arg("sample_rate"),
             "Polyphonic sample-playback instrument engine (8 voices, ADSR, pitch shift).")
        .def("prepare",        &Sampler::prepare,       py::arg("sample_rate"),
             "Call when the host sample rate changes.")
        .def("reset",          &Sampler::reset,
             "Stop all voices; keep the loaded sample in memory.")
        .def("load_sample",
             [](Sampler& self,
                py::array_t<float, py::array::c_style | py::array::forcecast> data,
                float file_sr,
                int channels)
             {
                 /* Accept any 1-D numpy float32 array from Python.
                  * forcecast ensures we always get a contiguous float buffer. */
                 py::buffer_info info = data.request();
                 if (info.ndim != 1) {
                     throw std::invalid_argument("load_sample: data must be a 1-D numpy array.");
                 }
                 self.load_sample(
                     static_cast<const float*>(info.ptr),
                     static_cast<int>(info.shape[0]),
                     file_sr,
                     channels);
             },
             py::arg("data"), py::arg("file_sr"), py::arg("channels"),
             "Load a flat float32 numpy array as the playback sample.\n"
             "data: 1-D float32 array (mono: [s0,s1,...] / stereo: [L0,R0,L1,R1,...]).\n"
             "file_sr: source sample rate in Hz.  channels: 1 or 2.")
        .def("set_attack_ms",  &Sampler::set_attack_ms,  py::arg("ms"),
             "ADSR attack time in ms (0..5000). Applies to the next note_on.")
        .def("set_decay_ms",   &Sampler::set_decay_ms,   py::arg("ms"),
             "ADSR decay time in ms (0..5000). Applies to the next note_on.")
        .def("set_sustain",    &Sampler::set_sustain,    py::arg("level"),
             "ADSR sustain level 0..1. Applies to the next note_on.")
        .def("set_release_ms", &Sampler::set_release_ms, py::arg("ms"),
             "ADSR release time in ms (0..10000). Applied at each note_off.")
        .def("set_root_note",  &Sampler::set_root_note,  py::arg("midi_note"),
             "MIDI note number that plays the sample at its original pitch (default 60 / C4).")
        .def("note_on",        &Sampler::note_on,        py::arg("midi_note"), py::arg("velocity"),
             "Trigger a new voice. velocity: 0..1. Steals the quietest voice if all 8 are busy.")
        .def("note_off",       &Sampler::note_off,       py::arg("midi_note"),
             "Release the voice matching midi_note; begins the RELEASE envelope stage.")
        .def("sample_loaded",      &Sampler::sample_loaded,
             "Returns True if a sample has been successfully loaded.")
        .def("sample_num_frames",  &Sampler::sample_num_frames,
             "Number of audio frames (not floats) in the loaded sample.")
        .def("active_voice_count", &Sampler::active_voice_count,
             "Number of currently active (non-idle) voices.")
        .def("process_block",
             [](Sampler& self,
                py::array_t<float> left, py::array_t<float> right) {
                 /* The Sampler::process() ADDS to the buffer — using
                  * process_block_impl is correct: it copies input to output
                  * first, then the sampler adds generated audio on top. */
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"),
             "Render one block.  Pass zero buffers for instrument-only output.\n"
             "Returns (out_left, out_right) as new numpy arrays.");

    // ═════════════════════════════════════════════════════════════════════════
    // OfflineExporter  --  32-bit float mix bus + WAV writer
    // ═════════════════════════════════════════════════════════════════════════
    py::class_<OfflineExporter>(m, "OfflineExporter",
        "32-bit float stereo mix bus with TPDF-dithered WAV output.\n\n"
        "Usage:\n"
        "  ex = daw_processors.OfflineExporter()\n"
        "  ex.prepare(sample_rate, total_frames)\n"
        "  ex.mix_in(left_f32, right_f32, at_frame, volume=1.0)\n"
        "  ex.write_wav('output.wav', bit_depth=24)")
        .def(py::init<>())
        .def("prepare",
             &OfflineExporter::prepare,
             py::arg("sample_rate"), py::arg("total_frames"),
             "Allocate the mix bus. Call before mix_in().")
        .def("reset",
             &OfflineExporter::reset,
             "Zero the mix bus (does not reallocate memory).")
        .def("mix_in",
             [](OfflineExporter& self,
                py::array_t<float, py::array::c_style | py::array::forcecast> left,
                py::array_t<float, py::array::c_style | py::array::forcecast> right,
                int   at_frame,
                float volume)
             {
                 py::buffer_info bl = left.request();
                 py::buffer_info br = right.request();
                 if (bl.ndim != 1 || br.ndim != 1)
                     throw std::invalid_argument(
                         "mix_in: left and right must be 1-D float32 arrays.");
                 const int n = static_cast<int>(bl.shape[0]);
                 self.mix_in(
                     static_cast<const float*>(bl.ptr),
                     static_cast<const float*>(br.ptr),
                     n, at_frame, volume);
             },
             py::arg("left"), py::arg("right"),
             py::arg("at_frame"), py::arg("volume") = 1.0f,
             "Accumulate a stereo block at the given frame offset.\n"
             "left / right: 1-D float32 numpy arrays of equal length.")
        .def("write_wav",
             &OfflineExporter::write_wav,
             py::arg("path"), py::arg("bit_depth") = 24,
             "Write the mix bus to a WAV file.\n"
             "bit_depth: 16 (CD), 24 (broadcast, default), or 32 (IEEE float).\n"
             "TPDF dithering is applied for 16-bit and 24-bit output.\n"
             "Returns True on success.")
        .def("peak_left",    &OfflineExporter::peak_left,
             "Peak sample magnitude in the left channel (0..∞).")
        .def("peak_right",   &OfflineExporter::peak_right,
             "Peak sample magnitude in the right channel (0..∞).")
        .def("sample_rate",  &OfflineExporter::sample_rate)
        .def("total_frames", &OfflineExporter::total_frames);

    // ═════════════════════════════════════════════════════════════════════════
    // TimelineEngine  --  real-time C++ transport + audio/MIDI scheduler
    // ═════════════════════════════════════════════════════════════════════════

    // ── TimelineTrackType enum ────────────────────────────────────────────────
    py::enum_<TimelineTrackType>(m, "TimelineTrackType",
        "Track content type: AUDIO (PCM clips) or INSTRUMENT (MIDI + Sampler).")
        .value("AUDIO",      TimelineTrackType::AUDIO)
        .value("INSTRUMENT", TimelineTrackType::INSTRUMENT)
        .export_values();

    // ── PendingMidiEvent struct ───────────────────────────────────────────────
    py::class_<PendingMidiEvent>(m, "PendingMidiEvent",
        "A MIDI event that fired during the most recent process_block_into() call.")
        .def_readonly("channel",  &PendingMidiEvent::channel,
             "MIDI channel 0-15.")
        .def_readonly("note",     &PendingMidiEvent::note,
             "MIDI note number 0-127.")
        .def_readonly("velocity", &PendingMidiEvent::velocity,
             "Velocity 0-127 (0 = note-off).")
        .def_readonly("is_on",    &PendingMidiEvent::is_on,
             "True = note-on, False = note-off.")
        .def("__repr__", [](const PendingMidiEvent& e) {
            return std::string("<PendingMidiEvent ch=") + std::to_string(e.channel)
                 + " note=" + std::to_string(e.note)
                 + " vel=" + std::to_string(e.velocity)
                 + (e.is_on ? " ON>" : " OFF>");
        });

    // ── TimelineEngine ────────────────────────────────────────────────────────
    py::class_<TimelineEngine>(m, "TimelineEngine",
        "Zero-allocation C++ real-time audio/MIDI timeline engine.\n\n"
        "Audio callback thread: process_block_into(), pop_midi_events(),\n"
        "                       current_frame(), current_beat(), is_playing().\n"
        "GUI / Python thread:   everything else (serialised by internal mutex).")

        // ── Construction ──────────────────────────────────────────────────────
        .def(py::init<int, double>(),
             py::arg("sample_rate") = 44100, py::arg("bpm") = 120.0,
             "Create a new TimelineEngine.\n"
             "sample_rate: host sample rate in Hz.  bpm: initial tempo.")

        // ── Transport ─────────────────────────────────────────────────────────
        .def("play",
             &TimelineEngine::play,
             py::arg("from_frame") = 0,
             "Start playback from the given sample frame.")
        .def("stop",
             &TimelineEngine::stop,
             "Stop playback.  Playhead position is preserved.")
        .def("seek",
             &TimelineEngine::seek,
             py::arg("frame"),
             "Jump to an absolute sample frame without changing play/stop state.")
        .def("set_loop",
             &TimelineEngine::set_loop,
             py::arg("enabled"), py::arg("start_frame"), py::arg("end_frame"),
             "Configure loop region.  start_frame must be < end_frame.")

        // ── Playhead queries (lock-free, safe from any thread) ────────────────
        .def("current_frame",
             &TimelineEngine::current_frame,
             "Current sample-frame position (atomic read, O(1)).")
        .def("current_beat",
             &TimelineEngine::current_beat,
             "Current beat position derived from current_frame() and BPM.")
        .def("is_playing",
             &TimelineEngine::is_playing,
             "True while the transport is running.")

        // ── Host settings ─────────────────────────────────────────────────────
        .def("set_bpm",
             &TimelineEngine::set_bpm,
             py::arg("bpm"),
             "Update tempo (takes effect at the next process_block_into() call).")
        .def("set_sample_rate",
             &TimelineEngine::set_sample_rate,
             py::arg("sr"),
             "Update host sample rate.")
        .def("bpm",
             &TimelineEngine::bpm,
             "Current BPM.")
        .def("sample_rate",
             &TimelineEngine::sample_rate,
             "Current sample rate in Hz.")

        // ── Track management ──────────────────────────────────────────────────
        .def("add_audio_track",
             &TimelineEngine::add_audio_track,
             "Add an AUDIO track and return its new unique ID.")
        .def("add_instrument_track",
             &TimelineEngine::add_instrument_track,
             "Add an INSTRUMENT track (MIDI + optional Sampler) and return its ID.")
        .def("remove_track",
             &TimelineEngine::remove_track,
             py::arg("id"),
             "Remove a track by ID, freeing all clips, events, and Sampler.")
        .def("track_count",
             &TimelineEngine::track_count,
             "Total number of registered tracks.")

        // ── Per-track routing ─────────────────────────────────────────────────
        .def("set_track_volume",
             &TimelineEngine::set_track_volume,
             py::arg("id"), py::arg("volume"),
             "Set output gain for a track (0 = mute, 1 = unity, 2 = +6 dB).")
        .def("set_track_pan",
             &TimelineEngine::set_track_pan,
             py::arg("id"), py::arg("pan"),
             "Set stereo pan: -1 (full L), 0 (centre), +1 (full R).")
        .def("set_track_mute",
             &TimelineEngine::set_track_mute,
             py::arg("id"), py::arg("muted"))
        .def("set_track_solo",
             &TimelineEngine::set_track_solo,
             py::arg("id"), py::arg("soloed"))
        .def("get_track_volume",
             &TimelineEngine::get_track_volume, py::arg("id"))
        .def("get_track_pan",
             &TimelineEngine::get_track_pan,    py::arg("id"))
        .def("get_track_mute",
             &TimelineEngine::get_track_mute,   py::arg("id"))
        .def("get_track_solo",
             &TimelineEngine::get_track_solo,   py::arg("id"))

        // ── Audio clip management ─────────────────────────────────────────────
        .def("load_audio_clip",
             [](TimelineEngine& self,
                int track_id,
                py::array_t<float, py::array::c_style | py::array::forcecast> left,
                py::array_t<float, py::array::c_style | py::array::forcecast> right,
                int64_t start_frame,
                const std::string& path)
             {
                 py::buffer_info bl = left.request();
                 py::buffer_info br = right.request();
                 const auto* pl = static_cast<const float*>(bl.ptr);
                 const auto* pr = static_cast<const float*>(br.ptr);
                 std::vector<float> lv(pl, pl + bl.shape[0]);
                 std::vector<float> rv(pr, pr + br.shape[0]);
                 self.load_audio_clip(track_id, lv, rv, start_frame, path);
             },
             py::arg("track_id"),
             py::arg("left"), py::arg("right"),
             py::arg("start_frame"),
             py::arg("path") = "",
             "Push a pre-decoded PCM clip to an AUDIO track.\n"
             "left/right: 1-D float32 numpy arrays (normalised ±1).\n"
             "start_frame: absolute sample-frame position on the timeline.")
        .def("clear_audio_clips",
             &TimelineEngine::clear_audio_clips,
             py::arg("track_id"),
             "Remove all audio clips from a track.")

        // ── MIDI event management ─────────────────────────────────────────────
        .def("add_midi_event",
             &TimelineEngine::add_midi_event,
             py::arg("track_id"), py::arg("frame_pos"),
             py::arg("type"), py::arg("channel"),
             py::arg("note"), py::arg("velocity"),
             "Add one MIDI event to an INSTRUMENT track.\n"
             "type: 0x90 = note-on, 0x80 = note-off.\n"
             "frame_pos: absolute sample-frame position.")
        .def("clear_midi_events",
             &TimelineEngine::clear_midi_events,
             py::arg("track_id"),
             "Remove all MIDI events from an INSTRUMENT track.")
        .def("sort_midi_events",
             &TimelineEngine::sort_midi_events,
             py::arg("track_id"),
             "Re-sort MIDI events by frame position.  Call after bulk add_midi_event().")

        // ── Sampler loading ───────────────────────────────────────────────────
        .def("load_sample",
             [](TimelineEngine& self,
                int track_id,
                py::array_t<float, py::array::c_style | py::array::forcecast> left,
                py::array_t<float, py::array::c_style | py::array::forcecast> right,
                int file_sample_rate,
                int midi_root_note)
             {
                 py::buffer_info bl = left.request();
                 py::buffer_info br = right.request();
                 const auto* pl = static_cast<const float*>(bl.ptr);
                 const auto* pr = static_cast<const float*>(br.ptr);
                 std::vector<float> lv(pl, pl + bl.shape[0]);
                 std::vector<float> rv(pr, pr + br.shape[0]);
                 return self.load_sample(track_id, lv, rv, file_sample_rate, midi_root_note);
             },
             py::arg("track_id"),
             py::arg("left"), py::arg("right"),
             py::arg("file_sample_rate"),
             py::arg("midi_root_note") = 60,
             "Load a PCM sample into the Sampler owned by an INSTRUMENT track.\n"
             "Creates the Sampler if it does not yet exist.\n"
             "Returns True if the sample loaded successfully.")

        // ── Pending MIDI event queue ──────────────────────────────────────────
        .def("pop_midi_events",
             &TimelineEngine::pop_midi_events,
             "Drain and return all MIDI events deposited by the most recent\n"
             "process_block_into() call as a list of PendingMidiEvent.\n"
             "Thread-safe: performs an O(1) vector swap under a spinlock.")

        // ── Main audio processing  (GIL released during C++ processing) ───────
        .def("process_block_into",
             [](TimelineEngine& self,
                py::array_t<float> out_left,
                py::array_t<float> out_right)
             {
                 py::buffer_info bl = out_left.request(true);    // writable
                 py::buffer_info br = out_right.request(true);
                 if (bl.ndim != 1 || br.ndim != 1)
                     throw std::invalid_argument(
                         "process_block_into: both arrays must be 1-D.");
                 const int n = static_cast<int>(bl.shape[0]);
                 if (n != static_cast<int>(br.shape[0]))
                     throw std::invalid_argument(
                         "process_block_into: left and right arrays must have equal length.");
                 {
                     py::gil_scoped_release release;
                     self.process_block_into(
                         static_cast<float*>(bl.ptr),
                         static_cast<float*>(br.ptr),
                         n);
                 }
             },
             py::arg("out_left"), py::arg("out_right"),
             "Fill pre-allocated stereo float32 arrays in-place.\n"
             "Both arrays must be contiguous, writable, 1-D float32 and equal length\n"
             "(≤ 4096 = MAX_BLOCK_SIZE).  The GIL is released during C++ processing\n"
             "so other Python threads remain unblocked.");

    // ─────────────────────────────────────────────────────────────────────────
    // WaveformGenerator  --  fast peak-array generation for waveform display
    // ─────────────────────────────────────────────────────────────────────────
    // generate_peaks_from_array(samples, channels, n_peaks) -> list[float]
    //
    // Accepts a 1-D float32 numpy array of INTERLEAVED PCM samples
    // (shape = [total_frames * channels]).  Returns n_peaks normalised
    // peak values in [0.0, 1.0] suitable for waveform rendering.
    //
    // The GIL is released during the C++ computation so the GUI thread
    // remains responsive while peaks are generated in a background thread.
    m.def("generate_peaks_from_array",
          [](py::array_t<float> samples, int channels, int n_peaks)
          -> std::vector<float>
          {
              if (samples.ndim() != 1)
                  throw std::invalid_argument(
                      "generate_peaks_from_array: samples must be a 1-D float32 array.");
              if (channels <= 0)
                  throw std::invalid_argument("channels must be > 0.");
              if (n_peaks <= 0)
                  throw std::invalid_argument("n_peaks must be > 0.");

              const py::buffer_info info = samples.request();
              const int total_samples = static_cast<int>(info.shape[0]);
              const int total_frames  = total_samples / channels;
              const float* ptr        = static_cast<const float*>(info.ptr);

              std::vector<float> result;
              {
                  // Release the GIL: peak generation is CPU-bound, not Python.
                  py::gil_scoped_release release;
                  result = WaveformGenerator::generate_peaks(
                      ptr, total_frames, channels, n_peaks);
              }
              return result;
          },
          py::arg("samples"),
          py::arg("channels"),
          py::arg("n_peaks") = 2000,
          "Generate normalised amplitude peaks from interleaved float32 PCM data.\n"
          "samples  : 1-D float32 numpy array, shape [total_frames * channels].\n"
          "channels : number of audio channels (1=mono, 2=stereo, ...).\n"
          "n_peaks  : number of peak values to return (default 2000).\n"
          "Returns  : list of floats in [0.0, 1.0], length <= n_peaks.\n"
          "The GIL is released during C++ processing.");

    // ─────────────────────────────────────────────────────────────────────────
    // VelocityHumanizer  --  Gaussian + timing-weight MIDI velocity humanizer
    // ─────────────────────────────────────────────────────────────────────────

    // Expose the Params struct so Python code can construct and pass it.
    py::class_<VelocityHumanizer::Params>(m, "VelocityHumanizerParams")
        .def(py::init<>())
        .def_readwrite("sigma",             &VelocityHumanizer::Params::sigma,
                       "Gaussian spread in velocity units [0, 64].")
        .def_readwrite("downbeat_boost",    &VelocityHumanizer::Params::downbeat_boost,
                       "Fractional velocity boost on bar's beat 1 [0, 1].")
        .def_readwrite("offbeat_reduction", &VelocityHumanizer::Params::offbeat_reduction,
                       "Fractional velocity cut on weak offbeats [0, 1].")
        .def_readwrite("time_sig_num",      &VelocityHumanizer::Params::time_sig_num,
                       "Beats per bar (time-signature numerator).")
        .def_readwrite("time_sig_denom",    &VelocityHumanizer::Params::time_sig_denom,
                       "Beat value (time-signature denominator; 4 = quarter note).")
        .def_readwrite("snap_tolerance",    &VelocityHumanizer::Params::snap_tolerance,
                       "Grid-snap window in beats; notes beyond this are off-grid.")
        .def_readwrite("seed",              &VelocityHumanizer::Params::seed,
                       "PRNG seed; 0 = built-in constant for reproducible defaults.");

    // Expose the main VelocityHumanizer class.
    py::class_<VelocityHumanizer>(m, "VelocityHumanizer")
        .def(py::init<VelocityHumanizer::Params>(),
             py::arg("params") = VelocityHumanizer::Params{},
             "Construct with optional initial Params.")
        .def("humanize",
             &VelocityHumanizer::humanize,
             py::arg("base_velocity"),
             py::arg("beat_position"),
             "Humanize one MIDI velocity.\n"
             "base_velocity : automation-line target [1, 127]\n"
             "beat_position : absolute beat from song start (0.0 = bar 1 beat 1)\n"
             "Returns an integer in [1, 127].")
        .def("set_params",
             &VelocityHumanizer::set_params,
             py::arg("params"),
             "Replace all parameters (updates timing config and optional PRNG seed).")
        .def("reseed",
             &VelocityHumanizer::reseed,
             py::arg("seed"),
             "Re-seed the Gaussian RNG for reproducible offline export.")
        .def_property_readonly("params",
             &VelocityHumanizer::params,
             "Read the current Params snapshot.");

    // Expose GaussianRng.pdf() as a free function for UI curve visualisation.
    m.def("gaussian_pdf",
          &GaussianRng::pdf,
          py::arg("x"), py::arg("mu"), py::arg("sigma"),
          "Evaluate the Gaussian PDF f(x) = 1/(σ√(2π))·exp(-½·((x-μ)/σ)²).\n"
          "Useful for drawing the distribution curve in the parameter panel.");

    // ═════════════════════════════════════════════════════════════════════════
    // LoudnessAutomation  --  RMS analyser + envelope follower + PID + gain ramp
    // ═════════════════════════════════════════════════════════════════════════

    // Expose the Params struct so Python can construct and configure it.
    py::class_<LoudnessAutomation::Params>(m, "LoudnessAutomationParams")
        .def(py::init<>())
        .def_readwrite("target_dbfs",  &LoudnessAutomation::Params::target_dbfs,
                       "Desired RMS loudness target in dBFS (default -18).")
        .def_readwrite("attack_ms",    &LoudnessAutomation::Params::attack_ms,
                       "Envelope follower attack time in ms (default 20).")
        .def_readwrite("release_ms",   &LoudnessAutomation::Params::release_ms,
                       "Envelope follower release time in ms (default 200).")
        .def_readwrite("kp",           &LoudnessAutomation::Params::kp,
                       "PID proportional gain (default 1.0).")
        .def_readwrite("ki",           &LoudnessAutomation::Params::ki,
                       "PID integral gain (default 0.1).")
        .def_readwrite("kd",           &LoudnessAutomation::Params::kd,
                       "PID derivative gain (default 0.05).")
        .def_readwrite("gain_min_db",  &LoudnessAutomation::Params::gain_min_db,
                       "Minimum allowed gain correction in dB (default -30).")
        .def_readwrite("gain_max_db",  &LoudnessAutomation::Params::gain_max_db,
                       "Maximum allowed gain correction in dB (default +12).");

    // Expose the main LoudnessAutomation processor.
    py::class_<LoudnessAutomation>(m, "LoudnessAutomation")
        .def(py::init<float>(),
             py::arg("sample_rate") = 44100.0f,
             "Construct a LoudnessAutomation processor at the given sample rate.")
        .def("prepare",
             &LoudnessAutomation::prepare,
             py::arg("sample_rate"),
             "Re-initialise at a new sample rate.  Recomputes envelope coefficients.")
        .def("set_params",
             &LoudnessAutomation::set_params,
             py::arg("params"),
             "Replace all parameters.  Rebuilds EnvelopeFollower and PID.")
        .def_property_readonly("params",
             &LoudnessAutomation::params,
             "Read the current Params snapshot.")
        .def("reset",
             &LoudnessAutomation::reset,
             "Zero all internal controller state (envelope, integral, gain).")
        .def("current_gain",
             &LoudnessAutomation::current_gain,
             "Current instantaneous gain multiplier (linear, not dB).")
        .def("current_gain_db",
             &LoudnessAutomation::current_gain_db,
             "Current gain in dB for metering.  Returns -120 when near-silent.")
        .def("process_block",
             [](LoudnessAutomation& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"),
             "Process one stereo block in-place (per-sample gain interpolation).\n"
             "Returns (out_left, out_right) as new numpy arrays.\n"
             "Gain changes are applied per-sample to prevent zipper noise.");

    // Expose RmsAnalyzer static helpers as free functions for Python metering.
    m.def("rms_to_dbfs",
          &RmsAnalyzer::to_dbfs,
          py::arg("rms"),
          "Convert linear RMS amplitude to dBFS.  Returns -120 for near-zero input.");
    m.def("dbfs_to_linear",
          &RmsAnalyzer::from_dbfs,
          py::arg("db"),
          "Convert dBFS to a linear amplitude multiplier.");

    // ═════════════════════════════════════════════════════════════════════════
    // SpectralPanningProcessor  --  FFT centroid analyser + masking panner
    // ═════════════════════════════════════════════════════════════════════════

    // Expose the Params struct so Python can configure and pass it.
    py::class_<SpectralPanningProcessor::Params>(m, "SpectralPanningParams")
        .def(py::init<>())
        .def_readwrite("group_id",     &SpectralPanningProcessor::Params::group_id,
                       "Integer group ID shared between the two paired plugins.")
        .def_readwrite("slot",         &SpectralPanningProcessor::Params::slot,
                       "Slot index: 0 = track A (pans left), 1 = track B (pans right).")
        .def_readwrite("tolerance_hz", &SpectralPanningProcessor::Params::tolerance_hz,
                       "Masking detection threshold in Hz (default 300).")
        .def_readwrite("max_pan",      &SpectralPanningProcessor::Params::max_pan,
                       "Maximum pan deflection [0, 1] (default 0.5).")
        .def_readwrite("smooth_ms",    &SpectralPanningProcessor::Params::smooth_ms,
                       "LP filter time constant for pan transitions in ms (default 100).");

    // Expose the main SpectralPanningProcessor.
    py::class_<SpectralPanningProcessor>(m, "SpectralPanningProcessor")
        .def(py::init<float>(),
             py::arg("sample_rate") = 44100.0f,
             "Construct a SpectralPanningProcessor at the given sample rate.")
        .def("prepare",
             &SpectralPanningProcessor::prepare,
             py::arg("sample_rate"),
             "Re-initialise at a new sample rate.  Clears analyzer and manager state.")
        .def("set_params",
             &SpectralPanningProcessor::set_params,
             py::arg("params"),
             "Replace all parameters.")
        .def_property_readonly("params",
             &SpectralPanningProcessor::params,
             "Read the current Params snapshot.")
        .def("reset",
             &SpectralPanningProcessor::reset,
             "Zero all internal state.")
        .def("get_centroid",
             &SpectralPanningProcessor::get_centroid,
             "Return the most recent spectral centroid in Hz.")
        .def("get_current_pan",
             &SpectralPanningProcessor::get_current_pan,
             "Return the current smoothed pan value for this track [-1, +1].")
        .def("process_block",
             [](SpectralPanningProcessor& self,
                py::array_t<float> left, py::array_t<float> right) {
                 return process_block_impl(self, left, right);
             },
             py::arg("left"), py::arg("right"),
             "Analyze + pan one stereo block (equal-power pan law).\n"
             "Returns (out_left, out_right) as new numpy arrays.\n"
             "Shared masking state is updated automatically via the singleton manager.");

    // =========================================================================
    // Grid snap system
    // =========================================================================

    // ── RulerMode enum ────────────────────────────────────────────────────────
    py::enum_<RulerMode>(m, "RulerMode")
        .value("BarsBeats", RulerMode::BarsBeats)
        .value("Time",      RulerMode::Time)
        .value("SMPTE",     RulerMode::SMPTE)
        .export_values();

    // ── GridType enum ─────────────────────────────────────────────────────────
    py::enum_<GridType>(m, "GridType")
        .value("Straight", GridType::Straight)
        .value("Triplet",  GridType::Triplet)
        .value("Dotted",   GridType::Dotted)
        .value("Free",     GridType::Free)
        .export_values();

    // ── GridValue struct ──────────────────────────────────────────────────────
    py::class_<GridValue>(m, "GridValue")
        .def_readonly("label",    &GridValue::label,
                      "Human-readable label, e.g. '1/16', '1/8T', 'Free'.")
        .def_readonly("type",     &GridValue::type,
                      "GridType enum tag.")
        .def_readonly("division", &GridValue::division,
                      "Note denominator (4 = quarter, 16 = sixteenth …).")
        .def_readonly("ticks",    &GridValue::ticks,
                      "Duration in PPQN ticks (960 PPQN).")
        .def_readonly("beats",    &GridValue::beats,
                      "Duration in quarter-note beats.");

    // ── GridDefinition static helpers ─────────────────────────────────────────
    py::class_<GridDefinition>(m, "GridDefinition")
        .def_static("all_grids",       &GridDefinition::all_grids,
                    "Return the complete ordered list of all GridValue entries.")
        .def_static("find",            &GridDefinition::find,
                    py::arg("label"),
                    "Look up a GridValue by label string.  Returns None if not found.",
                    py::return_value_policy::reference)
        .def_static("ticks_to_beats",  &GridDefinition::ticks_to_beats,
                    py::arg("ticks"),
                    "Convert PPQN ticks to quarter-note beats.")
        .def_static("beats_to_ticks",  &GridDefinition::beats_to_ticks,
                    py::arg("beats"),
                    "Convert beats to PPQN ticks (truncated to integer).");

    // ── QuantizeEngine static helpers ─────────────────────────────────────────
    py::class_<QuantizeEngine>(m, "QuantizeEngine")
        .def_static("snap_nearest",
                    &QuantizeEngine::snap_nearest,
                    py::arg("beat"), py::arg("grid_beats"),
                    "Snap beat to the nearest grid multiple.")
        .def_static("snap_floor",
                    &QuantizeEngine::snap_floor,
                    py::arg("beat"), py::arg("grid_beats"),
                    "Snap beat down to the grid multiple at or before beat.")
        .def_static("snap_ceil",
                    &QuantizeEngine::snap_ceil,
                    py::arg("beat"), py::arg("grid_beats"),
                    "Snap beat up to the grid multiple at or after beat.")
        .def_static("quantize",
                    &QuantizeEngine::quantize,
                    py::arg("beat"), py::arg("grid_beats"), py::arg("strength"),
                    "Blend beat toward its snapped position (strength 0..1).")
        .def_static("grid_positions",
                    &QuantizeEngine::grid_positions,
                    py::arg("start_beat"), py::arg("end_beat"), py::arg("grid_beats"),
                    "Return all grid-line beat positions in [start_beat, end_beat].");

    // ── RulerLabel struct ─────────────────────────────────────────────────────
    py::class_<RulerLabel>(m, "RulerLabel")
        .def_readonly("beat",     &RulerLabel::beat,     "Horizontal position in beats.")
        .def_readonly("text",     &RulerLabel::text,     "Formatted label string.")
        .def_readonly("is_major", &RulerLabel::is_major, "True for bar lines.");

    // ── TimelineRuler static helpers ──────────────────────────────────────────
    py::class_<TimelineRuler>(m, "TimelineRuler")
        .def_static("format_bars_beats",
                    &TimelineRuler::format_bars_beats,
                    py::arg("beat"), py::arg("time_sig") = 4,
                    "Format beat as 'BAR N' or 'bar:beat'.")
        .def_static("format_time",
                    &TimelineRuler::format_time,
                    py::arg("beat"), py::arg("bpm"),
                    "Format beat as 'mm:ss.ms'.")
        .def_static("format_smpte",
                    &TimelineRuler::format_smpte,
                    py::arg("beat"), py::arg("bpm"), py::arg("fps"),
                    "Format beat as 'HH:MM:SS:FF' SMPTE timecode.")
        .def_static("ruler_labels",
                    &TimelineRuler::ruler_labels,
                    py::arg("start_beat"), py::arg("end_beat"),
                    py::arg("pixels_per_beat"), py::arg("bpm"),
                    py::arg("mode"),
                    py::arg("fps")      = 30.0,
                    py::arg("time_sig") = 4,
                    "Generate a list of RulerLabel entries for the visible range.");

    // ── GridSnapper ───────────────────────────────────────────────────────────
    py::class_<GridSnapper>(m, "GridSnapper")
        .def(py::init<>(),
             "Construct a GridSnapper with default settings (1/16 grid, BarsBeats ruler).")
        .def("set_grid",        &GridSnapper::set_grid,
             py::arg("label"),
             "Set the active grid by label string.")
        .def("grid_label",      &GridSnapper::grid_label,
             "Return the active grid label.")
        .def("grid_beats",      &GridSnapper::grid_beats,
             "Return the grid step in quarter-note beats.")
        .def("set_strength",    &GridSnapper::set_strength,
             py::arg("s"),
             "Set quantize strength [0 = off, 1 = full snap].")
        .def("strength",        &GridSnapper::strength,
             "Return the current quantize strength.")
        .def("set_ruler_mode",  &GridSnapper::set_ruler_mode,
             py::arg("mode_str"),
             "Set ruler mode: 'BarsBeats', 'Time', or 'SMPTE'.")
        .def("ruler_mode_str",  &GridSnapper::ruler_mode_str,
             "Return the current ruler mode as a string.")
        .def("set_fps",         &GridSnapper::set_fps,
             py::arg("fps"),
             "Set the SMPTE frame rate (24, 25, 29.97, 30).")
        .def("fps",             &GridSnapper::fps,
             "Return the SMPTE frame rate.")
        .def("set_bpm",         &GridSnapper::set_bpm,
             py::arg("bpm"),
             "Set the tempo in BPM (used for Time/SMPTE ruler).")
        .def("bpm",             &GridSnapper::bpm,
             "Return the tempo in BPM.")
        .def("set_time_sig",    &GridSnapper::set_time_sig,
             py::arg("beats_per_bar"),
             "Set the time signature numerator (beats per bar).")
        .def("time_sig",        &GridSnapper::time_sig,
             "Return the beats-per-bar value.")
        .def("snap",            &GridSnapper::snap,
             py::arg("beat"),
             "Snap a beat position to the nearest grid line.")
        .def("grid_lines",      &GridSnapper::grid_lines,
             py::arg("start_beat"), py::arg("end_beat"),
             "Return all grid-line positions in [start_beat, end_beat].")
        .def("ruler_labels",    &GridSnapper::ruler_labels,
             py::arg("start_beat"), py::arg("end_beat"), py::arg("pixels_per_beat"),
             "Return formatted ruler labels for the visible range.")
        .def("format_position", &GridSnapper::format_position,
             py::arg("beat"),
             "Format a single beat position using the current ruler mode.");

    // =========================================================================
    // AudioLoopScheduler
    // =========================================================================

    py::class_<AudioLoopScheduler>(m, "AudioLoopScheduler",
        "Precision audio-loop boundary scheduler.\n\n"
        "Uses steady_clock + sleep_until for sub-millisecond loop boundary\n"
        "precision.  Calls stop_fn at every loop boundary before firing clips\n"
        "for the next iteration so old audio never overlaps new playback.")

        .def(py::init<>(),
             "Construct a scheduler.  Call set_bpm, set_loop, set_clip_fn, \n"
             "set_stop_fn, add_clip before calling play().")

        // ── Configuration ─────────────────────────────────────────────────────
        .def("set_bpm",
             &AudioLoopScheduler::set_bpm,
             py::arg("bpm"),
             "Set tempo in BPM.")
        .def("set_loop",
             &AudioLoopScheduler::set_loop,
             py::arg("enabled"), py::arg("start_beat"), py::arg("end_beat"),
             "Configure loop region.  enabled=False plays clips once then stops.")
        .def("set_clip_fn",
             [](AudioLoopScheduler& self, py::object fn) {
                 // Wrap the Python callable with GIL acquisition since the
                 // C++ worker thread calls it without holding the GIL.
                 self.set_clip_fn(
                     [fn](int tid, const std::string& path,
                           double remaining, double offset) {
                         py::gil_scoped_acquire acquire;
                         fn(tid, path, remaining, offset);
                     });
             },
             py::arg("fn"),
             "Set the clip-start callback: fn(track_id, path, remaining_secs, offset_secs).")
        .def("set_stop_fn",
             [](AudioLoopScheduler& self, py::object fn) {
                 self.set_stop_fn([fn]() {
                     py::gil_scoped_acquire acquire;
                     fn();
                 });
             },
             py::arg("fn"),
             "Set the stop-all callback: fn() — called at every loop boundary.")

        // ── Clip list ─────────────────────────────────────────────────────────
        .def("add_clip",
             &AudioLoopScheduler::add_clip,
             py::arg("track_id"), py::arg("path"),
             py::arg("start_beat"), py::arg("duration_secs"),
             "Add a clip.  Call before play().  duration_secs=0 means unknown.")
        .def("clear_clips",
             &AudioLoopScheduler::clear_clips,
             "Remove all clips.")

        // ── Transport ─────────────────────────────────────────────────────────
        .def("play",
             &AudioLoopScheduler::play,
             py::arg("from_beat") = 0.0,
             "Start scheduling.  from_beat is the initial playhead position.",
             py::call_guard<py::gil_scoped_release>())
        .def("stop",
             &AudioLoopScheduler::stop,
             "Stop the scheduler.  Blocks until the worker thread exits.",
             py::call_guard<py::gil_scoped_release>())
        .def("is_playing",
             &AudioLoopScheduler::is_playing,
             "True while the scheduler is running.")
        .def("current_beat",
             &AudioLoopScheduler::current_beat,
             "Lock-free interpolated beat position.  Safe from any thread.");

    // =========================================================================
    // AutomationProcessor  --  piecewise-linear automation curve interpolator
    // =========================================================================
    py::class_<AutomationProcessor>(m, "AutomationProcessor",
        "Piecewise-linear automation curve interpolator.\n\n"
        "Store (time_secs, value) control points with add_point(), then query\n"
        "via value_at() or generate a per-sample buffer with fill_buffer().\n"
        "Used by FullProjectRenderer to apply volume / pan automation per frame.")

        .def(py::init<>(),
             "Construct an empty AutomationProcessor (no control points).")

        // ── Population ────────────────────────────────────────────────────────
        .def("add_point",
             &AutomationProcessor::add_point,
             py::arg("time_secs"), py::arg("value"),
             "Add one (time_secs, value) control point.  Out-of-order inserts allowed.")
        .def("clear_points",
             &AutomationProcessor::clear_points,
             "Remove all control points.")
        .def("has_points",
             &AutomationProcessor::has_points,
             "True if at least one control point exists.")

        // ── Query ─────────────────────────────────────────────────────────────
        .def("value_at",
             &AutomationProcessor::value_at,
             py::arg("time_secs"),
             "Return the linearly-interpolated value at time_secs.\n"
             "Clamps to the first / last value outside the defined range.")
        .def("fill_buffer",
             [](const AutomationProcessor& self,
                int n_frames, double start_secs, double sample_rate)
             -> py::array_t<float>
             {
                 // Allocate output numpy array and fill in C++.
                 py::array_t<float> out(n_frames);
                 self.fill_buffer(out.mutable_data(), n_frames,
                                  start_secs, sample_rate);
                 return out;
             },
             py::arg("n_frames"), py::arg("start_secs"), py::arg("sample_rate"),
             "Generate a float32 numpy array of length n_frames.\n"
             "out[i] = value_at(start_secs + i / sample_rate).");

    // =========================================================================
    // FullProjectRenderer  --  stereo offline mix bus for mastering
    // =========================================================================
    py::class_<FullProjectRenderer>(m, "FullProjectRenderer",
        "Stereo offline mix bus for full-project mastering export.\n\n"
        "Usage:\n"
        "  r = daw_processors.FullProjectRenderer()\n"
        "  r.prepare(n_frames, 44100)\n"
        "  r.mix_track(L, R, at_frame=0, volume=1.0, pan=0.0)\n"
        "  mix = np.vstack([np.array(r.get_L()), np.array(r.get_R())])")

        .def(py::init<>(),
             "Construct an empty renderer.  Call prepare() before mix_track().")

        // ── Lifecycle ─────────────────────────────────────────────────────────
        .def("prepare",
             &FullProjectRenderer::prepare,
             py::arg("n_frames"), py::arg("sample_rate"),
             "Allocate the mix bus for n_frames stereo samples at sample_rate Hz.")
        .def("reset",
             &FullProjectRenderer::reset,
             "Zero the mix bus without reallocating (reuse for a second render pass).")

        // ── Mixing ────────────────────────────────────────────────────────────
        .def("mix_track",
             [](FullProjectRenderer& self,
                py::array_t<float, py::array::c_style | py::array::forcecast> L,
                py::array_t<float, py::array::c_style | py::array::forcecast> R,
                int     at_frame,
                double  volume,
                double  pan,
                py::object vol_auto_obj,
                py::object pan_auto_obj)
             {
                 py::buffer_info bl = L.request();
                 py::buffer_info br = R.request();
                 const int n = static_cast<int>(bl.shape[0]);

                 // Resolve optional AutomationProcessor references.
                 // Python passes None when no automation exists for that parameter.
                 const AutomationProcessor* va = vol_auto_obj.is_none()
                     ? nullptr
                     : py::cast<AutomationProcessor*>(vol_auto_obj);
                 const AutomationProcessor* pa = pan_auto_obj.is_none()
                     ? nullptr
                     : py::cast<AutomationProcessor*>(pan_auto_obj);

                 self.mix_track(
                     static_cast<const float*>(bl.ptr),
                     static_cast<const float*>(br.ptr),
                     n, at_frame, volume, pan, va, pa);
             },
             py::arg("L"), py::arg("R"),
             py::arg("at_frame") = 0,
             py::arg("volume")   = 1.0,
             py::arg("pan")      = 0.0,
             py::arg("vol_auto") = py::none(),
             py::arg("pan_auto") = py::none(),
             "Add a stereo track buffer into the mix bus.\n"
             "L, R      : 1-D float32 numpy arrays of equal length.\n"
             "at_frame  : start position in the mix bus.\n"
             "volume    : linear gain 0-2 (overridden by vol_auto if supplied).\n"
             "pan       : stereo pan -1..+1 (overridden by pan_auto if supplied).\n"
             "vol_auto  : AutomationProcessor or None.\n"
             "pan_auto  : AutomationProcessor or None.")

        // ── Output ────────────────────────────────────────────────────────────
        .def("get_L",
             [](const FullProjectRenderer& self) -> py::array_t<float> {
                 const auto& v = self.get_L();
                 return py::array_t<float>(v.size(), v.data());
             },
             "Return the left  channel mix as a float32 numpy array.")
        .def("get_R",
             [](const FullProjectRenderer& self) -> py::array_t<float> {
                 const auto& v = self.get_R();
                 return py::array_t<float>(v.size(), v.data());
             },
             "Return the right channel mix as a float32 numpy array.")
        .def("get_n_frames",
             &FullProjectRenderer::get_n_frames,
             "Total number of frames in the mix bus.");

    // ── AuditionMode ──────────────────────────────────────────────────────────
    // Expose the C++ enum so Python UI code can reference the same constants
    // instead of hard-coding raw integers.
    //   daw_processors.AuditionMode.BYPASS    == 0
    //   daw_processors.AuditionMode.PREVIEW   == 1
    //   daw_processors.AuditionMode.STREAMING == 2
    py::enum_<AuditionMode>(m, "AuditionMode",
        "Audition mode for the real-time MasterBus.\n\n"
        "  BYPASS    -- Normal user FX chain + user-controlled limiter.\n"
        "  PREVIEW   -- Simulate a -7  LUFS commercial master (+7 dB / -1 dBFS).\n"
        "  STREAMING -- Simulate a -14 LUFS streaming master (0 dB / -1 dBFS).")
        .value("BYPASS",    AuditionMode::BYPASS)
        .value("PREVIEW",   AuditionMode::PREVIEW)
        .value("STREAMING", AuditionMode::STREAMING)
        .export_values();

    // ── MasterBus ─────────────────────────────────────────────────────────────
    // Real-time stereo master bus: sums all active audio tracks, routes through
    // the active audition mode, and exposes peak levels for the GUI VU meter.
    py::class_<MasterBus>(m, "MasterBus",
        "Real-time stereo master bus.\n\n"
        "Typical per-block usage:\n"
        "  bus.reset()\n"
        "  bus.add_track(L, R)   # once per active track\n"
        "  bus.process()         # gain + limiter + peak (GIL released)\n"
        "  L_out = bus.get_L()   # final output arrays")
        .def(py::init<float>(),
             py::arg("sample_rate") = 44100.0f,
             "Create a MasterBus for the given sample rate.")
        // Lifecycle
        .def("prepare",
             &MasterBus::prepare,
             py::arg("n_frames"), py::arg("sample_rate"),
             py::call_guard<py::gil_scoped_release>(),
             "Allocate internal buffers and reconfigure the limiter.")
        .def("reset",
             &MasterBus::reset,
             py::call_guard<py::gil_scoped_release>(),
             "Zero the sum buffers. Call once per block before add_track().")
        // Summing
        .def("add_track",
             [](MasterBus& self,
                py::array_t<float, py::array::c_style | py::array::forcecast> L,
                py::array_t<float, py::array::c_style | py::array::forcecast> R)
             {
                 py::gil_scoped_release rel;
                 auto bL = L.request();
                 auto bR = R.request();
                 int n = static_cast<int>(std::min(bL.size, bR.size));
                 self.add_track(
                     static_cast<const float*>(bL.ptr),
                     static_cast<const float*>(bR.ptr), n);
             },
             py::arg("L"), py::arg("R"),
             "Accumulate one stereo float32 track (numpy arrays) into the sum bus.")
        // Processing
        .def("process",
             &MasterBus::process,
             py::call_guard<py::gil_scoped_release>(),
             "Apply master gain + brickwall limiter and update peak meters.\n"
             "The GIL is released for the entire duration of this call.")
        // Output
        .def("get_L",
             [](MasterBus& self) -> py::array_t<float> {
                 py::gil_scoped_release rel;
                 auto v = self.get_L();
                 return py::array_t<float>(v.size(), v.data());
             },
             "Return the processed left-channel output as a float32 numpy array.")
        .def("get_R",
             [](MasterBus& self) -> py::array_t<float> {
                 py::gil_scoped_release rel;
                 auto v = self.get_R();
                 return py::array_t<float>(v.size(), v.data());
             },
             "Return the processed right-channel output as a float32 numpy array.")
        // Peak metering
        .def("peak_L",   &MasterBus::peak_L,
             "Left-channel peak level (0.0–1.0+). Updated by process().")
        .def("peak_R",   &MasterBus::peak_R,
             "Right-channel peak level (0.0–1.0+). Updated by process().")
        // Parameter control
        .def("set_gain", &MasterBus::set_gain,
             py::arg("gain"),
             "Set master gain (0.0 = silence, 1.0 = unity, 2.0 ≈ +6 dB).")
        .def("get_gain", &MasterBus::get_gain,
             "Return the current master gain scalar.")
        .def("set_ceiling", &MasterBus::set_ceiling,
             py::arg("db"),
             "Set the limiter true-peak ceiling in dBFS (e.g. -0.1).")
        .def("get_ceiling", &MasterBus::get_ceiling,
             "Return the limiter ceiling in dBFS.")
        .def("set_limiter_enabled", &MasterBus::set_limiter_enabled,
             py::arg("enabled"),
             "Enable (True) or bypass (False) the brickwall limiter.")
        .def("get_limiter_enabled", &MasterBus::get_limiter_enabled,
             "Return True if the brickwall limiter is active.")
        // Audition mode — thread-safe: safe to call from the GUI thread while
        // the audio thread runs process().  The new mode takes effect within
        // one audio block (typically < 1 ms latency).
        .def("set_audition_mode",
             &MasterBus::set_audition_mode,
             py::arg("mode"),
             "Switch the audition mode instantly (thread-safe).\n"
             "Pass an AuditionMode integer: BYPASS=0, PREVIEW=1, STREAMING=2.")
        .def("get_audition_mode",
             &MasterBus::get_audition_mode,
             "Return the current audition mode as an integer.");

    // ── SFZ instrument engine (SfzParser + SfizzEngine) ──────────────────────
    bind_sfz(m);

    // ── VST3 advanced hosting extensions ─────────────────────────────────────
    // (compiled as stubs when HAVE_VST3_SDK is not defined)
    bind_vst3_extensions(m);

    // ── Decent Sampler engine + VST3 bus manager ─────────────────────────────
    bind_ds(m);
}
