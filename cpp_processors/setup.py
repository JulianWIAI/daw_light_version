"""
Build script for daw_processors C++ extension.

Usage:
    pip install pybind11 numpy
    python setup.py build_ext --inplace

Or with pip (preferred):
    pip install .

Compatible with pybind11 2.x and 3.x — uses get_include() directly
instead of the setup_helpers module that was removed in pybind11 3.0.
"""

import sys
from pathlib import Path

try:
    import pybind11
except ImportError:
    raise SystemExit(
        "pybind11 is required to build this extension. "
        "Install it with: pip install pybind11"
    )

from setuptools import Extension, setup

BASE = Path(__file__).parent

# All C++ source files that make up the module.
SOURCES = [
    # Dynamics processors (original 6)
    str(BASE / "src" / "BrickwallLimiter.cpp"),
    str(BASE / "src" / "MultibandCompressor.cpp"),
    str(BASE / "src" / "DynamicEQ.cpp"),
    str(BASE / "src" / "DeEsser.cpp"),
    str(BASE / "src" / "TransientShaper.cpp"),
    str(BASE / "src" / "GateExpander.cpp"),
    # Spatial / time-based effects (4)
    str(BASE / "src" / "DelayEcho.cpp"),
    str(BASE / "src" / "Flanger.cpp"),
    str(BASE / "src" / "Phaser.cpp"),
    str(BASE / "src" / "StereoImager.cpp"),
    # Harmonic & character processors (4)
    str(BASE / "src" / "Saturation.cpp"),
    str(BASE / "src" / "Overdrive.cpp"),
    str(BASE / "src" / "Bitcrusher.cpp"),
    str(BASE / "src" / "Exciter.cpp"),
    # Advanced utilities & specialty filters (3)
    str(BASE / "src" / "PitchCorrector.cpp"),
    str(BASE / "src" / "PitchShifter.cpp"),
    str(BASE / "src" / "AutoFilter.cpp"),
    # Sampler instrument engine
    str(BASE / "src" / "Sampler.cpp"),
    # Offline export mix bus + WAV writer
    str(BASE / "src" / "OfflineExporter.cpp"),
    # Real-time timeline / transport engine
    str(BASE / "src" / "TimelineEngine.cpp"),
    # pybind11 module entry point
    str(BASE / "src" / "bindings.cpp"),
]

# Compiler-specific optimisation flags.
if sys.platform == "win32":
    # MSVC — /std:c++17 sets the language standard on MSVC.
    extra_args = [
        "/std:c++17",
        "/O2",          # maximise speed
        "/arch:AVX2",   # enable AVX2 SIMD (remove if CPU does not support it)
        "/fp:fast",     # aggressive floating-point + flush-to-zero
        "/DNOMINMAX",   # prevent Windows.h macros clobbering std::min/max
    ]
    extra_link_args = []
else:
    # GCC / Clang
    extra_args = [
        "-O3",
        "-march=native",
        "-ffast-math",
        "-std=c++17",
    ]
    extra_link_args = []

ext = Extension(
    name="daw_processors",
    sources=SOURCES,
    # pybind11.get_include() returns the path to pybind11's headers,
    # which works with every pybind11 version including 3.x.
    include_dirs=[
        str(BASE / "include"),
        pybind11.get_include(),
    ],
    extra_compile_args=extra_args,
    extra_link_args=extra_link_args,
    language="c++",
)

setup(
    name="daw_processors",
    version="1.0.0",
    description="Real-time C++ dynamics processors for a Python DAW",
    ext_modules=[ext],
    python_requires=">=3.8",
    install_requires=["pybind11>=2.10", "numpy"],
    zip_safe=False,
)
