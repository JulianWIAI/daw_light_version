"""
export_dialog.py -- Multi-Format Mastering Export Dialog
=========================================================
PySide6 QDialog that lets the user select which commercial-target formats
to bounce, pick an output folder, and monitor live export progress.

Connected worker: MasteringExportWorker (QThread defined in
mastering_export_worker.py).  The dialog owns the worker lifetime:
it creates the thread when the user clicks Export and stops it cleanly
when the dialog is closed or the export completes.

Dark theme mirrors the main window palette (void / abyss / cyan).
All PySide6 enum references use the fully-qualified enum form required
by PySide6 strict-enum mode (e.g. Qt.AlignmentFlag.AlignLeft,
Qt.CheckState.Checked).
"""

from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from .mastering_export_worker import ExportConfig, MasteringExportWorker
from .project_render_info import FullProjectRenderInfo

# ── Palette (kept in sync with gui_windows.C) ────────────────────────────────
_C = {
    "void":     "#030308",
    "abyss":    "#060A18",
    "deep":     "#0A0E22",
    "surface":  "#0E1430",
    "cyan":     "#00E5FF",
    "purple":   "#9945FF",
    "lime":     "#39FF14",
    "orange":   "#FF6B2B",
    "text":     "#C8E6FF",
    "text_dim": "#3D5A80",
}

_DIALOG_SS = f"""
QDialog {{
    background: {_C["abyss"]};
    color: {_C["text"]};
    font-family: 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px;
}}
QGroupBox {{
    background: {_C["deep"]};
    border: 1px solid rgba(0,229,255,0.18);
    border-radius: 6px;
    margin-top: 10px;
    padding: 8px 10px;
    color: {_C["text"]};
    font-size: 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: {_C["cyan"]};
}}
QCheckBox {{
    color: {_C["text"]};
    spacing: 8px;
    font-size: 13px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid rgba(0,229,255,0.35);
    border-radius: 3px;
    background: {_C["deep"]};
}}
QCheckBox::indicator:checked {{
    background: {_C["cyan"]};
    border-color: {_C["cyan"]};
}}
QCheckBox::indicator:hover {{
    border-color: {_C["cyan"]};
}}
QRadioButton {{
    color: {_C["text"]};
    spacing: 8px;
    font-size: 13px;
}}
QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid rgba(0,229,255,0.35);
    border-radius: 7px;
    background: {_C["deep"]};
}}
QRadioButton::indicator:checked {{
    background: {_C["cyan"]};
    border-color: {_C["cyan"]};
}}
QRadioButton::indicator:hover {{
    border-color: {_C["cyan"]};
}}
QLabel {{
    color: {_C["text"]};
    background: transparent;
}}
QLineEdit {{
    background: {_C["deep"]};
    border: 1px solid rgba(0,229,255,0.20);
    border-radius: 4px;
    padding: 5px 8px;
    color: {_C["text"]};
    selection-background-color: {_C["cyan"]};
    selection-color: {_C["void"]};
}}
QLineEdit:focus {{
    border-color: {_C["cyan"]};
}}
QProgressBar {{
    background: {_C["deep"]};
    border: 1px solid rgba(0,229,255,0.18);
    border-radius: 4px;
    color: transparent;
    text-align: center;
    height: 10px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {_C["purple"]}, stop:1 {_C["cyan"]});
    border-radius: 4px;
}}
QPushButton {{
    background: {_C["deep"]};
    border: 1px solid rgba(0,229,255,0.25);
    border-radius: 5px;
    padding: 7px 18px;
    color: {_C["text"]};
    font-size: 13px;
}}
QPushButton:hover {{
    background: {_C["surface"]};
    border-color: {_C["cyan"]};
    color: {_C["cyan"]};
}}
QPushButton:disabled {{
    background: {_C["deep"]};
    border-color: rgba(0,229,255,0.08);
    color: {_C["text_dim"]};
}}
"""

# Per-target accent colours shown as a small tag next to the checkbox label.
_TARGET_META = {
    "Preview MP3":    (_C["orange"],  "-7 LUFS · 320 kbps MP3"),
    "Streaming WAV":  (_C["cyan"],    "-14 LUFS · -1 dBFS · 24-bit WAV"),
    "Lease WAV":      (_C["purple"],  "-3 dBFS peak · 24-bit WAV"),
    "Trackout Stems": (_C["lime"],    "-3 dBFS peak · 24-bit WAV stems"),
}


class ExportDialog(QDialog):
    """
    Multi-format mastering export dialog.

    Parameters
    ----------
    render_info:
        Complete project snapshot (MIDI + audio + step events + BPM).
        Built in gui_windows._on_master_export() and passed here so the
        worker thread never needs to touch live Qt objects.
    output_dir:
        Default output directory (last used path or project directory).
    project_name:
        Default base filename stem (no extension).
    parent:
        Parent QWidget.
    """

    def __init__(
        self,
        render_info: FullProjectRenderInfo,
        output_dir: str = "",
        project_name: str = "project",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._render_info = render_info
        self._worker: Optional[MasteringExportWorker] = None

        self.setWindowTitle("Export / Master")
        self.setMinimumWidth(540)
        self.setModal(True)
        self.setStyleSheet(_DIALOG_SS)

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(18, 18, 18, 18)

        # ── Header ──────────────────────────────────────────────────────────
        hdr = QLabel("Mastering & Export")
        hdr.setStyleSheet(
            f"font-size:17px; font-weight:700; color:{_C['cyan']};"
            f" letter-spacing:1px;"
        )
        root.addWidget(hdr)

        sub = QLabel(
            "Select one or more targets. Each target applies its own mastering"
            " chain on a shared offline render of the project."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{_C['text_dim']}; font-size:11px;")
        root.addWidget(sub)

        # ── Target checkboxes ────────────────────────────────────────────────
        targets_box = QGroupBox("Export Targets")
        targets_layout = QVBoxLayout(targets_box)
        targets_layout.setSpacing(8)

        self._preset_factories = [
            ExportConfig.preview_mp3,
            ExportConfig.streaming_wav,
            ExportConfig.lease_wav,
            ExportConfig.stems_wav,
        ]
        self._checkboxes: List[QCheckBox] = []

        for factory in self._preset_factories:
            cfg = factory()
            color, tag_text = _TARGET_META.get(
                cfg.target_name, (_C["text_dim"], "")
            )

            row = QHBoxLayout()
            row.setSpacing(10)

            cb = QCheckBox(cfg.target_name)
            cb.setCheckState(Qt.CheckState.Checked)
            self._checkboxes.append(cb)
            row.addWidget(cb)

            if tag_text:
                tag = QLabel(tag_text)
                tag.setStyleSheet(
                    f"color:{color}; font-size:10px; background:transparent;"
                )
                tag.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                row.addWidget(tag, 1)

            targets_layout.addLayout(row)

        root.addWidget(targets_box)

        # ── Mastering flavor ─────────────────────────────────────────────────
        flavor_box = QGroupBox("Mastering Flavor")
        flavor_layout = QVBoxLayout(flavor_box)
        flavor_layout.setSpacing(6)

        flavor_desc = QLabel(
            "Applied once to the raw mix before all targets."
        )
        flavor_desc.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:10px; background:transparent;"
        )
        flavor_layout.addWidget(flavor_desc)

        self._flavor_btn_group = QButtonGroup(self)
        self._flavor_radios: List[QRadioButton] = []
        _flavor_data = [
            (0, "Transparent",   "No color — flat signal path"),
            (1, "Analog Warmth", "Soft saturation + high-shelf roll-off (−1 dB @ 12 kHz)"),
            (2, "Club / Festival", "Low-shelf boost (+2 dB @ 50 Hz) + light VCA compression"),
        ]
        for fid, label, desc in _flavor_data:
            rb = QRadioButton(label)
            rb.setChecked(fid == 0)
            self._flavor_btn_group.addButton(rb, fid)
            self._flavor_radios.append(rb)

            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(rb)
            tag = QLabel(desc)
            tag.setStyleSheet(
                f"color:{_C['text_dim']}; font-size:10px; background:transparent;"
            )
            tag.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(tag, 1)
            flavor_layout.addLayout(row)

        root.addWidget(flavor_box)

        # ── Output folder ────────────────────────────────────────────────────
        folder_box = QGroupBox("Output")
        folder_layout = QVBoxLayout(folder_box)
        folder_layout.setSpacing(8)

        # Project name row
        name_row = QHBoxLayout()
        name_lbl = QLabel("Project name:")
        name_lbl.setFixedWidth(100)
        name_row.addWidget(name_lbl)
        self._name_edit = QLineEdit(project_name)
        self._name_edit.setPlaceholderText("e.g.  my_track")
        name_row.addWidget(self._name_edit, 1)
        folder_layout.addLayout(name_row)

        # Folder row
        dir_row = QHBoxLayout()
        dir_lbl = QLabel("Folder:")
        dir_lbl.setFixedWidth(100)
        dir_row.addWidget(dir_lbl)
        self._dir_edit = QLineEdit(output_dir or os.path.expanduser("~"))
        self._dir_edit.setReadOnly(True)
        dir_row.addWidget(self._dir_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_folder)
        dir_row.addWidget(browse_btn)
        folder_layout.addLayout(dir_row)

        root.addWidget(folder_box)

        # ── Progress section ─────────────────────────────────────────────────
        progress_box = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_box)
        progress_layout.setSpacing(6)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(10)
        progress_layout.addWidget(self._progress_bar)

        self._status_label = QLabel("Ready.")
        self._status_label.setStyleSheet(
            f"color:{_C['text_dim']}; font-size:11px; background:transparent;"
        )
        self._status_label.setWordWrap(True)
        progress_layout.addWidget(self._status_label)

        root.addWidget(progress_box)

        # ── Buttons ──────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        )

        self._export_btn = QPushButton("Export")
        self._export_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"  stop:0 {_C['purple']}, stop:1 {_C['cyan']});"
            f"  border: none; border-radius: 5px;"
            f"  padding: 8px 28px;"
            f"  color: {_C['void']}; font-weight: 700; font-size: 13px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {_C['cyan']}; color: {_C['void']};"
            f"}}"
            f"QPushButton:disabled {{"
            f"  background: {_C['deep']}; color: {_C['text_dim']};"
            f"}}"
        )
        self._export_btn.clicked.connect(self._start_export)
        btn_row.addWidget(self._export_btn)

        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self._on_close_clicked)
        btn_row.addWidget(self._close_btn)

        root.addLayout(btn_row)

    # ── Slots ────────────────────────────────────────────────────────────────

    @Slot()
    def _browse_folder(self) -> None:
        current = self._dir_edit.text()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            current if os.path.isdir(current) else os.path.expanduser("~"),
        )
        if chosen:
            self._dir_edit.setText(chosen)

    @Slot()
    def _start_export(self) -> None:
        selected_configs = self._build_selected_configs()
        if not selected_configs:
            self._status_label.setText("No targets selected — please tick at least one.")
            return

        output_dir = self._dir_edit.text().strip()
        if not output_dir or not os.path.isdir(output_dir):
            self._status_label.setText(
                "Output folder does not exist. Please choose a valid folder."
            )
            return

        project_name = self._name_edit.text().strip() or "project"

        self._set_busy(True)
        self._progress_bar.setValue(0)
        self._status_label.setText("Starting export…")

        self._worker = MasteringExportWorker(
            render_info  = self._render_info,
            configs      = selected_configs,
            output_dir   = output_dir,
            project_name = project_name,
            flavor_id    = self._flavor_btn_group.checkedId(),
            parent       = self,
        )
        self._worker.progress_updated.connect(self._on_progress)
        self._worker.status_changed.connect(self._on_status)
        self._worker.export_finished.connect(self._on_finished)
        self._worker.start()

    @Slot(int)
    def _on_progress(self, value: int) -> None:
        self._progress_bar.setValue(value)

    @Slot(str)
    def _on_status(self, message: str) -> None:
        self._status_label.setText(message)

    @Slot(bool, str)
    def _on_finished(self, success: bool, summary: str) -> None:
        self._set_busy(False)
        self._progress_bar.setValue(100 if success else self._progress_bar.value())
        self._status_label.setText(summary)
        if success:
            self._status_label.setStyleSheet(
                f"color:{_C['lime']}; font-size:11px; background:transparent;"
            )
        else:
            self._status_label.setStyleSheet(
                f"color:{_C['orange']}; font-size:11px; background:transparent;"
            )

    @Slot()
    def _on_close_clicked(self) -> None:
        self._stop_worker()
        self.reject()

    # ── QDialog override ─────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_worker()
        super().closeEvent(event)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_selected_configs(self) -> List[ExportConfig]:
        configs: List[ExportConfig] = []
        for cb, factory in zip(self._checkboxes, self._preset_factories):
            if cb.checkState() == Qt.CheckState.Checked:
                cfg = factory()
                cfg.enabled = True
                configs.append(cfg)
        return configs

    def _set_busy(self, busy: bool) -> None:
        """Lock / unlock UI controls during an active export run."""
        self._export_btn.setEnabled(not busy)
        self._close_btn.setText("Cancel" if busy else "Close")
        for cb in self._checkboxes:
            cb.setEnabled(not busy)
        for rb in self._flavor_radios:
            rb.setEnabled(not busy)
        self._name_edit.setEnabled(not busy)

    def _stop_worker(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait(3000)
        self._worker = None
