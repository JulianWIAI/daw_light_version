"""
dspreset_parser.py -- XML parser for Decent Sampler (.dspreset) files.
=======================================================================
Responsibility: parse XML only. Produces pure Python data classes that
are then passed to the C++ DecentSamplerEngine for all audio work.

No audio logic lives here -- this module only reads, validates and
structures the metadata so the rest of the system (C++ engine, GUI panel)
can consume it without touching XML directly.

Public API
----------
  parse_dspreset(path: str) -> DsInstrumentInfo

Data model hierarchy
--------------------
  DsInstrumentInfo
    ├── ui_elements : List[DsUIElement]   (knobs / sliders / buttons)
    ├── groups      : List[DsGroup]
    │     └── zones : List[DsSampleZone]
    └── zones       : List[DsSampleZone]  (flat list for C++ consumption)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Data classes  (mirrored in C++ DsZoneData / DsUIElementData structs)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DsUIElement:
    """
    One UI control element from the <ui> block.
    Passed to DsPresetPanel to create matching Qt widgets.
    """
    element_type:   str         # "labeled-knob" | "slider" | "button"
    label:          str         # Text label shown on the widget
    x:              int         # X position on the original DS canvas (pixels)
    y:              int         # Y position
    width:          int         # Widget width in pixels
    height:         int         # Widget height in pixels
    min_value:      float       # Minimum parameter value
    max_value:      float       # Maximum parameter value
    default_value:  float       # Value applied when the preset loads
    parameter_name: str         # Internal identifier, e.g. "ENV_ATTACK"
    cc:             Optional[int]   = None  # MIDI CC for hardware automation
    step_size:      Optional[float] = None  # Quantisation step (None = smooth)


@dataclass
class DsSampleZone:
    """
    One <sample> element — metadata for one WAV file and its mapping.
    The C++ engine reads these fields to load the WAV and set up voices.
    """
    path:          str          # WAV path relative to the .dspreset directory
    root_note:     int          # MIDI note the sample was recorded at
    lo_note:       int          # Lowest MIDI note this zone covers
    hi_note:       int          # Highest MIDI note this zone covers
    lo_vel:        int          # Lowest velocity trigger (0–127)
    hi_vel:        int          # Highest velocity trigger (0–127)
    volume_db:     float = 0.0  # Volume trim in dB (0 = unity)
    pan:           float = 0.0  # Stereo pan: -100 (L) … 0 (C) … +100 (R)
    # ADSR envelope parameters passed to the C++ voice renderer.
    attack:        float = 0.002   # Attack  time in seconds
    decay:         float = 0.1     # Decay   time in seconds
    sustain:       float = 1.0     # Sustain level 0.0–1.0
    release:       float = 0.3     # Release time in seconds
    # Sample loop (used by C++ to loop the WAV within a voice).
    loop_enabled:  bool  = False
    loop_start:    int   = 0       # Loop start, in sample frames
    loop_end:      int   = -1      # Loop end (-1 means end-of-file)
    # Round-robin sequencing: the C++ engine tracks seq_position per group.
    seq_position:  int   = 1       # This zone's 1-based RR position
    seq_length:    int   = 1       # Total RR alternatives in the group
    trigger:       str   = "attack"  # "attack" | "release" | "first" | "legato"


@dataclass
class DsGroup:
    """
    One <group> element.  Groups carry default values that zones inherit;
    the C++ engine uses the flat zone list so DsGroup is GUI / parse only.
    """
    zones:     List[DsSampleZone] = field(default_factory=list)
    lo_note:   int   = 0
    hi_note:   int   = 127
    lo_vel:    int   = 0
    hi_vel:    int   = 127
    volume_db: float = 0.0
    attack:    float = 0.002
    decay:     float = 0.1
    sustain:   float = 1.0
    release:   float = 0.3
    trigger:   str   = "attack"


@dataclass
class DsInstrumentInfo:
    """
    Top-level descriptor returned by parse_dspreset().

    The C++ engine receives zones (flat list + absolute base_dir).
    The GUI panel receives ui_elements.
    """
    name:        str                = ""
    path:        str                = ""       # Absolute path to .dspreset file
    base_dir:    str                = ""       # Directory containing the file
    ui_width:    int                = 812
    ui_height:   int                = 375
    ui_elements: List[DsUIElement]  = field(default_factory=list)
    groups:      List[DsGroup]      = field(default_factory=list)
    zones:       List[DsSampleZone] = field(default_factory=list)  # flat
    num_zones:   int                = 0
    num_groups:  int                = 0
    # CC → parameter_name for real-time MIDI automation.
    cc_map:      Dict[int, str]     = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# Internal parse helpers
# ═══════════════════════════════════════════════════════════════════════════════

# Semitone offsets for the seven note letter names (C = 0).
_SEMITONES: Dict[str, int] = {
    "c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11,
}


def _note(raw: str) -> int:
    """
    Parse a note name ('C3', 'F#4', 'Bb2') or a plain integer string into a
    MIDI note number (0–127).  Returns -1 on failure.

    C-1 = 0 convention: (octave + 1) * 12 + semitone.
    """
    s = raw.strip()
    try:
        return max(0, min(127, int(s)))
    except ValueError:
        pass
    if not s:
        return -1
    letter = s[0].lower()
    if letter not in _SEMITONES:
        return -1
    semi = _SEMITONES[letter]
    i = 1
    if i < len(s) and s[i] == "#":
        semi += 1; i += 1
    elif i < len(s) and s[i].lower() == "b":
        semi -= 1; i += 1
    try:
        oct_ = int(s[i:])
        return max(0, min(127, (oct_ + 1) * 12 + semi))
    except (ValueError, IndexError):
        return -1


def _vol_db(raw: str) -> float:
    """
    Parse a DS volume string to dB.
    Accepts 'XdB', 'Xdb', or a plain float (interpreted as linear gain
    when in the 0–4 range, otherwise treated as already in dB).
    """
    s = raw.strip()
    if s.lower().endswith("db"):
        try:
            return float(s[:-2])
        except ValueError:
            return 0.0
    try:
        v = float(s)
        if 0.0 < v <= 4.0:
            import math
            return 20.0 * math.log10(max(v, 1e-9))
        return v
    except ValueError:
        return 0.0


def _f(elem: ET.Element, key: str, default: float) -> float:
    """Read a float XML attribute, returning default on missing or bad value."""
    raw = elem.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _i(elem: ET.Element, key: str, default: int) -> int:
    """Read an int XML attribute that may also be a note name."""
    raw = elem.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        v = _note(raw)
        return v if v >= 0 else default


# ═══════════════════════════════════════════════════════════════════════════════
# UI parser
# ═══════════════════════════════════════════════════════════════════════════════

_UI_TAGS = {"labeled-knob", "knob", "slider", "button"}


def _parse_ui(ui_elem: ET.Element, info: DsInstrumentInfo) -> None:
    """
    Collect every recognised control widget from <ui> (including nested
    <tab> children) and append DsUIElement entries to info.ui_elements.
    Builds info.cc_map as a side effect.
    """
    info.ui_width  = int(ui_elem.get("width",  812))
    info.ui_height = int(ui_elem.get("height", 375))

    # DS controls can live directly under <ui> or inside <tab> children.
    candidates = [ui_elem] + list(ui_elem.iter("tab"))
    for parent in candidates:
        for child in parent:
            tag = child.tag.lower()
            if tag not in _UI_TAGS:
                continue
            # Unify "knob" → "labeled-knob".
            etype = "labeled-knob" if tag in ("labeled-knob", "knob") else tag

            label  = child.get("label", child.get("text", ""))
            min_v  = float(child.get("minValue", 0.0))
            max_v  = float(child.get("maxValue", 1.0))
            def_v  = float(child.get("value",
                           child.get("defaultValue", str(min_v))))
            param  = child.get("parameterName",
                     child.get("parameter", label))

            cc_raw = child.get("cc", child.get("midiCC"))
            cc: Optional[int] = None
            if cc_raw is not None:
                try:
                    cc = int(cc_raw)
                except ValueError:
                    pass

            step_raw = child.get("stepSize")
            step: Optional[float] = float(step_raw) if step_raw else None

            info.ui_elements.append(DsUIElement(
                element_type=etype, label=label,
                x=int(child.get("x", 0)), y=int(child.get("y", 0)),
                width=int(child.get("width", 80)),
                height=int(child.get("height", 80)),
                min_value=min_v, max_value=max_v, default_value=def_v,
                parameter_name=param, cc=cc, step_size=step,
            ))
            if cc is not None:
                info.cc_map[cc] = param


# ═══════════════════════════════════════════════════════════════════════════════
# Sample mapping parser
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_sample(elem: ET.Element, grp: DsGroup) -> DsSampleZone:
    """
    Build a DsSampleZone from one <sample> element.
    Any attribute absent on the sample is inherited from the parent DsGroup.
    """
    path     = elem.get("path", elem.get("file", ""))
    root_raw = elem.get("rootNote", elem.get("root", "60"))
    root     = _note(root_raw) if not root_raw.lstrip("-").isdigit() \
               else int(root_raw)
    if root < 0:
        root = 60  # Middle C fallback.

    return DsSampleZone(
        path      = path,
        root_note = root,
        lo_note   = _i(elem, "loNote", grp.lo_note),
        hi_note   = _i(elem, "hiNote", grp.hi_note),
        lo_vel    = _i(elem, "loVel",  grp.lo_vel),
        hi_vel    = _i(elem, "hiVel",  grp.hi_vel),
        volume_db = _vol_db(elem.get("volume", "0"))
                    if elem.get("volume") is not None else grp.volume_db,
        pan       = _f(elem, "pan", 0.0),
        attack    = _f(elem, "attack",  grp.attack),
        decay     = _f(elem, "decay",   grp.decay),
        sustain   = _f(elem, "sustain", grp.sustain),
        release   = _f(elem, "release", grp.release),
        loop_enabled = elem.get("loopEnabled", "false").lower() == "true",
        loop_start   = _i(elem, "loopStart", 0),
        loop_end     = _i(elem, "loopEnd",  -1),
        seq_position = _i(elem, "seqPosition", 1),
        seq_length   = _i(elem, "seqLength",   1),
        trigger      = elem.get("trigger", grp.trigger),
    )


def _parse_groups(root_elem: ET.Element, info: DsInstrumentInfo) -> None:
    """
    Walk <groups> → <group> → <sample> and fill info.groups and info.zones.
    Some presets omit the <groups> wrapper and place <group> directly under
    the document root, so both layouts are handled.
    """
    container = root_elem.find("groups")
    search    = container if container is not None else root_elem

    # Global ADSR defaults on the <groups> element (inherited by all groups).
    g_attack  = _f(search, "attack",  0.002)
    g_decay   = _f(search, "decay",   0.1)
    g_sustain = _f(search, "sustain", 1.0)
    g_release = _f(search, "release", 0.3)

    for grp_elem in search.findall("group"):
        grp = DsGroup(
            lo_note   = _i(grp_elem, "loNote",  0),
            hi_note   = _i(grp_elem, "hiNote",  127),
            lo_vel    = _i(grp_elem, "loVel",   0),
            hi_vel    = _i(grp_elem, "hiVel",   127),
            volume_db = _vol_db(grp_elem.get("volume", "0")),
            attack    = _f(grp_elem, "attack",  g_attack),
            decay     = _f(grp_elem, "decay",   g_decay),
            sustain   = _f(grp_elem, "sustain", g_sustain),
            release   = _f(grp_elem, "release", g_release),
            trigger   = grp_elem.get("trigger", "attack"),
        )
        for sample_elem in grp_elem.findall("sample"):
            zone = _parse_sample(sample_elem, grp)
            grp.zones.append(zone)
            info.zones.append(zone)
        info.groups.append(grp)

    info.num_groups = len(info.groups)
    info.num_zones  = len(info.zones)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def parse_dspreset(path: str) -> DsInstrumentInfo:
    """
    Parse a Decent Sampler .dspreset file and return a DsInstrumentInfo.

    Never raises — on any error a partial DsInstrumentInfo is returned
    and the failure is logged.  The C++ engine call site should check
    info.num_zones > 0 before proceeding.
    """
    info          = DsInstrumentInfo()
    info.path     = os.path.abspath(path)
    info.base_dir = os.path.dirname(info.path)
    info.name     = os.path.splitext(os.path.basename(path))[0]

    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except (ET.ParseError, OSError) as exc:
        logger.error("parse_dspreset: cannot read '%s': %s", path, exc)
        return info

    ui_elem = root.find("ui")
    if ui_elem is not None:
        _parse_ui(ui_elem, info)

    _parse_groups(root, info)

    logger.debug(
        "parse_dspreset: '%s' → %d zones / %d groups / "
        "%d UI elements / %d CC bindings",
        info.name, info.num_zones, info.num_groups,
        len(info.ui_elements), len(info.cc_map),
    )
    return info
