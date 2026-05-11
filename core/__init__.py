"""
SBS-Synth Master — Core Package
================================
Exposes the primary subsystems so that `main.py` can import them with a
single `from core import ...` statement.  Every module lives in its own
file, following the one-class-per-file rule described in the architecture.
"""

from .audio_engine import AudioEngine
from .midi_logic import MidiLogic
from .controller import ControllerManager
from .gui_windows import MainWindow
from .effects import EffectChain
from .vst_engine import VstManager

__all__ = [
    "AudioEngine", "MidiLogic", "ControllerManager",
    "MainWindow", "EffectChain", "VstManager",
]