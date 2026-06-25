"""
auto_mastering_dialog.py — Automaster UI Dialog
================================================
PySide6 dialog that exposes all AutoMasterEngine parameters and runs the
mastering pipeline on a background thread so the GUI stays responsive.

Controls map directly to AutoMasterEngine.process_file() arguments:
    Input WAV       → input_path
    Output WAV      → output_path
    Genre           → genre
    Target LUFS     → target_lufs
    True Peak       → target_true_peak
    Stereo Width    → stereo_width

Usage
-----
    dialog = AutoMasterDialog(parent=main_window)
    dialog.set_input_path("/path/to/rendered_mix.wav")   # optional pre-fill
    dialog.exec()
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QComboBox, QDoubleSpinBox, QSlider,
    QProgressBar, QMessageBox, QGroupBox, QFormLayout, QLineEdit,
)

from .auto_mastering import AutoMasterEngine

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Colour palette (matches the rest of the DAW)
# ─────────────────────────────────────────────────────────────────────────────

_C = {
    "abyss":  "#060A18",
    "deep":   "#0A0E22",
    "panel":  "#0D1128",
    "cyan":   "#00E5FF",
    "purple": "#9945FF",
    "gold":   "#FFD700",
    "green":  "#00FF88",
    "text":   "#C8E6FF",
    "dim":    "#3D5A80",
    "orange": "#FF6B2B",
    "border": "rgba(0,229,255,0.18)",
}

_STYLESHEET = f"""
    QDialog {{
        background: {_C['abyss']};
        color:      {_C['text']};
    }}
    QGroupBox {{
        background:  {_C['deep']};
        color:       {_C['cyan']};
        border:      1px solid {_C['border']};
        border-radius: 6px;
        margin-top:  14px;
        padding:     8px 10px;
        font-size:   10px;
        font-weight: bold;
        letter-spacing: 1px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        padding: 0 6px;
        color: {_C['cyan']};
    }}
    QLabel {{
        color:       {_C['text']};
        font-size:   10px;
        background:  transparent;
    }}
    QLineEdit {{
        background:  {_C['panel']};
        color:       {_C['text']};
        border:      1px solid {_C['border']};
        border-radius: 4px;
        padding:     3px 6px;
        font-size:   9px;
    }}
    QComboBox {{
        background:  {_C['panel']};
        color:       {_C['text']};
        border:      1px solid {_C['border']};
        border-radius: 4px;
        font-size:   9px;
        padding:     3px 8px;
    }}
    QComboBox QAbstractItemView {{
        background:  {_C['deep']};
        color:       {_C['text']};
        selection-background-color: {_C['purple']};
    }}
    QDoubleSpinBox {{
        background:  {_C['panel']};
        color:       {_C['gold']};
        border:      1px solid {_C['border']};
        border-radius: 4px;
        font-size:   10px;
        padding:     2px 6px;
    }}
    QSlider::groove:horizontal {{
        background: {_C['dim']};
        height: 4px;
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background:   {_C['cyan']};
        border:       1px solid {_C['border']};
        width: 14px;  height: 14px;
        margin: -5px 0;
        border-radius: 7px;
    }}
    QSlider::sub-page:horizontal {{
        background: {_C['cyan']};
        border-radius: 2px;
    }}
    QProgressBar {{
        background:    {_C['panel']};
        color:         {_C['green']};
        border:        1px solid {_C['border']};
        border-radius: 4px;
        text-align:    center;
        font-size:     9px;
    }}
    QProgressBar::chunk {{
        background: {_C['green']};
        border-radius: 3px;
    }}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────

class _MasterWorker(QThread):
    """
    Runs AutoMasterEngine.process_file() on a background thread.

    Signals
    -------
    progress(int, str)     — percent 0-100 and a status message
    finished(dict)         — result dict from process_file() on success
    failed(str)            — error message on failure
    """

    progress = Signal(int, str)
    finished = Signal(dict)
    failed   = Signal(str)

    def __init__(
        self,
        input_path:       str,
        output_path:      str,
        genre:            str,
        target_lufs:      float,
        target_true_peak: float,
        stereo_width:     float,
    ) -> None:
        super().__init__()
        self._input_path       = input_path
        self._output_path      = output_path
        self._genre            = genre
        self._target_lufs      = target_lufs
        self._target_true_peak = target_true_peak
        self._stereo_width     = stereo_width

    def run(self) -> None:
        try:
            engine = AutoMasterEngine()
            result = engine.process_file(
                input_path        = self._input_path,
                output_path       = self._output_path,
                genre             = self._genre,
                target_lufs       = self._target_lufs,
                target_true_peak  = self._target_true_peak,
                stereo_width      = self._stereo_width,
                progress_callback = lambda pct, msg: self.progress.emit(pct, msg),
            )
            self.finished.emit(result)
        except Exception as exc:
            logger.exception("AutoMaster worker failed")
            self.failed.emit(str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Dialog
# ─────────────────────────────────────────────────────────────────────────────

class AutoMasterDialog(QDialog):
    """
    Offline auto-mastering dialog.

    All parameter widgets map directly to AutoMasterEngine.process_file():

        Genre combo        → genre
        Target LUFS spin   → target_lufs
        True Peak spin     → target_true_peak
        Width slider       → stereo_width
    """

    # Emitted after a successful master so the host window can offer to
    # play the result or import it as a new audio clip.
    master_completed = Signal(str)   # output_path

    def __init__(self, parent=None, input_path: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto-Master")
        self.setMinimumWidth(460)
        self.setStyleSheet(_STYLESHEET)
        self._worker: Optional[_MasterWorker] = None

        self._build_ui()

        if input_path:
            self._input_edit.setText(input_path)
            self._suggest_output(input_path)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Title
        title = QLabel("AUTO-MASTER")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color:{_C['cyan']}; font-size:14px; font-weight:bold;"
            f" letter-spacing:4px; background:transparent;"
        )
        root.addWidget(title)

        subtitle = QLabel("Offline post-mix mastering chain")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet(
            f"color:{_C['dim']}; font-size:9px; background:transparent;"
        )
        root.addWidget(subtitle)

        # ── I/O ───────────────────────────────────────────────────────────────
        io_box = QGroupBox("I / O")
        io_lay = QFormLayout(io_box)
        io_lay.setSpacing(6)

        self._input_edit = QLineEdit()
        self._input_edit.setPlaceholderText("Path to rendered stereo WAV …")
        self._input_edit.textChanged.connect(self._on_input_changed)
        btn_in = self._small_button("Browse")
        btn_in.clicked.connect(self._browse_input)
        in_row = QHBoxLayout()
        in_row.addWidget(self._input_edit, 1)
        in_row.addWidget(btn_in)
        io_lay.addRow("Input WAV:", in_row)

        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("Output path …")
        btn_out = self._small_button("Browse")
        btn_out.clicked.connect(self._browse_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self._output_edit, 1)
        out_row.addWidget(btn_out)
        io_lay.addRow("Output WAV:", out_row)

        root.addWidget(io_box)

        # ── Parameters ────────────────────────────────────────────────────────
        param_box = QGroupBox("Mastering Parameters")
        param_lay = QFormLayout(param_box)
        param_lay.setSpacing(8)

        # Genre
        self._genre_combo = QComboBox()
        self._genre_combo.addItems([
            "electronic", "hiphop", "trap", "pop", "classical", "cinematic", "other",
        ])
        self._genre_combo.setCurrentText("other")
        param_lay.addRow("Genre:", self._genre_combo)

        # Target LUFS
        self._lufs_spin = QDoubleSpinBox()
        self._lufs_spin.setRange(-30.0, -5.0)
        self._lufs_spin.setSingleStep(0.5)
        self._lufs_spin.setDecimals(1)
        self._lufs_spin.setValue(-14.0)
        self._lufs_spin.setSuffix("  LUFS")
        self._lufs_spin.setToolTip(
            "Spotify -14 · Apple Music -16 · YouTube -14 · CD -9"
        )
        param_lay.addRow("Target LUFS:", self._lufs_spin)

        # True Peak
        self._tp_spin = QDoubleSpinBox()
        self._tp_spin.setRange(-6.0, -0.1)
        self._tp_spin.setSingleStep(0.1)
        self._tp_spin.setDecimals(1)
        self._tp_spin.setValue(-1.0)
        self._tp_spin.setSuffix("  dBTP")
        self._tp_spin.setToolTip(
            "Standard streaming delivery: -1.0 dBTP.  "
            "Use -0.3 for CD or broadcast."
        )
        param_lay.addRow("True Peak:", self._tp_spin)

        # Stereo Width
        width_row = QHBoxLayout()
        self._width_slider = QSlider(Qt.Horizontal)
        self._width_slider.setRange(50, 200)     # 0.5 → 2.0 mapped as ×100
        self._width_slider.setValue(100)
        self._width_slider.setTickInterval(25)
        self._width_label = QLabel("1.00×")
        self._width_label.setFixedWidth(38)
        self._width_label.setStyleSheet(f"color:{_C['gold']}; font-size:10px;")
        self._width_slider.valueChanged.connect(self._on_width_changed)
        width_row.addWidget(self._width_slider, 1)
        width_row.addWidget(self._width_label)
        param_lay.addRow("Stereo Width:", width_row)

        root.addWidget(param_box)

        # ── Progress ──────────────────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(18)
        root.addWidget(self._progress)

        self._status_lbl = QLabel("")
        self._status_lbl.setAlignment(Qt.AlignCenter)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            f"color:{_C['dim']}; font-size:8px; background:transparent;"
        )
        root.addWidget(self._status_lbl)

        # ── Result readout ────────────────────────────────────────────────────
        self._result_lbl = QLabel("")
        self._result_lbl.setAlignment(Qt.AlignCenter)
        self._result_lbl.setWordWrap(True)
        self._result_lbl.setStyleSheet(
            f"color:{_C['green']}; font-size:9px; background:{_C['deep']};"
            f" border:1px solid rgba(0,255,136,0.2); border-radius:4px;"
            f" padding:4px;"
        )
        self._result_lbl.setVisible(False)
        root.addWidget(self._result_lbl)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()

        self._start_btn = QPushButton("▶  Master")
        self._start_btn.setFixedHeight(34)
        self._start_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']}; color:{_C['cyan']};"
            f" border:1px solid {_C['cyan']}; border-radius:5px;"
            f" font-size:11px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.1); }}"
            f"QPushButton:disabled {{ color:{_C['dim']}; border-color:{_C['dim']}; }}"
        )
        self._start_btn.clicked.connect(self._start_mastering)

        self._cancel_btn = QPushButton("✕  Close")
        self._cancel_btn.setFixedHeight(34)
        self._cancel_btn.setStyleSheet(
            f"QPushButton {{ background:{_C['deep']}; color:{_C['dim']};"
            f" border:1px solid {_C['dim']}; border-radius:5px; font-size:11px; }}"
            f"QPushButton:hover {{ color:{_C['orange']}; border-color:{_C['orange']}; }}"
        )
        self._cancel_btn.clicked.connect(self._on_cancel)

        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._cancel_btn)
        root.addLayout(btn_row)

    # ── Slots ─────────────────────────────────────────────────────────────────

    @Slot(str)
    def set_input_path(self, path: str) -> None:
        self._input_edit.setText(path)
        self._suggest_output(path)

    @Slot()
    def _browse_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select rendered stereo WAV",
            os.path.expanduser("~"),
            "WAV files (*.wav);;All files (*)",
        )
        if path:
            self._input_edit.setText(path)
            self._suggest_output(path)

    @Slot()
    def _browse_output(self) -> None:
        default = self._output_edit.text() or os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save mastered WAV",
            default,
            "WAV files (*.wav)",
        )
        if path:
            if not path.lower().endswith(".wav"):
                path += ".wav"
            self._output_edit.setText(path)

    @Slot(str)
    def _on_input_changed(self, text: str) -> None:
        if not self._output_edit.text():
            self._suggest_output(text)

    @Slot(int)
    def _on_width_changed(self, value: int) -> None:
        self._width_label.setText(f"{value / 100:.2f}×")

    @Slot()
    def _start_mastering(self) -> None:
        input_path  = self._input_edit.text().strip()
        output_path = self._output_edit.text().strip()

        if not input_path:
            QMessageBox.warning(self, "Missing input", "Please select an input WAV file.")
            return
        if not os.path.isfile(input_path):
            QMessageBox.warning(self, "File not found", f"Input file not found:\n{input_path}")
            return
        if not output_path:
            QMessageBox.warning(self, "Missing output", "Please specify an output path.")
            return

        self._start_btn.setEnabled(False)
        self._progress.setValue(0)
        self._result_lbl.setVisible(False)
        self._set_status("Starting mastering pipeline …")

        self._worker = _MasterWorker(
            input_path        = input_path,
            output_path       = output_path,
            genre             = self._genre_combo.currentText(),
            target_lufs       = self._lufs_spin.value(),
            target_true_peak  = self._tp_spin.value(),
            stereo_width      = self._width_slider.value() / 100.0,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed  .connect(self._on_failed)
        self._worker.start()

    @Slot(int, str)
    def _on_progress(self, pct: int, msg: str) -> None:
        self._progress.setValue(pct)
        self._set_status(msg)

    @Slot(dict)
    def _on_finished(self, result: dict) -> None:
        self._start_btn.setEnabled(True)
        self._progress.setValue(100)
        self._set_status("Mastering complete.")

        in_lufs  = result.get("input_lufs",  0.0)
        out_lufs = result.get("output_lufs", 0.0)
        gain_db  = result.get("gain_db",     0.0)
        sr       = result.get("sr",          44100)
        n        = result.get("n_samples",   0)
        duration = n / sr if sr > 0 else 0.0

        self._result_lbl.setText(
            f"✓  Input: {in_lufs:.1f} LUFS  →  Output: {out_lufs:.1f} LUFS  "
            f"({gain_db:+.2f} dB)  |  {duration:.1f} s  @ {sr} Hz"
        )
        self._result_lbl.setVisible(True)

        output_path = self._output_edit.text().strip()
        self.master_completed.emit(output_path)
        logger.info("AutoMaster finished → %s", output_path)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._start_btn.setEnabled(True)
        self._set_status(f"✗ Error: {message}")
        QMessageBox.critical(
            self, "Mastering Failed",
            f"The mastering pipeline encountered an error:\n\n{message}",
        )

    @Slot()
    def _on_cancel(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)
            self._set_status("Cancelled.")
            self._start_btn.setEnabled(True)
        self.reject()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _suggest_output(self, input_path: str) -> None:
        """Auto-fill the output path as <name>_mastered.wav next to the input."""
        if not input_path or not os.path.isfile(input_path):
            return
        base, _ = os.path.splitext(input_path)
        self._output_edit.setText(base + "_mastered.wav")

    def _set_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    @staticmethod
    def _small_button(label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedWidth(56)
        btn.setFixedHeight(24)
        btn.setStyleSheet(
            f"QPushButton {{ background:{_C['panel']}; color:{_C['cyan']};"
            f" border:1px solid {_C['border']}; border-radius:3px; font-size:9px; }}"
            f"QPushButton:hover {{ background:rgba(0,229,255,0.07); }}"
        )
        return btn
