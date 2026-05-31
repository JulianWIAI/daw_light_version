"""
fx_plugin_registry.py -- Central registry of all available FX rack plugins.
============================================================================
PLUGIN_REGISTRY maps display names to plugin classes.
PLUGIN_CATEGORIES groups them for the slot picker dropdown menu.

To add a new effect:
  1. Implement a FxPluginBase subclass (in a new or existing plugins file).
  2. Import the class below and add it to PLUGIN_REGISTRY and PLUGIN_CATEGORIES.
"""

from __future__ import annotations

from typing import Dict, List, Type

from .fx_plugin_base import FxPluginBase
from .fx_plugins_pedalboard import (
    EQPlugin,
    ReverbPlugin,
    CompressorPlugin,
    ChorusPlugin,
)
from .fx_plugins_cpp import (
    BrickwallLimiterPlugin,
    MultibandCompressorPlugin,
    DynamicEQPlugin,
    DeEsserPlugin,
    TransientShaperPlugin,
    GateExpanderPlugin,
)
from .fx_plugins_spatial import (
    DelayEchoPlugin,
    FlangerPlugin,
    PhaserPlugin,
    StereoImagerPlugin,
)
from .fx_plugins_harmonic import (
    SaturationPlugin,
    OverdrivePlugin,
    BitcrusherPlugin,
    ExciterPlugin,
)
from .ai_smart_eq import SmartEQPlugin
from .ai_generative_reverb import GenerativeReverbPlugin
from .ai_smart_master import SmartMasterPlugin
from .fx_plugins_pitch import PitchCorrectorPlugin, PitchShifterPlugin
from .fx_plugins_filter import AutoFilterPlugin
from .fx_plugins_sampler import SamplerPlugin
from .fx_plugins_loudness import LoudnessAutomationPlugin
from .fx_plugins_spectral_panning import SpectralPanningPlugin

# ---------------------------------------------------------------------------
# Master plugin registry: display_name -> class
# ---------------------------------------------------------------------------

PLUGIN_REGISTRY: Dict[str, Type[FxPluginBase]] = {
    # --- Legacy pedalboard effects ---
    EQPlugin.DISPLAY_NAME:         EQPlugin,
    ReverbPlugin.DISPLAY_NAME:     ReverbPlugin,
    CompressorPlugin.DISPLAY_NAME: CompressorPlugin,
    ChorusPlugin.DISPLAY_NAME:     ChorusPlugin,

    # --- C++ dynamics processors ---
    BrickwallLimiterPlugin.DISPLAY_NAME:     BrickwallLimiterPlugin,
    MultibandCompressorPlugin.DISPLAY_NAME:  MultibandCompressorPlugin,
    DynamicEQPlugin.DISPLAY_NAME:            DynamicEQPlugin,
    DeEsserPlugin.DISPLAY_NAME:              DeEsserPlugin,
    TransientShaperPlugin.DISPLAY_NAME:      TransientShaperPlugin,
    GateExpanderPlugin.DISPLAY_NAME:         GateExpanderPlugin,

    # --- C++ spatial / time-based effects ---
    DelayEchoPlugin.DISPLAY_NAME:    DelayEchoPlugin,
    FlangerPlugin.DISPLAY_NAME:      FlangerPlugin,
    PhaserPlugin.DISPLAY_NAME:       PhaserPlugin,
    StereoImagerPlugin.DISPLAY_NAME: StereoImagerPlugin,

    # --- C++ harmonic & character processors ---
    SaturationPlugin.DISPLAY_NAME: SaturationPlugin,
    OverdrivePlugin.DISPLAY_NAME:  OverdrivePlugin,
    BitcrusherPlugin.DISPLAY_NAME: BitcrusherPlugin,
    ExciterPlugin.DISPLAY_NAME:    ExciterPlugin,

    # --- AI-powered processors ---
    SmartEQPlugin.DISPLAY_NAME:        SmartEQPlugin,
    GenerativeReverbPlugin.DISPLAY_NAME: GenerativeReverbPlugin,
    SmartMasterPlugin.DISPLAY_NAME:    SmartMasterPlugin,

    # --- Advanced utilities & specialty filters ---
    PitchCorrectorPlugin.DISPLAY_NAME: PitchCorrectorPlugin,
    PitchShifterPlugin.DISPLAY_NAME:   PitchShifterPlugin,
    AutoFilterPlugin.DISPLAY_NAME:     AutoFilterPlugin,

    # --- Loudness & gain automation ---
    LoudnessAutomationPlugin.DISPLAY_NAME: LoudnessAutomationPlugin,

    # --- Spectral panning & masking resolution ---
    SpectralPanningPlugin.DISPLAY_NAME: SpectralPanningPlugin,

    # --- Instruments ---
    SamplerPlugin.DISPLAY_NAME: SamplerPlugin,
}

# ---------------------------------------------------------------------------
# Grouped categories for the picker menu (order preserved -- Python 3.7+)
# ---------------------------------------------------------------------------

PLUGIN_CATEGORIES: Dict[str, List[str]] = {
    "EQ & Tone": [
        EQPlugin.DISPLAY_NAME,
        DynamicEQPlugin.DISPLAY_NAME,
    ],
    "Dynamics": [
        CompressorPlugin.DISPLAY_NAME,
        BrickwallLimiterPlugin.DISPLAY_NAME,
        MultibandCompressorPlugin.DISPLAY_NAME,
        GateExpanderPlugin.DISPLAY_NAME,
        DeEsserPlugin.DISPLAY_NAME,
        TransientShaperPlugin.DISPLAY_NAME,
        LoudnessAutomationPlugin.DISPLAY_NAME,
    ],
    "Ambience": [
        ReverbPlugin.DISPLAY_NAME,
        ChorusPlugin.DISPLAY_NAME,
    ],
    "Spatial": [
        DelayEchoPlugin.DISPLAY_NAME,
        FlangerPlugin.DISPLAY_NAME,
        PhaserPlugin.DISPLAY_NAME,
        StereoImagerPlugin.DISPLAY_NAME,
        SpectralPanningPlugin.DISPLAY_NAME,
    ],
    "Harmonic": [
        SaturationPlugin.DISPLAY_NAME,
        OverdrivePlugin.DISPLAY_NAME,
        BitcrusherPlugin.DISPLAY_NAME,
        ExciterPlugin.DISPLAY_NAME,
    ],
    "AI": [
        SmartEQPlugin.DISPLAY_NAME,
        GenerativeReverbPlugin.DISPLAY_NAME,
        SmartMasterPlugin.DISPLAY_NAME,
    ],
    "Pitch & Tune": [
        PitchCorrectorPlugin.DISPLAY_NAME,
        PitchShifterPlugin.DISPLAY_NAME,
    ],
    "Filters": [
        AutoFilterPlugin.DISPLAY_NAME,
    ],
    "Instruments": [
        SamplerPlugin.DISPLAY_NAME,
    ],
}
