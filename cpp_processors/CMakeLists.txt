cmake_minimum_required(VERSION 3.18)
project(daw_processors)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

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
    # pybind11 module entry point
    src/bindings.cpp
)

# Silence MSVC warnings about using std::min/std::max with nominmax not defined.
if(MSVC)
    target_compile_definitions(daw_processors PRIVATE NOMINMAX)
endif()
