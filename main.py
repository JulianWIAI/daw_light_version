"""
main.py — SBS-Synth Master Entry Point
=======================================
Thin launcher: bootstraps logging, wires the three core subsystems
(AudioEngine, MidiLogic, ControllerManager), shows the MainWindow, and
hands control to the PySide6 event loop.

All application logic lives in core/.  Keeping main.py small makes it
easy to add alternative entry points (headless test runner, CLI export
tool) without touching any core code.
"""

import logging
import os
import sys

# Add the compiled C++ extension directory to sys.path so that
# "import daw_processors" resolves to the .pyd/.so built in-place.
# This must happen before any core module is imported because
# TimelineEngineBridge (imported transitively) tries to import
# daw_processors at construction time.
_CPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpp_processors")
if _CPP_DIR not in sys.path:
    sys.path.insert(0, _CPP_DIR)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

from core.audio_engine import AudioEngine
from core.midi_logic import MidiLogic
from core.controller import ControllerManager
from core.gui_windows import MainWindow


def configure_logging() -> None:
    """
    Set up root logger so every module emits structured lines to stdout.

    Why stdout?  macOS Console.app reads stdout/stderr from bundled apps,
    making it trivial to monitor logs without a separate log file.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def main() -> int:
    """
    Application entry point.

    Steps:
        1. Configure logging.
        2. Create the QApplication (must happen before any widget).
        3. Start the AudioEngine (opens audio hardware via FluidSynth).
        4. Instantiate MidiLogic (sequencer) and ControllerManager (input).
        5. Show the MainWindow — the user adds tracks via "+ Add Track".
        6. Enter the Qt event loop; return its exit code on close.
    """
    configure_logging()
    log = logging.getLogger(__name__)
    log.info("=== SBS-Synth Master starting ===")

    # Must be set before QApplication is constructed.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # QApplication must be created before any QWidget.
    app = QApplication(sys.argv)
    app.setApplicationName("SBS-Synth Master")
    app.setOrganizationName("SBS Studio")

    icon_path = os.path.join(os.path.dirname(__file__), "assets", "icons", "icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Audio subsystem — FluidSynth opens CoreAudio on macOS.
    engine = AudioEngine()
    started = engine.start(sample_rate=44100, gain=0.8)
    if not started:
        log.warning(
            "Audio engine did not start — running in SILENT mode.  "
            "Make sure FluidSynth is installed:  brew install fluid-synth"
        )

    # MIDI sequencer and input controller.
    midi = MidiLogic()
    midi.bpm = 120.01
    controller = ControllerManager(engine, midi)

    # Main window — starts empty; user adds tracks via the toolbar.
    window = MainWindow(engine, midi, controller)
    window._on_scale_changed()   # populate the status bar scale label

    # Wire the audio engine's output to the telemetry analyzer so every note
    # played via FluidSynth (SF2) is captured alongside SFZ/DS streams.
    engine._telemetry_push = window._telemetry.push_audio

    window.show()
    window.raise_()
    window.activateWindow()
    app.processEvents()

    log.info("Window ready — entering Qt event loop.")
    exit_code = app.exec()
    log.info("=== SBS-Synth Master exiting (code %d) ===", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())