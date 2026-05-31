"""
fx_plugin_base.py -- Abstract base class for every FX rack insert-slot plugin.
===============================================================================
All effects (pedalboard wrappers, C++ processors, future VST bridges) must
subclass FxPluginBase so the FX rack can treat every plugin uniformly.

Contract for subclasses:
    DISPLAY_NAME  -- str shown in the slot picker dropdown menu.
    process()     -- apply DSP and return the processed float32 audio array.
    create_parameter_widget() -- build and return a styled QWidget with controls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Callable

import numpy as np


class FxPluginBase(ABC):
    """
    Common interface for every effect that can be loaded into an FX rack slot.

    Attributes:
        DISPLAY_NAME : Class-level string used in the slot picker menu.
        enabled      : When False the plugin is skipped in the signal chain
                       (bypass mode). Toggled by the slot's power button.
        _on_changed  : Optional callable set by FxRackWidget so parameter
                       edits trigger an immediate re-render signal.
    """

    # Override in every subclass -- shown in the effect picker menu.
    DISPLAY_NAME: str = "Effect"

    def __init__(self) -> None:
        # Bypass toggle: False = bypassed (plugin skipped in chain).
        self.enabled: bool = True
        # Callback injected by FxRackWidget; call via self._notify() to
        # signal that parameters have changed and a re-render may be needed.
        self._on_changed: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # Notification helper (used by parameter widgets)
    # ------------------------------------------------------------------

    def _notify(self) -> None:
        """Fire the parameter-changed callback if one has been registered."""
        if self._on_changed is not None:
            self._on_changed()

    # ------------------------------------------------------------------
    # Audio processing (must be thread-safe: called from a daemon thread)
    # ------------------------------------------------------------------

    @abstractmethod
    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """
        Apply DSP to the audio buffer and return the processed result.

        This method is called from AudioFilePlayer's background render thread.
        It must NOT touch any Qt widgets or GUI state.

        Args:
            audio       : float32 ndarray, shape (n_samples, n_channels).
                          Typically stereo: (n_samples, 2).
            sample_rate : Playback sample rate in Hz (e.g. 44100).

        Returns:
            Processed float32 ndarray, same shape as input.
            Return the input unchanged if the plugin has no effect at current
            settings (e.g. all-zero EQ bands).
        """

    # ------------------------------------------------------------------
    # GUI (called from the main/GUI thread only)
    # ------------------------------------------------------------------

    @abstractmethod
    def create_parameter_widget(self) -> "QWidget":  # type: ignore[name-defined]
        """
        Build and return a QWidget containing all parameter controls for this
        plugin.  The widget is shown in the FxRackWidget parameter panel when
        the user selects this slot.

        The widget must call self._notify() (via slider callbacks) whenever
        a parameter changes so FxRackWidget can emit chain_changed.
        """

    # ------------------------------------------------------------------
    # Instrument capability query
    # ------------------------------------------------------------------

    def is_instrument_active(self) -> bool:
        """
        Return True if this plugin acts as a note-driven instrument and is
        ready to produce sound on its own (e.g. a sampler with a file loaded).

        When any plugin in a MIDI track's FX chain returns True, the FluidSynth
        engine is bypassed for that channel so only the instrument plugin plays.
        Pure effect plugins (EQ, reverb, etc.) must leave this returning False.
        """
        return False

    # ------------------------------------------------------------------
    # Serialisation (Project Save / Load)
    # ------------------------------------------------------------------

    def get_params(self) -> dict:
        """
        Return all serialisable plugin parameters as a plain dict.

        Only scalar types (int, float, bool, str) are included so that
        the result can be safely written to JSON.  Private attributes
        (underscore prefix) and Qt widget objects are excluded.
        Subclasses may override this to add extra keys (e.g. file paths
        stored with a leading underscore for encapsulation reasons).
        """
        SKIP = {"_on_changed"}
        out: dict = {}
        for k, v in self.__dict__.items():
            if k in SKIP or callable(v):
                continue
            if isinstance(v, (int, float, bool, str)):
                out[k] = v
        return out

    def set_params(self, params: dict) -> None:
        """
        Restore scalar parameters from a dict produced by get_params().

        Only keys that already exist on the instance are updated so
        unexpected JSON keys cannot inject arbitrary attributes.
        Widget state (sliders etc.) is NOT automatically refreshed —
        subclasses must override this to also update their UI.
        """
        for k, v in params.items():
            if hasattr(self, k) and isinstance(v, (int, float, bool, str)):
                setattr(self, k, v)
