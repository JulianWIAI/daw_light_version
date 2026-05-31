"""
ai_stem_splitter.py -- AI Stem Splitter Dialog (Demucs Integration)
====================================================================
Separates a stereo audio file into 4 stems (Vocals, Drums, Bass, Other/Melody)
using Facebook Demucs, then calls back into the DAW to create new audio tracks.

Architecture (DAW thread safety):
    - StemSplitterDialog is a standalone QDialog opened from the main window.
      It never touches the audio render thread.

    - StemSplitWorker is a QThread subclass.  It runs the Demucs subprocess
      entirely in a background thread so the DAW UI stays responsive.
      When Demucs finishes it emits stems_ready(dict) back to the GUI thread
      via Qt's Signal/Slot mechanism.

    - The dialog emits tracks_ready(list[str]) once the user clicks
      "Add Stems to Timeline", passing the 4 stem file paths.  The caller
      (MainWindow) connects this signal to its track import function.

Usage from MainWindow:
    dialog = StemSplitterDialog(
        audio_file_path = "/path/to/mix.wav",
        parent = self
    )
    dialog.tracks_ready.connect(self._import_stem_tracks)
    dialog.exec()

Dependencies (optional — dialog shows a clear error if absent):
    pip install demucs
    (Demucs requires torch; the first run downloads the model weights ~80 MB.)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Dict, List

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QFileDialog, QWidget,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Background worker QThread
# ─────────────────────────────────────────────────────────────────────────────

class StemSplitWorker(QThread):
    """
    Runs Demucs in a subprocess on a background thread.

    Emits:
        progress(str)            -- log line to display in the dialog
        stems_ready(dict)        -- {'vocals': path, 'drums': path, ...}
        separation_failed(str)   -- error message
    """

    progress         = Signal(str)
    stems_ready      = Signal(dict)
    separation_failed = Signal(str)

    # Demucs v4 default model name and stem names.
    _MODEL   = "htdemucs"
    _STEMS   = ["vocals", "drums", "bass", "other"]

    def __init__(self, input_path: str, output_dir: str) -> None:
        super().__init__()
        self._input_path = input_path
        self._output_dir = output_dir

    def run(self) -> None:
        """Entry point — runs entirely in the background thread."""
        try:
            stem_paths = self._run_demucs()
            self.stems_ready.emit(stem_paths)
        except Exception as exc:
            logger.exception("StemSplitWorker failed")
            self.separation_failed.emit(str(exc))

    def _run_demucs(self) -> Dict[str, str]:
        """
        Execute Demucs via subprocess and return a dict of stem file paths.

        Demucs v4 output structure:
            <output_dir>/<model>/<file_stem>/<stem_name>.wav
        e.g.:
            /tmp/stems/htdemucs/mix/vocals.wav
        """
        # Build the command: run demucs as a module so it picks up the correct
        # Python environment (avoids PATH / shebang issues on Windows).
        cmd = [
            sys.executable, "-m", "demucs",
            "--out", self._output_dir,
            "--model", self._MODEL,
            "--two-stems", "none",   # produce all 4 stems
            self._input_path,
        ]

        # Remove '--two-stems none' — it's a flag only for 2-stem mode.
        cmd = [
            sys.executable, "-m", "demucs",
            "--out", self._output_dir,
            self._input_path,
        ]

        self.progress.emit(f"Running: {' '.join(cmd)}")
        self.progress.emit("Downloading model weights on first run (~80 MB)…")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Stream output lines back to the GUI via the signal.
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.progress.emit(line)
        proc.wait()

        if proc.returncode != 0:
            raise RuntimeError(
                f"Demucs exited with code {proc.returncode}. "
                "Check the log above for details."
            )

        # Locate the output directory.
        file_stem = Path(self._input_path).stem
        stem_dir  = Path(self._output_dir) / self._MODEL / file_stem

        if not stem_dir.is_dir():
            # Some Demucs versions write the model name differently — search.
            candidates = list(Path(self._output_dir).rglob(file_stem))
            if candidates:
                stem_dir = candidates[0]
            else:
                raise FileNotFoundError(
                    f"Could not find Demucs output directory under {self._output_dir}. "
                    f"Expected: {stem_dir}"
                )

        self.progress.emit(f"Stems written to: {stem_dir}")

        stem_paths: Dict[str, str] = {}
        for stem in self._STEMS:
            path = stem_dir / f"{stem}.wav"
            if path.is_file():
                stem_paths[stem] = str(path)
                self.progress.emit(f"  ✓ {stem}: {path.name}")
            else:
                self.progress.emit(f"  ✗ {stem}: not found (skipping)")

        if not stem_paths:
            raise FileNotFoundError("No stem files were produced by Demucs.")

        return stem_paths


# ─────────────────────────────────────────────────────────────────────────────
# Dialog
# ─────────────────────────────────────────────────────────────────────────────

_C = {
    "abyss":    "#060A18", "deep":    "#0A0E22",
    "cyan":     "#00E5FF", "purple":  "#9945FF",
    "pink":     "#FF2D9E", "gold":    "#FFD700",
    "text":     "#C8E6FF", "text_dim":"#3D5A80",
    "orange":   "#FF6B2B",
}


class StemSplitterDialog(QDialog):
    """
    Modal dialog that separates a source audio file into 4 stems and reports
    the paths back to the caller via the tracks_ready signal.

    Signals:
        tracks_ready(list[str]) -- emitted with ordered list of stem paths
                                   [vocals, drums, bass, other] for the caller
                                   to import as new DAW audio tracks.
    """

    # Ordered list of stem wav paths: [vocals, drums, bass, other]
    tracks_ready = Signal(list)

    def __init__(
        self,
        audio_file_path: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI Stem Splitter — Powered by Demucs")
        self.setMinimumSize(540, 520)
        self.setStyleSheet(f"background:{_C['abyss']}; color:{_C['text']};")

        self._input_path = audio_file_path
        self._stem_paths: Dict[str, str] = {}
        self._worker: Optional[StemSplitWorker] = None
        # Temporary output directory — cleaned up when dialog closes.
        self._tmp_dir = tempfile.mkdtemp(prefix="daw_stems_")

        self._build_ui()
        if audio_file_path:
            self._file_lbl.setText(Path(audio_file_path).name)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        # Title
        title = QLabel("AI STEM SPLITTER")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color:{_C['cyan']}; font-size:14px; font-weight:bold;"
            f" letter-spacing:2px; background:transparent;"
        )
        lay.addWidget(title)

        sub = QLabel("Separate a stereo mix into Vocals · Drums · Bass · Other")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:9px; background:transparent;"
        )
        lay.addWidget(sub)

        # ── File selection ────────────────────────────────────────────────────
        file_row = QHBoxLayout()
        self._file_lbl = QLabel("No file selected")
        self._file_lbl.setStyleSheet(
            f"color:{_C['text']}; font-size:10px; background:{_C['deep']};"
            f" border:1px solid rgba(0,229,255,0.2); border-radius:4px; padding:4px;"
        )
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.setFixedHeight(28)
        browse_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid {_C['cyan']}; border-radius:4px;"
            f" color:{_C['cyan']}; font-size:9px; }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.08); }}"
        )
        browse_btn.clicked.connect(self._browse_file)
        file_row.addWidget(self._file_lbl, 1)
        file_row.addWidget(browse_btn)
        lay.addLayout(file_row)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()

        self._run_btn = QPushButton("⚡  RUN SEPARATION")
        self._run_btn.setFixedHeight(36)
        self._run_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid {_C['purple']}; border-radius:5px;"
            f" color:{_C['purple']}; font-size:11px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:rgba(153,69,255,0.1); }}"
            f"QPushButton:disabled {{ color:{_C['text_dim']};"
            f" border-color:{_C['text_dim']}; }}"
        )
        self._run_btn.clicked.connect(self._start_separation)

        self._add_btn = QPushButton("✓  ADD STEMS TO TIMELINE")
        self._add_btn.setFixedHeight(36)
        self._add_btn.setEnabled(False)
        self._add_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']};"
            f" border:1px solid {_C['cyan']}; border-radius:5px;"
            f" color:{_C['cyan']}; font-size:11px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.08); }}"
            f"QPushButton:disabled {{ color:{_C['text_dim']};"
            f" border-color:{_C['text_dim']}; }}"
        )
        self._add_btn.clicked.connect(self._add_to_timeline)

        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._add_btn)
        lay.addLayout(btn_row)

        # ── Progress bar ──────────────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate (marquee) mode
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setStyleSheet(
            f"QProgressBar {{ background:{_C['deep']}; border:none; border-radius:3px; }}"
            f"QProgressBar::chunk {{ background:{_C['purple']}; border-radius:3px; }}"
        )
        self._progress.setVisible(False)
        lay.addWidget(self._progress)

        # ── Log output ────────────────────────────────────────────────────────
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(180)
        self._log.setStyleSheet(
            f"background:{_C['deep']}; color:{_C['text_dim']};"
            f" border:1px solid rgba(0,229,255,0.1); border-radius:4px;"
            f" font-family:Consolas,monospace; font-size:8px; padding:4px;"
        )
        lay.addWidget(self._log)

        # ── Stem list (shown after completion) ────────────────────────────────
        self._stem_lbl = QLabel("")
        self._stem_lbl.setWordWrap(True)
        self._stem_lbl.setStyleSheet(
            f"color:{_C['gold']}; font-size:9px; background:transparent;"
        )
        lay.addWidget(self._stem_lbl)

        lay.addStretch()

    # ── File browser ──────────────────────────────────────────────────────────

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select audio file", "",
            "Audio Files (*.wav *.mp3 *.flac *.aiff *.ogg);;All Files (*)"
        )
        if path:
            self._input_path = path
            self._file_lbl.setText(Path(path).name)

    # ── Start separation ──────────────────────────────────────────────────────

    def _start_separation(self) -> None:
        if not self._input_path or not Path(self._input_path).is_file():
            self._log_line("⚠  Please select a valid audio file first.")
            return

        # Check Demucs is installed.
        try:
            import demucs  # noqa: F401
        except ImportError:
            self._log_line(
                "⚠  Demucs not found. Install with: pip install demucs\n"
                "   (also requires PyTorch: https://pytorch.org/get-started/locally/)"
            )
            return

        self._run_btn.setEnabled(False)
        self._add_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._stem_lbl.setText("")
        self._log.clear()
        self._log_line(f"Input file:  {self._input_path}")
        self._log_line(f"Output dir:  {self._tmp_dir}")

        # Launch background worker — never blocks the GUI thread.
        self._worker = StemSplitWorker(self._input_path, self._tmp_dir)
        self._worker.progress          .connect(self._log_line)
        self._worker.stems_ready       .connect(self._on_stems_ready)
        self._worker.separation_failed .connect(self._on_separation_failed)
        self._worker.finished          .connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_finished(self) -> None:
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)

    def _on_stems_ready(self, paths: Dict[str, str]) -> None:
        """Called on the GUI thread when Demucs has finished successfully."""
        self._stem_paths = paths
        lines = "\n".join(f"  ✓ {k}: {Path(v).name}" for k, v in paths.items())
        self._stem_lbl.setText(f"Stems ready:\n{lines}")
        self._add_btn.setEnabled(True)
        self._log_line("Separation complete!")

    def _on_separation_failed(self, msg: str) -> None:
        self._log_line(f"✗ Separation failed:\n{msg}")

    # ── Add to timeline ───────────────────────────────────────────────────────

    def _add_to_timeline(self) -> None:
        """
        Emit tracks_ready with the stem paths in canonical order.
        The caller (MainWindow) is responsible for creating audio tracks.
        """
        ordered = []
        for stem in ["vocals", "drums", "bass", "other"]:
            if stem in self._stem_paths:
                ordered.append(self._stem_paths[stem])

        if ordered:
            self.tracks_ready.emit(ordered)
            self._log_line(f"Sent {len(ordered)} stems to timeline.")
            self.accept()

    # ── Helper ────────────────────────────────────────────────────────────────

    def _log_line(self, text: str) -> None:
        self._log.append(text)
