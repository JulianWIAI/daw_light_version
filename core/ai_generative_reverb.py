"""
ai_generative_reverb.py -- Generative Reverb via Text-to-IR Semantic Matching
==============================================================================
The user types a natural-language description of a space ("church cathedral",
"tight vocal booth", "large concert hall") and the plugin finds the best-matching
Impulse Response (IR) .wav file from a local folder using sentence-transformers.

Once an IR is loaded, the plugin applies it as a convolution reverb on every
audio block using scipy.signal.fftconvolve (which internally calls numpy's FFT
routines — compiled C/Fortran — so the convolution is performed in native code).

Overlap-Add convolution (OLA):
    Because the DAW processes audio in fixed-size blocks (typically 512 samples)
    and an IR can be many thousands of samples long, a naive per-block convolution
    would produce discontinuities at block boundaries.  The plugin maintains a
    "tail" buffer (_tail_l, _tail_r) that carries the OLA residual from each
    block into the next, producing perfectly seamless convolution reverb.

Architecture (DAW thread safety):
    - IRSearchWorker (QThread) runs sentence-transformers off the audio thread.
      It emits ir_found(str) with the best-matching IR path, or ir_not_found(str).

    - The main thread loads the IR into _ir_l / _ir_r numpy arrays on receipt
      of ir_found.  A threading.Lock prevents the audio thread from reading the
      IR while it is being swapped.  Because the IR swap completes atomically
      (Python assignment is GIL-protected), the lock is a belt-and-suspenders
      measure for future safety.

    - process() is called by the audio render thread.  It takes the IR ref
      under the lock and runs the scipy OLA step — no GUI or Qt code executes
      on the audio thread.

Dependencies (all optional):
    pip install sentence-transformers soundfile scipy
    (sentence-transformers downloads a ~80 MB model on first use.)
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional, List

import numpy as np

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QGroupBox, QFileDialog,
)

from .fx_plugin_base import FxPluginBase

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Optional dependency guards
# ─────────────────────────────────────────────────────────────────────────────

try:
    from sentence_transformers import SentenceTransformer
    _ST_OK = True
except ImportError:
    _ST_OK = False
    logger.warning("sentence-transformers not found. pip install sentence-transformers")

try:
    import soundfile as sf
    _SF_OK = True
except ImportError:
    _SF_OK = False
    logger.warning("soundfile not found. pip install soundfile")

try:
    from scipy.signal import fftconvolve
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False
    logger.warning("scipy not found. pip install scipy")

# ─────────────────────────────────────────────────────────────────────────────
# Default IR folder — user can override via the UI
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_IR_FOLDER = str(Path(__file__).parent.parent / "impulse_responses")

# Sentence-transformer model: small (80 MB), fast, high quality.
_EMBED_MODEL = "all-MiniLM-L6-v2"

# Maximum IR length in samples.  IRs longer than this are truncated to keep
# the OLA cost manageable.  4 seconds at 44100 Hz = 176 400 samples.
_MAX_IR_SAMPLES = 176_400


# ─────────────────────────────────────────────────────────────────────────────
# Background IR search worker (QThread)
# ─────────────────────────────────────────────────────────────────────────────

class IRSearchWorker(QThread):
    """
    Embeds the user's text prompt and all IR filenames, then picks the IR whose
    filename embedding has the highest cosine similarity to the prompt.

    Emits:
        ir_found(str, str)      -- (ir_file_path, matched_name)
        ir_not_found(str)       -- error / empty-folder message
    """

    ir_found     = Signal(str, str)  # (path, display_name)
    ir_not_found = Signal(str)

    def __init__(self, prompt: str, ir_folder: str) -> None:
        super().__init__()
        self._prompt    = prompt
        self._ir_folder = ir_folder

    def run(self) -> None:
        """Runs entirely in the background QThread — never on the GUI thread."""
        if not _ST_OK:
            self.ir_not_found.emit(
                "sentence-transformers not installed.\n"
                "pip install sentence-transformers"
            )
            return

        try:
            best_path, best_name = self._search()
            if best_path:
                self.ir_found.emit(best_path, best_name)
            else:
                self.ir_not_found.emit("No IR files found in the selected folder.")
        except Exception as exc:
            logger.exception("IRSearchWorker failed")
            self.ir_not_found.emit(str(exc))

    def _search(self):
        folder = Path(self._ir_folder)
        ir_files: List[Path] = []
        for ext in ("*.wav", "*.WAV", "*.aiff", "*.AIFF"):
            ir_files.extend(folder.glob(ext))

        if not ir_files:
            return None, None

        # Build human-readable descriptions from file stems.
        # "church_large_reverb" → "church large reverb"
        descriptions = [
            p.stem.replace("_", " ").replace("-", " ") for p in ir_files
        ]

        # Load the embedding model once (cached by sentence-transformers).
        model = SentenceTransformer(_EMBED_MODEL)

        # Embed all IR descriptions + the prompt in one batch for efficiency.
        all_texts    = [self._prompt] + descriptions
        embeddings   = model.encode(all_texts, convert_to_numpy=True,
                                    normalize_embeddings=True)
        prompt_emb   = embeddings[0]       # shape (D,)
        ir_embs      = embeddings[1:]      # shape (N, D)

        # Cosine similarity = dot product when embeddings are unit-normalised.
        scores    = ir_embs @ prompt_emb   # shape (N,)
        best_idx  = int(np.argmax(scores))
        best_path = str(ir_files[best_idx])
        best_name = descriptions[best_idx]
        return best_path, best_name


# ─────────────────────────────────────────────────────────────────────────────
# FX Rack Plugin
# ─────────────────────────────────────────────────────────────────────────────

_C = {
    "abyss": "#060A18", "deep": "#0A0E22",
    "cyan": "#00E5FF", "purple": "#9945FF",
    "text": "#C8E6FF", "text_dim": "#3D5A80",
    "gold": "#FFD700",
}


class GenerativeReverbPlugin(FxPluginBase):
    """
    Text-to-IR semantic matching convolution reverb insert-slot plugin.

    The user types a space description; the plugin finds the closest IR in a
    local folder and applies it as a high-quality overlap-add convolution reverb.

    Audio thread: process() performs OLA convolution with scipy.signal.fftconvolve.
    GUI thread:   IRSearchWorker QThread searches for the IR and loads it.
    """

    DISPLAY_NAME = "Generative Reverb"

    def __init__(self) -> None:
        super().__init__()
        self._ir_folder = _DEFAULT_IR_FOLDER
        self._wet:  float = 0.5
        self._dry:  float = 0.5

        # IR buffers (mono or stereo).  Swapped atomically under _ir_lock.
        self._ir_l: Optional[np.ndarray] = None
        self._ir_r: Optional[np.ndarray] = None
        self._ir_lock = threading.Lock()

        # OLA tail buffers — carry leftover energy from block to block.
        self._tail_l: np.ndarray = np.zeros(0, dtype=np.float32)
        self._tail_r: np.ndarray = np.zeros(0, dtype=np.float32)
        self._tail_lock = threading.Lock()

        self._worker:   Optional[IRSearchWorker] = None
        self._matched_name: str = ""

        # UI references
        self._status_lbl: Optional[QLabel] = None
        self._ir_name_lbl: Optional[QLabel] = None

    # ── FxPluginBase interface ────────────────────────────────────────────────

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """
        Apply overlap-add convolution reverb to the current audio block.
        Falls back to pass-through if no IR is loaded.
        """
        if not _SCIPY_OK:
            return audio

        # Acquire a snapshot of the current IR under the lock.
        with self._ir_lock:
            ir_l = self._ir_l
            ir_r = self._ir_r

        if ir_l is None:
            return audio

        # Ensure stereo layout.
        if audio.ndim == 1:
            audio = np.column_stack([audio, audio])
        elif audio.ndim == 2 and audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)

        dry_l = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
        dry_r = np.ascontiguousarray(audio[:, 1], dtype=np.float32)

        n = len(dry_l)

        # ── Overlap-Add per channel ───────────────────────────────────────────
        # fftconvolve returns length (n + len(ir) - 1).
        # The first n samples are the output; the remainder is the OLA tail.

        with self._tail_lock:
            wet_l = self._ola_step(dry_l, ir_l, self._tail_l)
            wet_r = self._ola_step(dry_r, ir_r, self._tail_r)

            # Trim output to block length.
            out_l = wet_l[:n]
            out_r = wet_r[:n]

            # Store tails (may be longer than n; carried to next block).
            new_tail_len = len(wet_l) - n
            self._tail_l = wet_l[n:] if new_tail_len > 0 else np.zeros(0, np.float32)
            self._tail_r = wet_r[n:] if new_tail_len > 0 else np.zeros(0, np.float32)

        # Mix wet + dry.
        out_l = self._dry * dry_l + self._wet * out_l
        out_r = self._dry * dry_r + self._wet * out_r

        return np.column_stack([out_l, out_r])

    @staticmethod
    def _ola_step(
        signal: np.ndarray,
        ir: np.ndarray,
        tail: np.ndarray,
    ) -> np.ndarray:
        """
        One overlap-add step: convolve signal with ir, then add the incoming tail.
        Returns the combined output (length = len(signal) + len(ir) - 1).
        """
        # Full linear convolution for this block.
        conv_out = fftconvolve(signal, ir, mode="full").astype(np.float32)

        # Add the tail from the previous block.
        tail_len = len(tail)
        out_len  = len(conv_out)
        if tail_len > 0:
            add_len = min(tail_len, out_len)
            conv_out[:add_len] += tail[:add_len]
            # Remaining tail (if ir is longer than block): prepend to next tail
            if tail_len > out_len:
                remaining = tail[out_len:]
                extra = np.zeros(len(remaining), dtype=np.float32)
                conv_out = np.concatenate([conv_out, extra])
                conv_out[out_len:out_len + len(remaining)] += remaining

        return conv_out

    # ── UI ────────────────────────────────────────────────────────────────────

    def create_parameter_widget(self) -> QWidget:
        def _grp(title: str) -> QGroupBox:
            g = QGroupBox(title)
            g.setStyleSheet(
                f"QGroupBox {{ border:1px solid rgba(153,69,255,0.3);"
                f" border-radius:6px; margin-top:10px; padding-top:6px;"
                f" color:{_C['text_dim']}; font-size:9px; background:{_C['abyss']}; }}"
                f"QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 5px;"
                f" color:rgba(153,69,255,0.9); }}"
            )
            return g

        def _btn(text: str, color: str) -> QPushButton:
            b = QPushButton(text)
            b.setFixedHeight(28)
            b.setStyleSheet(
                f"QPushButton {{ background:{_C['deep']};"
                f" border:1px solid {color}; border-radius:5px;"
                f" color:{color}; font-size:10px; font-weight:bold; }}"
                f"QPushButton:hover {{ background:rgba(0,229,255,0.06); }}"
                f"QPushButton:disabled {{ color:{_C['text_dim']};"
                f" border-color:{_C['text_dim']}; }}"
            )
            return b

        def _slider_row(parent, label: str, lo: int, hi: int,
                        init: int, scale: float, dec: int, attr: str):
            from PySide6.QtWidgets import QSlider, QWidget, QHBoxLayout
            ctr = QWidget(parent)
            row = QHBoxLayout(ctr)
            row.setContentsMargins(0, 2, 0, 2)
            row.setSpacing(6)
            lbl = QLabel(label)
            lbl.setFixedWidth(72)
            lbl.setStyleSheet(
                f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
            )
            sl = QSlider(Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(init)
            sl.setStyleSheet(
                "QSlider::groove:horizontal { height:4px;"
                " background:rgba(0,229,255,0.12); border-radius:2px; }"
                "QSlider::handle:horizontal { width:12px; height:12px; margin:-4px 0;"
                " background:#00E5FF; border-radius:6px; }"
                "QSlider::sub-page:horizontal { background:rgba(0,229,255,0.35);"
                " border-radius:2px; }"
            )
            val_lbl = QLabel(f"{init/scale:.{dec}f}")
            val_lbl.setFixedWidth(40)
            val_lbl.setAlignment(Qt.AlignRight)
            val_lbl.setStyleSheet(
                f"color:{_C['cyan']}; font-size:9px; background:transparent;"
            )
            def _cb(v, a=attr, s=scale, l=val_lbl, d=dec):
                setattr(self, f"_{a}", v / s)
                l.setText(f"{v/s:.{d}f}")
                self._notify()
            sl.valueChanged.connect(_cb)
            row.addWidget(lbl); row.addWidget(sl); row.addWidget(val_lbl)
            return ctr

        root = QWidget()
        root.setStyleSheet(f"background:{_C['abyss']};")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # ── IR folder ─────────────────────────────────────────────────────────
        folder_grp = _grp("IR FOLDER")
        folder_lay = QVBoxLayout(folder_grp)

        self._folder_lbl = QLabel(
            Path(self._ir_folder).name
            if Path(self._ir_folder).exists()
            else "Folder not found — click Browse"
        )
        self._folder_lbl.setStyleSheet(
            f"color:{_C['text']}; font-size:9px; background:transparent;"
        )
        folder_lay.addWidget(self._folder_lbl)

        browse_btn = _btn("Browse IR Folder…", _C["text_dim"])
        browse_btn.clicked.connect(self._browse_ir_folder)
        folder_lay.addWidget(browse_btn)
        lay.addWidget(folder_grp)

        # ── Text prompt ───────────────────────────────────────────────────────
        prompt_grp = _grp("SPACE DESCRIPTION")
        prompt_lay = QVBoxLayout(prompt_grp)

        if not _ST_OK:
            warn = QLabel(
                "⚠  sentence-transformers not installed.\n"
                "pip install sentence-transformers"
            )
            warn.setStyleSheet(
                f"color:{_C['gold']}; font-size:9px; background:transparent;"
            )
            prompt_lay.addWidget(warn)

        self._prompt_edit = QLineEdit()
        self._prompt_edit.setPlaceholderText(
            'e.g. "church cathedral" / "tight vocal booth" / "spring reverb"'
        )
        self._prompt_edit.setStyleSheet(
            f"background:{_C['deep']}; color:{_C['text']};"
            f" border:1px solid rgba(0,229,255,0.25); border-radius:4px;"
            f" padding:4px; font-size:10px;"
        )
        self._prompt_edit.returnPressed.connect(self._start_search)
        prompt_lay.addWidget(self._prompt_edit)

        find_btn = _btn("⚡  FIND MATCHING IR", _C["purple"])
        find_btn.setEnabled(_ST_OK)
        find_btn.clicked.connect(self._start_search)
        prompt_lay.addWidget(find_btn)

        self._status_lbl = QLabel("Enter a description and click Find.")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        prompt_lay.addWidget(self._status_lbl)
        lay.addWidget(prompt_grp)

        # ── Current IR display ────────────────────────────────────────────────
        ir_grp = _grp("LOADED IR")
        ir_lay = QVBoxLayout(ir_grp)

        self._ir_name_lbl = QLabel("None")
        self._ir_name_lbl.setStyleSheet(
            f"color:{_C['cyan']}; font-size:10px; font-weight:bold;"
            f" background:transparent;"
        )
        ir_lay.addWidget(self._ir_name_lbl)

        # Manual browse button — skip AI search and load directly.
        manual_btn = _btn("Load IR Manually…", _C["text_dim"])
        manual_btn.clicked.connect(self._browse_ir_file)
        ir_lay.addWidget(manual_btn)
        lay.addWidget(ir_grp)

        # ── Mix controls ──────────────────────────────────────────────────────
        mix_grp = _grp("MIX")
        mix_lay = QVBoxLayout(mix_grp)
        mix_lay.addWidget(
            _slider_row(mix_grp, "Wet", 0, 100, int(self._wet * 100), 100.0, 2, "wet")
        )
        lay.addWidget(mix_grp)

        lay.addStretch()
        return root

    # ── IR folder browser ─────────────────────────────────────────────────────

    def _browse_ir_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            None, "Select IR Folder", self._ir_folder
        )
        if folder:
            self._ir_folder = folder
            if hasattr(self, "_folder_lbl"):
                self._folder_lbl.setText(Path(folder).name)

    def _browse_ir_file(self) -> None:
        """Directly load an IR file without AI matching."""
        path, _ = QFileDialog.getOpenFileName(
            None, "Load Impulse Response",
            self._ir_folder,
            "Audio Files (*.wav *.aiff *.WAV);;All Files (*)"
        )
        if path:
            self._load_ir(path)

    # ── Background search ─────────────────────────────────────────────────────

    def _start_search(self) -> None:
        prompt = self._prompt_edit.text().strip()
        if not prompt:
            if self._status_lbl:
                self._status_lbl.setText("Type a description first.")
            return

        if not Path(self._ir_folder).is_dir():
            if self._status_lbl:
                self._status_lbl.setText(
                    f"IR folder not found:\n{self._ir_folder}\nClick Browse to set it."
                )
            return

        if self._status_lbl:
            self._status_lbl.setText("Searching… (semantic matching in background)")

        self._worker = IRSearchWorker(prompt, self._ir_folder)
        # Signals cross thread boundary safely via Qt's queued connection.
        self._worker.ir_found    .connect(self._on_ir_found)
        self._worker.ir_not_found.connect(self._on_ir_not_found)
        self._worker.start()

    def _on_ir_found(self, path: str, name: str) -> None:
        """Called on the GUI thread when the worker finds the best IR."""
        if self._status_lbl:
            self._status_lbl.setText(f'Best match: "{name}"')
        self._load_ir(path)

    def _on_ir_not_found(self, msg: str) -> None:
        if self._status_lbl:
            self._status_lbl.setText(f"Not found: {msg}")

    # ── IR loading ────────────────────────────────────────────────────────────

    def _load_ir(self, path: str) -> None:
        """Load an IR file into the plugin. Called from the GUI thread."""
        if not _SF_OK:
            if self._status_lbl:
                self._status_lbl.setText(
                    "soundfile not installed. pip install soundfile"
                )
            return

        try:
            data, sr_ir = sf.read(path, dtype="float32", always_2d=True)
        except Exception as exc:
            if self._status_lbl:
                self._status_lbl.setText(f"Failed to load IR: {exc}")
            return

        # Normalise and truncate to MAX_IR_SAMPLES.
        peak = np.abs(data).max()
        if peak > 0:
            data = data / peak

        if len(data) > _MAX_IR_SAMPLES:
            data = data[:_MAX_IR_SAMPLES]

        ir_l = data[:, 0]
        ir_r = data[:, 1] if data.shape[1] > 1 else data[:, 0]

        # Swap the IR atomically under the lock.
        with self._ir_lock:
            self._ir_l = ir_l
            self._ir_r = ir_r

        # Reset OLA tails so the old reverb tail doesn't bleed into the new IR.
        with self._tail_lock:
            self._tail_l = np.zeros(0, dtype=np.float32)
            self._tail_r = np.zeros(0, dtype=np.float32)

        self._matched_name = Path(path).stem
        if self._ir_name_lbl:
            self._ir_name_lbl.setText(self._matched_name)

        self._notify()  # trigger a re-render
