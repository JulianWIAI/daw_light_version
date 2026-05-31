"""
audio_fx_chain.py -- Dynamic DSP Effect Chain for Audio File Tracks.
=====================================================================
Replaces the old hardcoded-parameter dataclass with a dynamic ordered list
of FxPluginBase instances so the chain is fully determined by whatever
plugins the user has loaded into the FX rack slots.

Architecture:
    AudioFxChain stores:
        - Routing parameters: volume, pan, muted, soloed
          (applied by AudioFilePlayer after plugin processing)
        - A Python list of FxPluginBase instances that represent the
          user's current effect rack.

    AudioFilePlayer._load_and_play() takes a snapshot of the plugin list
    and runs each active (non-bypassed) plugin sequentially on the audio
    buffer before applying volume/pan.

    Memory management:
        AudioFxChain.remove_plugin() pops the instance from the list.
        Once no other reference holds it, the GC frees it.  C++ processors
        (held in FxPluginBase._processor) are freed when the plugin object
        is collected because pybind11 objects are reference-counted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class AudioFxChain:
    """
    Per-audio-track routing parameters plus an ordered list of effect plugins.

    Attributes:
        track_id : AudioTrack.track_id this chain belongs to.
        volume   : Output gain multiplier (1.0 = unity, 0.0 = silence).
        pan      : Stereo position (−1.0 = full left, +1.0 = full right).
        muted    : When True the track output is silenced.
        soloed   : Signals the player that only soloed tracks should play.
        plugins  : Ordered list of FxPluginBase instances.  Each entry must
                   have a process(audio, sample_rate) method.  Empty list =
                   no DSP applied (pass-through).
    """

    track_id: int

    # Routing (these are applied by AudioFilePlayer, not by this class).
    volume: float = 1.0
    pan:    float = 0.0
    muted:  bool  = False
    soloed: bool  = False

    # Dynamic plugin list -- populated via the FX rack GUI.
    plugins: List = field(default_factory=list)

    # Automation envelopes: target_key → AutomationEnvelope.
    # Keys are "volume", "pan", or "PluginDisplayName.param_name".
    # Written by AutomationLane; read by apply_automation() every ~20 Hz.
    # Using plain dict so audio_fx_chain.py does not import Qt-dependent modules.
    envelopes: dict = field(default_factory=dict)

    # -------------------------------------------------------------------------
    # DSP processing
    # -------------------------------------------------------------------------

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """
        Pass the audio buffer through every active (non-bypassed) plugin in
        insertion order.

        Called from AudioFilePlayer's background render thread.  A shallow
        copy of the plugin list is taken at the start so the GUI thread can
        safely add or remove slots while a render is in progress.

        Args:
            audio       : float32 ndarray, shape (n_samples, n_channels).
            sample_rate : Sample rate in Hz (typically 44100).

        Returns:
            Processed float32 ndarray, same shape as input.
        """
        # Snapshot prevents IndexError if the GUI thread modifies the list
        # concurrently (the GIL makes list() atomic enough for this use case).
        for plugin in list(self.plugins):
            if plugin is None:
                continue
            if not plugin.enabled:
                continue  # plugin is bypassed -- skip without touching audio
            try:
                audio = plugin.process(audio, sample_rate)
            except Exception as exc:
                logger.warning(
                    "AudioFxChain track=%d: plugin '%s' raised %s",
                    self.track_id, getattr(plugin, "DISPLAY_NAME", "?"), exc,
                )
        return audio

    # -------------------------------------------------------------------------
    # Volume / Pan (unchanged from original -- applied after DSP)
    # -------------------------------------------------------------------------

    def apply_gain_pan(self, audio_data: np.ndarray, n_channels: int = 2) -> np.ndarray:
        """
        Apply volume and stereo pan to a float32 (samples, channels) array.

        Pan uses a simple linear law: the louder side stays at the volume
        multiplier while the quieter side is attenuated proportionally.

        Returns an all-zeros array if muted.
        """
        if self.muted:
            return np.zeros_like(audio_data)

        # Skip copy when no processing is needed.
        if self.volume == 1.0 and self.pan == 0.0:
            return audio_data

        out = audio_data.copy() * float(self.volume)

        if n_channels == 2 and self.pan != 0.0:
            left_gain  = max(0.0, min(1.0, 1.0 - self.pan))
            right_gain = max(0.0, min(1.0, 1.0 + self.pan))
            out[:, 0] *= left_gain
            out[:, 1] *= right_gain

        return out

    # -------------------------------------------------------------------------
    # Plugin list helpers (called by FxRackWidget)
    # -------------------------------------------------------------------------

    def add_plugin(self, plugin) -> None:
        """Append a plugin to the end of the insert chain."""
        self.plugins.append(plugin)

    def remove_plugin(self, index: int) -> None:
        """
        Remove the plugin at *index* from the chain.

        After this call the list reference is gone.  If no other object holds
        a reference to the plugin instance the GC will collect it, freeing any
        associated C++ processor memory.
        """
        if 0 <= index < len(self.plugins):
            self.plugins.pop(index)

    def move_plugin(self, from_index: int, to_index: int) -> None:
        """Reorder the chain by moving a plugin to a new position."""
        n = len(self.plugins)
        if 0 <= from_index < n and 0 <= to_index < n:
            plugin = self.plugins.pop(from_index)
            self.plugins.insert(to_index, plugin)

    # -------------------------------------------------------------------------
    # Automation envelope application
    # -------------------------------------------------------------------------

    def apply_automation(self, beat_pos: float) -> None:
        """
        Evaluate every stored AutomationEnvelope at beat_pos and write the
        resulting values to chain parameters immediately.

        Called from the GUI thread at ~20 Hz (MainWindow._on_refresh_tick).
        No dynamic memory allocation occurs on the C++ side because each
        affected setter (e.g. Flanger.set_rate) only stores a float.

        The envelopes dict is duck-typed: any object with evaluate(float) → float
        and a truthy .nodes attribute is accepted.
        """
        for key, env in list(self.envelopes.items()):  # snapshot for thread safety
            if not getattr(env, "nodes", None):
                continue
            try:
                value = float(env.evaluate(beat_pos))
            except Exception:
                continue

            if key == "volume":
                # Clamp to a safe range to prevent digital clipping surprises
                self.volume = max(0.0, min(4.0, value))

            elif key == "pan":
                self.pan = max(-1.0, min(1.0, value))

            else:
                # Plugin parameter encoded as "PluginDisplayName.param_name"
                parts = key.split(".", 1)
                if len(parts) != 2:
                    continue
                plugin_name, param_name = parts
                for plugin in list(self.plugins):
                    if plugin is None:
                        continue
                    if getattr(plugin, "DISPLAY_NAME", "") != plugin_name:
                        continue
                    if hasattr(plugin, param_name):
                        setattr(plugin, param_name, value)
                        # Forward to the C++ backend without any heap allocation.
                        # The setter convention in bindings.cpp is set_<param_name>.
                        proc = getattr(plugin, "_proc", None)
                        if proc is not None:
                            cpp_setter = getattr(proc, f"set_{param_name}", None)
                            if callable(cpp_setter):
                                try:
                                    cpp_setter(value)
                                except Exception:
                                    pass
                    break  # Only update the first slot with this plugin name
