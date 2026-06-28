"""
core/benchmark_store.py

Loads genre acoustic benchmark JSON files and exposes typed targets/tolerances
for the telemetry HUD overlay panels.
"""

from __future__ import annotations

import json
import pathlib
from typing import List, Tuple

_BENCHMARKS_DIR = pathlib.Path(__file__).parent.parent / "benchmarks"

# Order matches the 7-band TelemetryBandPanel._BANDS list.
_BAND_KEYS = ["sub_bass", "bass", "low_mid", "mid", "high_mid", "high", "brilliance"]


class BenchmarkProfile:
    """Parsed benchmark for one genre."""

    def __init__(
        self,
        genre: str,
        freq_targets: List[float],
        freq_tolerances: List[float],
        hp_ratio_target: float,
        hp_ratio_tolerance: float,
    ) -> None:
        self.genre = genre
        self.freq_targets = freq_targets          # 7 values, 0-1 fractions
        self.freq_tolerances = freq_tolerances    # 7 values, 0-1 fractions
        self.hp_ratio_target = hp_ratio_target    # harmonic fraction [0, 1]
        self.hp_ratio_tolerance = hp_ratio_tolerance


def load_benchmark(path: pathlib.Path) -> BenchmarkProfile:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    genre = data.get("genre", path.stem.replace("_benchmark", ""))

    t_bands   = data["targets"]["frequency_bands"]["distribution_percentages"]
    tol_bands = data["tolerances"]["frequency_bands"]["distribution_percentages"]

    freq_targets     = [t_bands.get(k, 0.0) / 100.0   for k in _BAND_KEYS]
    freq_tolerances  = [tol_bands.get(k, 0.0) / 100.0 for k in _BAND_KEYS]

    hp_target    = float(data["targets"]["instruments"]["harmonic_vs_percussive_ratio"])
    hp_tolerance = float(data["tolerances"]["instruments"]["harmonic_vs_percussive_ratio"])

    return BenchmarkProfile(genre, freq_targets, freq_tolerances, hp_target, hp_tolerance)


def scan_benchmarks() -> List[Tuple[str, pathlib.Path]]:
    """Return sorted list of (display_name, path) for all *_benchmark.json files."""
    results: List[Tuple[str, pathlib.Path]] = []
    if not _BENCHMARKS_DIR.exists():
        return results
    for p in sorted(_BENCHMARKS_DIR.glob("*_benchmark.json")):
        name = p.stem.replace("_benchmark", "")
        results.append((name, p))
    return results
