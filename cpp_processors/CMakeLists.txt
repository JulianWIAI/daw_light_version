cmake_minimum_required(VERSION 3.18)
project(daw_processors)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ── sfizz SFZ instrument engine ───────────────────────────────────────────────
# sfizz is fetched from GitHub at configure time.  Set SFIZZ_GIT_TAG to pin a
# specific release, or set SFIZZ_ROOT to use a locally installed copy instead.
set(SFIZZ_GIT_TAG "1.2.3" CACHE STRING "sfizz git tag to fetch")
set(SFIZZ_ROOT    ""      CACHE PATH   "Path to a pre-installed sfizz (skips FetchContent)")

if(SFIZZ_ROOT AND EXISTS "${SFIZZ_ROOT}")
    # Use a local installation: assumes sfizz-config.cmake is present.
    find_package(sfizz REQUIRED HINTS "${SFIZZ_ROOT}")
    message(STATUS "sfizz: using local installation at ${SFIZZ_ROOT}")
else()
    include(FetchContent)
    FetchContent_Declare(
        sfizz
        GIT_REPOSITORY https://github.com/sfztools/sfizz.git
        GIT_TAG        ${SFIZZ_GIT_TAG}
        GIT_SHALLOW    TRUE
    )
    # Disable all sfizz build targets except the core library.
    set(SFIZZ_JACK        OFF CACHE BOOL "" FORCE)
    set(SFIZZ_RENDER      OFF CACHE BOOL "" FORCE)
    set(SFIZZ_BENCHMARKS  OFF CACHE BOOL "" FORCE)
    set(SFIZZ_TESTS       OFF CACHE BOOL "" FORCE)
    set(SFIZZ_DEVTOOLS    OFF CACHE BOOL "" FORCE)
    set(SFIZZ_LV2_PLUGIN  OFF CACHE BOOL "" FORCE)
    set(SFIZZ_VST_PLUGIN  OFF CACHE BOOL "" FORCE)
    set(SFIZZ_AU_PLUGIN   OFF CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(sfizz)
    message(STATUS "sfizz: fetched from GitHub (tag ${SFIZZ_GIT_TAG})")
endif()

# ── Optional VST3 SDK (for advanced hosting: state, automation, transport) ────
# Set VST3_SDK_ROOT to the root of your VST3 SDK checkout, e.g.:
#   cmake -B build -DVST3_SDK_ROOT=/path/to/vst3sdk
# If not set, VST3 hosting extensions are compiled as no-op stubs.
set(VST3_SDK_ROOT "" CACHE PATH "Path to the VST3 SDK root (optional)")

if(VST3_SDK_ROOT AND EXISTS "${VST3_SDK_ROOT}/pluginterfaces")
    message(STATUS "VST3 SDK found at: ${VST3_SDK_ROOT} — hosting extensions ENABLED")
    set(HAVE_VST3_SDK ON)
else()
    message(STATUS "VST3 SDK not configured (VST3_SDK_ROOT not set) — hosting extensions DISABLED")
    set(HAVE_VST3_SDK OFF)
endif()

# Release build flags for maximum performance.
# /fp:fast on MSVC fuses multiply-add, reorders floating-point ops and
# enables flush-to-zero mode — together these give ~20-30% speedup on DSP code.
# -ffast-math on GCC/Clang does the same; -march=native enables SIMD auto-vectorisation.
if(MSVC)
    add_compile_options(/O2 /arch:AVX2 /fp:fast)
else()
    add_compile_options(-O3 -march=native -ffast-math)
endif()

# Auto-detect pybind11 cmake directory from the active Python interpreter so
# the build works without manually setting pybind11_DIR on the command line.
if(NOT DEFINED pybind11_DIR)
    execute_process(
        COMMAND python -c "import pybind11; print(pybind11.get_cmake_dir())"
        OUTPUT_VARIABLE PYBIND11_CMAKE_DIR
        OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE PYBIND11_QUERY_RESULT
    )
    if(PYBIND11_QUERY_RESULT EQUAL 0)
        set(pybind11_DIR "${PYBIND11_CMAKE_DIR}")
        message(STATUS "pybind11 cmake dir (auto-detected): ${pybind11_DIR}")
    else()
        message(FATAL_ERROR
            "Could not auto-detect pybind11. Run:\n"
            "  pip install pybind11\n"
            "or pass -Dpybind11_DIR=<path> to cmake.")
    endif()
endif()

find_package(pybind11 REQUIRED)

include_directories(include)

pybind11_add_module(daw_processors
    # Dynamics processors
    src/BrickwallLimiter.cpp
    src/MultibandCompressor.cpp
    src/DynamicEQ.cpp
    src/DeEsser.cpp
    src/TransientShaper.cpp
    src/GateExpander.cpp
    # Spatial / time-based effects
    src/DelayEcho.cpp
    src/Flanger.cpp
    src/Phaser.cpp
    src/StereoImager.cpp
    # Harmonic & character processors
    src/Saturation.cpp
    src/Overdrive.cpp
    src/Bitcrusher.cpp
    src/Exciter.cpp
    # Advanced utilities & specialty filters
    src/PitchCorrector.cpp
    src/PitchShifter.cpp
    src/AutoFilter.cpp
    # Sampler instrument engine
    src/Sampler.cpp
    # Offline export mix bus + WAV writer
    src/OfflineExporter.cpp
    # Real-time timeline / transport engine
    src/TimelineEngine.cpp
    # Waveform peak generator
    src/WaveformGenerator.cpp
    # Gaussian velocity humanizer
    src/GaussianRng.cpp
    src/TimingWeightFunction.cpp
    src/VelocityHumanizer.cpp
    # Real-time loudness automation
    src/RmsAnalyzer.cpp
    src/EnvelopeFollower.cpp
    src/PidController.cpp
    src/LoudnessAutomation.cpp
    # Spectral panning & masking
    src/SpectralAnalyzer.cpp
    src/SpectralMaskingManager.cpp
    src/SpectralPanningProcessor.cpp
    # Grid snap system
    src/GridDefinition.cpp
    src/QuantizeEngine.cpp
    src/TimelineRuler.cpp
    src/GridSnapper.cpp
    # Precision audio-loop boundary scheduler
    src/AudioLoopScheduler.cpp
    # Offline mastering: stereo mix bus (AutomationProcessor is header-only)
    src/FullProjectRenderer.cpp
    # Audition mode loudness processor (PREVIEW / STREAMING simulation)
    src/AuditionProcessor.cpp
    # Real-time master bus: track summing, audition routing, gain, peak metering
    src/MasterBus.cpp
    # SFZ v1/v2 instrument metadata parser (no audio dependency)
    src/SfzParser.cpp
    # sfizz-backed SFZ audio engine (pImpl isolates sfizz headers)
    src/SfizzEngine.cpp
    # VST3 advanced hosting: state, automation, transport (stubs if no SDK)
    src/Vst3StateManager.cpp
    src/Vst3AutomationQueue.cpp
    src/Vst3TransportContext.cpp
    # Decent Sampler (.dspreset) polyphonic sampler engine
    src/DecentSamplerEngine.cpp
    # VST3 multi-bus topology manager (stub if no SDK)
    src/Vst3BusManager.cpp
    # Async multi-track MIDI drag-and-drop import
    src/MidiDropImporter.cpp
    src/TimelineEngine_MidiDrop.cpp
    # Offline mastering flavor processor (tanh saturation, biquad EQ, VCA compressor)
    src/MasteringFlavorProcessor.cpp
    # Real-time audio telemetry analyzer (RMS, FFT, chroma, HPSS)
    src/TelemetryAnalyzer.cpp
    # Split pybind11 binding modules (keep bindings.cpp manageable)
    src/bindings_sfz.cpp
    src/bindings_vst3.cpp
    src/bindings_ds.cpp
    src/bindings_midi_drop.cpp
    src/bindings_flavor.cpp
    src/bindings_telemetry.cpp
    # pybind11 module entry point (calls bind_sfz / bind_vst3_extensions / bind_ds / bind_midi_drop / bind_telemetry)
    src/bindings.cpp
)

# Silence MSVC warnings about using std::min/std::max with nominmax not defined.
if(MSVC)
    target_compile_definitions(daw_processors PRIVATE NOMINMAX)
endif()

# ── Link sfizz ────────────────────────────────────────────────────────────────
target_link_libraries(daw_processors PRIVATE sfizz::sfizz)

# ── VST3 SDK include path + compile definition ────────────────────────────────
if(HAVE_VST3_SDK)
    target_include_directories(daw_processors PRIVATE ${VST3_SDK_ROOT})
    target_compile_definitions(daw_processors PRIVATE HAVE_VST3_SDK)
endif()
