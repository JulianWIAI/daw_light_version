"""
core/telemetry_dashboard.py

TelemetryDashboard — the single polling container that drives all five
telemetry panels at 30 FPS from the C++ TelemetryAnalyzer.

Architecture:
  - One self.after(33, self._update) loop on the Tk main thread.
  - Inside _update: one call to analyzer.get_frame() (GIL released in C++,
    always instant — no DSP on this thread).
  - Each panel receives the frame via update_from_frame().  No canvas.delete()
    is ever called inside panel updates — all drawing uses pre-allocated IDs.
  - Genre matching: a threshold profile is loaded from GENRE_PROFILES when
    the user picks a genre.  Every tick, the live frame metrics are scored
    against the profile; a sustained match (≥ _MATCH_FRAMES consecutive ticks)
    lights the "★ PATTERN MATCH ★" alert.

Metric expressions understood by _get_metric():
    rms          → frame.rms
    harmonic     → frame.harmonic
    percussive   → frame.percussive
    sub_bass     → bands[0]   (20–60 Hz)
    bass_ratio   → bands[0] + bands[1]   (20–250 Hz)
    mid          → bands[3]   (500–2 kHz)
    high_mid     → bands[4]   (2–4 kHz)
    high         → bands[6]   (8–20 kHz)
    chroma_spread→ normalised Shannon entropy of the chroma distribution [0, 1]
                   0 = all energy in one pitch class (very simple)
                   1 = perfectly even across all 12 (very complex / chromatic)
    novelty      → spectral flux × 10: sum of positive band-energy rises
                   per frame, scaled so strong percussive hits reach 15–50+
    repetition   → mean Pearson correlation between consecutive band vectors
                   over the last ~0.5 s window [0, 1]; high → loop-like

Usage:
    dashboard = TelemetryDashboard(root, analyzer=engine.telemetry_analyzer)
    dashboard.show()
    engine.telemetry_analyzer.push(mono_chunk)  # from audio callback
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk

try:
    import daw_processors as _dp
    _HAS_CPP = hasattr(_dp, 'TelemetryAnalyzer')
except ImportError:
    _HAS_CPP = False

from core.telemetry_waveform_panel  import TelemetryWaveformPanel
from core.telemetry_band_panel      import TelemetryBandPanel
from core.telemetry_chroma_panel    import TelemetryChromaPanel
from core.telemetry_waterfall_panel import TelemetryWaterfallPanel
from core.telemetry_hpss_panel      import TelemetryHpssPanel

_BG        = '#0a0a14'
_TITLE_BG  = '#111126'
_TEXT_COL  = '#88aacc'
_ALERT_COL = '#ffd700'
_DIM_COL   = '#445566'
_FPS       = 30
_FRAME_MS  = 1000 // _FPS       # ~33 ms per display tick
_MATCH_FRAMES    = 6             # consecutive ticks required before alert fires
_HISTORY_FRAMES  = 15            # ~0.5 s of band history for repetition/novelty


# ── Genre fingerprint profiles ────────────────────────────────────────────────
#
# Each value is a list of (metric_expr, operator, threshold) tuples.
# All conditions must pass for a PATTERN MATCH to register.
# See the module docstring for the full list of supported metric_expr names.
# ─────────────────────────────────────────────────────────────────────────────

GENRE_PROFILES: Dict[str, List[Tuple[str, str, float]]] = {
    "TRAP": [
        ("percussive", ">", 0.60),       # Fast hi-hats and snares
        ("sub_bass",   ">", 0.30),       # Heavy 808 presence
        ("mid",        "<", 0.25),       # Scooped mids to leave room for the kick
    ],
    "TECHNO": [
        ("rms",         ">", 0.40),      # Loud, compressed sustained energy
        ("bass_ratio",  ">", 0.35),      # Driving low end (kick/bass)
        ("repetition",  ">", 0.80),      # Highly structured and repetitive matrix
    ],
    "PHONK": [
        ("sub_bass",  ">", 0.35),        # Extreme sub-bass
        ("novelty",   ">", 15.0),        # High transient activity (cowbells, saturated hits)
        ("high_mid",  ">", 0.30),        # Distorted/lo-fi brightness
    ],
    "CINEMATIC": [
        ("harmonic",   ">", 0.70),       # Sustained string/brass chords
        ("percussive", "<", 0.30),       # Sparse hits
        ("rms",        "<", 0.25),       # Massive headroom for crescendos
    ],
    "POP": [
        ("harmonic",      ">", 0.50),    # Clear tonal center
        ("rms",           ">", 0.30),    # Consistent commercial loudness
        ("chroma_spread", "<", 0.40),    # Simple, diatonic chord structures
    ],
    "JPOP": [
        ("chroma_spread", ">", 0.55),    # Complex, jazzy chord extensions (7ths/9ths)
        ("high_mid",      ">", 0.35),    # Dense vocal and synth layering
        ("percussive",    ">", 0.40),    # Energetic rhythm section
    ],
    "HIPHOP": [
        ("bass_ratio",    ">", 0.40),    # Boom-bap/punchy low end
        ("percussive",    ">", 0.45),    # Prominent drum groove
        ("chroma_spread", "<", 0.45),    # Loop-based, repetitive harmony
    ],
    "CLASSICAL": [
        ("harmonic",  ">", 0.80),        # Purely tonal/orchestral
        ("sub_bass",  "<", 0.05),        # Absence of electronic sub-frequencies
        ("rms",       "<", 0.20),        # Wide, uncompressed dynamic range
    ],
    "EDM": [
        ("rms",        ">", 0.45),       # Heavily compressed and limited
        ("high",       ">", 0.25),       # White noise sweeps, bright leads
        ("bass_ratio", ">", 0.30),       # Big room kicks
    ],
    "HOUSE": [
        ("bass_ratio",    ">", 0.35),    # 4-on-the-floor groove
        ("chroma_spread", ">", 0.45),    # Deep house jazz chords
        ("percussive",    ">", 0.50),    # Hi-hat heavy rhythmic shuffle
    ],
}

# Display metadata per genre (color, full label for the alert).
_GENRE_META: Dict[str, Dict[str, str]] = {
    "TRAP":      {"color": "#cc44ff", "label": "Trap"},
    "TECHNO":    {"color": "#ff2244", "label": "Techno"},
    "PHONK":     {"color": "#ff6600", "label": "Phonk"},
    "CINEMATIC": {"color": "#4488ff", "label": "Cinematic"},
    "POP":       {"color": "#ff88cc", "label": "Pop"},
    "JPOP":      {"color": "#00ffcc", "label": "J-Pop"},
    "HIPHOP":    {"color": "#aaff44", "label": "Hip-Hop"},
    "CLASSICAL": {"color": "#aaddff", "label": "Classical"},
    "EDM":       {"color": "#44ff88", "label": "EDM"},
    "HOUSE":     {"color": "#ffcc44", "label": "House"},
}

_GENRE_NAMES: List[str] = ["— None —"] + list(GENRE_PROFILES.keys())


# ── Metric computation helpers ────────────────────────────────────────────────

def _chroma_entropy(chroma: list) -> float:
    """
    Normalised Shannon entropy of the chroma probability distribution.

    Returns a value in [0, 1]:
        0 → all energy concentrated in one pitch class (very simple)
        1 → perfectly uniform across all 12 pitch classes (maximally complex)

    Typical ranges:
        Simple pop / single chord  ≈ 0.20–0.38
        Diatonic progressions      ≈ 0.40–0.55
        Jazz extensions (7th/9th)  ≈ 0.55–0.75
    """
    total = sum(chroma)
    if total <= 0.0:
        return 0.0
    entropy = 0.0
    for c in chroma:
        p = c / total
        if p > 0.0:
            entropy -= p * math.log(p)
    return entropy / math.log(len(chroma))   # normalise by log(12)


def _pearson(a: list, b: list) -> float:
    """Pearson correlation coefficient between two equal-length lists."""
    n = len(a)
    if n == 0:
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    num  = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da   = math.sqrt(sum((x - ma) ** 2 for x in a))
    db   = math.sqrt(sum((x - mb) ** 2 for x in b))
    denom = da * db
    return num / denom if denom > 0.0 else 0.0


def _get_metric(frame: Any, expr: str,
                extra: Optional[Dict[str, float]] = None) -> float:
    """
    Resolve a metric expression to a float from a TelemetryFrame-like object.

    ``extra`` carries history-based metrics (novelty, repetition) that cannot
    be derived from a single frame.
    """
    try:
        # History-based metrics are pre-computed by the caller.
        if extra is not None and expr in extra:
            return extra[expr]

        # Band-energy aliases.
        band_aliases = {
            "sub_bass": 0,   # 20–60 Hz
            "high_mid": 4,   # 2–4 kHz
            "high":     6,   # 8–20 kHz
            "mid":      3,   # 500–2 kHz
        }
        if expr in band_aliases:
            return float(frame.bands[band_aliases[expr]])

        # Composite / derived metrics.
        if expr == "bass_ratio":
            return float(frame.bands[0] + frame.bands[1])
        if expr == "brightness":
            return float(frame.bands[5] + frame.bands[6])
        if expr == "chroma_spread":
            return _chroma_entropy(list(frame.chroma))

        # Indexed band access (legacy: "bands[N]").
        if expr.startswith("bands["):
            idx = int(expr[6:-1])
            return float(frame.bands[idx])

        # Direct frame attributes (rms, harmonic, percussive, …).
        return float(getattr(frame, expr, 0.0))
    except Exception:
        return 0.0


def _score_frame(frame: Any, conditions: List[Tuple[str, str, float]],
                 extra: Optional[Dict[str, float]] = None) -> Tuple[int, int]:
    """
    Evaluate each condition in *conditions* against the frame.

    Returns ``(passed, total)`` where total == len(conditions).
    """
    passed = 0
    for expr, op, thresh in conditions:
        value = _get_metric(frame, expr, extra)
        if op == ">" and value > thresh:
            passed += 1
        elif op == "<" and value < thresh:
            passed += 1
    return passed, len(conditions)


# ── Dashboard window ──────────────────────────────────────────────────────────

class TelemetryDashboard(tk.Toplevel):
    """Floating dashboard window containing all five telemetry panels.

    Polls the C++ TelemetryAnalyzer at 30 FPS.  All five panels are refreshed
    from a single get_frame() call — the Python UI thread does zero DSP.

    Genre detection compares live frame metrics against a threshold profile
    and shows a "★ PATTERN MATCH ★" alert when the audio matches for
    _MATCH_FRAMES consecutive ticks (~200 ms), suppressing transient flicker.
    """

    def __init__(self, parent: tk.Misc,
                 analyzer: Optional[object] = None) -> None:
        super().__init__(parent)
        self.title('Telemetry Dashboard')
        self.resizable(False, False)
        self.configure(bg=_BG)
        self.protocol('WM_DELETE_WINDOW', self.hide)

        self._analyzer     = analyzer
        self._running      = False
        self._match_streak = 0

        # Rolling band-vector history for novelty and repetition.
        self._band_history: deque = deque(maxlen=_HISTORY_FRAMES)
        self._prev_bands: List[float] = []

        self._build_ui()

    # ── public API ────────────────────────────────────────────────────────────

    def attach_analyzer(self, analyzer: object) -> None:
        """Attach or replace the C++ analyzer at any time."""
        self._analyzer = analyzer

    def show(self) -> None:
        """Make the window visible and start the polling loop."""
        self.deiconify()
        if not self._running:
            self._running = True
            self.after(_FRAME_MS, self._update)

    def hide(self) -> None:
        """Hide the window and pause the polling loop."""
        self._running = False
        self.withdraw()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Lay out panels in a grid inside the Toplevel."""
        # Title bar.
        tk.Label(self, text='  Telemetry  ', bg=_TITLE_BG,
                 fg=_TEXT_COL, font=('Consolas', 9, 'bold'),
                 anchor='w').grid(row=0, column=0, columnspan=2,
                                  sticky='ew', padx=0, pady=0)

        # Row 1: Waveform (left) + HPSS (right).
        self._waveform_panel = TelemetryWaveformPanel(self)
        self._waveform_panel.grid(row=1, column=0, padx=4, pady=4, sticky='nsew')

        self._hpss_panel = TelemetryHpssPanel(self)
        self._hpss_panel.grid(row=1, column=1, padx=4, pady=4, sticky='nsew')

        # Row 2: Freq bands + Chroma + Waterfall.
        bottom_frame = tk.Frame(self, bg=_BG)
        bottom_frame.grid(row=2, column=0, columnspan=2, padx=4, pady=4)

        self._band_panel = TelemetryBandPanel(bottom_frame)
        self._band_panel.pack(side=tk.LEFT, padx=(0, 6))

        self._chroma_panel = TelemetryChromaPanel(bottom_frame)
        self._chroma_panel.pack(side=tk.LEFT, padx=(0, 6))

        self._waterfall_panel = TelemetryWaterfallPanel(bottom_frame)
        self._waterfall_panel.pack(side=tk.LEFT)

        # Row 3: Genre selector + match indicator.
        genre_frame = tk.Frame(self, bg=_TITLE_BG)
        genre_frame.grid(row=3, column=0, columnspan=2, sticky='ew',
                         padx=0, pady=0)

        tk.Label(genre_frame, text=' Genre target:', bg=_TITLE_BG,
                 fg=_TEXT_COL, font=('Consolas', 8)).pack(side=tk.LEFT, padx=(6, 4))

        self._genre_var = tk.StringVar(value=_GENRE_NAMES[0])
        genre_menu = tk.OptionMenu(genre_frame, self._genre_var, *_GENRE_NAMES,
                                   command=self._on_genre_changed)
        genre_menu.config(
            bg=_BG, fg=_TEXT_COL, activebackground='#1a1a40',
            activeforeground=_ALERT_COL, highlightthickness=0,
            font=('Consolas', 8), relief='flat', bd=0,
        )
        genre_menu['menu'].config(
            bg=_BG, fg=_TEXT_COL, activebackground='#1a1a40',
            activeforeground=_ALERT_COL, font=('Consolas', 8),
        )
        genre_menu.pack(side=tk.LEFT, padx=(0, 10))

        self._match_var   = tk.StringVar(value='')
        self._match_label = tk.Label(genre_frame, textvariable=self._match_var,
                                     bg=_TITLE_BG, fg=_DIM_COL,
                                     font=('Consolas', 8, 'bold'))
        self._match_label.pack(side=tk.LEFT, padx=(0, 6))

        # Row 4: Status bar.
        self._status_var = tk.StringVar(value='No analyzer attached.')
        tk.Label(self, textvariable=self._status_var,
                 bg=_TITLE_BG, fg=_TEXT_COL,
                 font=('Consolas', 8), anchor='w').grid(
            row=4, column=0, columnspan=2, sticky='ew')

    # ── Genre control ─────────────────────────────────────────────────────────

    def _on_genre_changed(self, _value: str = '') -> None:
        """Reset state when the user picks a different genre."""
        self._match_streak = 0
        self._band_history.clear()
        self._prev_bands = []
        genre = self._genre_var.get()
        if genre == '— None —':
            self._match_var.set('')
            self._match_label.config(fg=_DIM_COL)
        else:
            self._match_var.set(f'[ {genre} ]  listening…')
            self._match_label.config(fg=_DIM_COL)

    def _compute_extra(self, bands: List[float]) -> Dict[str, float]:
        """
        Compute history-based metrics from the current and past band vectors.

        novelty    — spectral flux ×10: sum of positive energy rises across
                     all 7 bands since the previous frame, scaled so that
                     strong percussive hits (3–4 bands jumping 0.5) reach ≈15–20.
        repetition — mean Pearson correlation between consecutive band vectors
                     over the history window.  Values near 1.0 → loop-like.
        """
        # Novelty: positive-only spectral flux.
        if self._prev_bands and len(self._prev_bands) == len(bands):
            flux = sum(max(0.0, bands[i] - self._prev_bands[i])
                       for i in range(len(bands)))
            novelty = flux * 10.0
        else:
            novelty = 0.0

        # Repetition: mean inter-frame Pearson correlation over history window.
        hist = list(self._band_history)
        if len(hist) >= 2:
            correlations = []
            for i in range(1, len(hist)):
                r = _pearson(hist[i - 1], hist[i])
                correlations.append(max(0.0, r))
            repetition = sum(correlations) / len(correlations)
        else:
            repetition = 0.0

        return {"novelty": novelty, "repetition": repetition}

    def _evaluate_genre(self, frame: Any) -> None:
        """
        Score the current frame against the selected genre profile.

        Requires _MATCH_FRAMES consecutive matches before showing the alert,
        preventing transient noise from triggering false positives.
        """
        genre = self._genre_var.get()
        if genre == '— None —':
            return

        conditions = GENRE_PROFILES.get(genre)
        if not conditions:
            return

        bands = list(frame.bands)
        extra = self._compute_extra(bands)

        # Update rolling history AFTER computing this tick's extra metrics
        # so the previous frame is always one tick behind.
        self._band_history.append(bands)
        self._prev_bands = bands

        passed, total = _score_frame(frame, conditions, extra)
        all_passed = (passed >= total)

        if all_passed:
            self._match_streak += 1
        else:
            self._match_streak = 0

        meta  = _GENRE_META.get(genre, {})
        color = meta.get('color', _ALERT_COL)
        label = meta.get('label', genre)

        if self._match_streak >= _MATCH_FRAMES:
            self._match_var.set(f'★ PATTERN MATCH: {label} ★')
            self._match_label.config(fg=color)
        else:
            score_pct = int(100 * passed / max(total, 1))
            self._match_var.set(f'[ {genre} ]  {score_pct}% match')
            self._match_label.config(fg=color if score_pct >= 67 else _DIM_COL)

    # ── 30-FPS polling loop ───────────────────────────────────────────────────

    def _update(self) -> None:
        """Single polling tick: query analyzer, delegate to panels, reschedule."""
        if not self._running:
            return

        if self._analyzer is not None:
            frame = self._analyzer.get_frame()

            self._waveform_panel.update_from_frame(frame)
            self._hpss_panel.update_from_frame(frame)
            self._band_panel.update_from_frame(frame)
            self._chroma_panel.update_from_frame(frame)
            self._waterfall_panel.update_from_frame(frame)

            self._evaluate_genre(frame)

            tick = getattr(frame, 'tick', '—')
            self._status_var.set(
                f'tick #{tick}  |  RMS {frame.rms:.3f}  '
                f'|  H {frame.harmonic:.0%}  P {frame.percussive:.0%}')
        else:
            self._status_var.set('No analyzer attached — call attach_analyzer().')

        self.after(_FRAME_MS, self._update)
