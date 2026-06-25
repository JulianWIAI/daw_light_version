"""
instrument_randomizer.py — Role-Based Soundfont Instrument Randomizer
=======================================================================
Scans a Soundfonts/ directory for categorized .sf2/.sfz files and maps
the 10 fixed DAW track roles to appropriate instruments at random.

Expected directory layout (auto-detected, case-insensitive):

    soundfonts/
        Drums/         →  01_Kick, 02_Percussion
        Bass/          →  03_Bass
        808/           →  03_Bass  (merged with Bass/)
        Leads/         →  04_Melody, 07_Arp, 09_Texture
        Plucks/        →  04_Melody, 07_Arp
        Keys/          →  04_Melody, 05_Chords, 07_Arp, 08_Stabs
        Piano/         →  04_Melody, 05_Chords
        Orchestral/    →  04_Melody, 09_Texture
        Strings/       →  04_Melody, 09_Texture
        Synths/        →  05_Chords, 06_Pad, 08_Stabs
        Pads/          →  06_Pad
        Organs/        →  05_Chords, 08_Stabs
        FX/            →  10_FX
        SFX/           →  10_FX
        Effects/       →  10_FX
        Percussion/    →  10_FX  (melodic percussion, not drum kits)

Fallback (single flat SF2 like GeneralUser-GS.sf2): uses GM preset ranges
appropriate for each role — yields maximum variety without extra files.

Usage
-----
    lib = SoundfontLibrary(scan_paths)
    randomizer = InstrumentRandomizer(lib)
    for track in midi.get_all_tracks():
        result = randomizer.pick(track.name)
        if result:
            sf2, bank, preset, name = result
            plugin = InstrumentPlugin(name=name, sf2_path=sf2,
                                      bank=bank, preset=preset,
                                      channel=track.channel)
            engine.register_instrument(plugin)
"""

from __future__ import annotations

import logging
import os
import random
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# (preset, bank, display_name)
_InstrumentEntry = Tuple[int, int, str]

# ── GM preset pools per role (used as fallback with a single SF2) ─────────────

_GM_DRUM_KITS: List[_InstrumentEntry] = [
    (0,  128, "Standard Kit"),
    (8,  128, "Room Kit"),
    (16, 128, "Power Kit"),
    (24, 128, "Electronic Kit"),
    (25, 128, "TR-808 Kit"),
    (32, 128, "Jazz Kit"),
    (40, 128, "Brush Kit"),
    (48, 128, "Orchestral Kit"),
    (56, 128, "SFX Kit"),
]

# Flat tuples: (preset, bank, name)
_GM_POOLS: Dict[str, List[_InstrumentEntry]] = {
    "drums": _GM_DRUM_KITS,

    "bass": [
        (32, 0, "Acoustic Bass"),
        (33, 0, "Electric Bass (finger)"),
        (34, 0, "Electric Bass (pick)"),
        (35, 0, "Fretless Bass"),
        (36, 0, "Slap Bass 1"),
        (37, 0, "Slap Bass 2"),
        (38, 0, "Synth Bass 1"),
        (39, 0, "Synth Bass 2"),
    ],

    "melody": [
        # Piano
        (0,  0, "Acoustic Grand Piano"), (4, 0, "Electric Piano 1"),
        (6,  0, "Harpsichord"),
        # Chromatic Perc
        (9,  0, "Glockenspiel"), (11, 0, "Vibraphone"), (12, 0, "Marimba"),
        (14, 0, "Tubular Bells"),
        # Strings
        (40, 0, "Violin"), (42, 0, "Cello"), (45, 0, "Pizzicato Strings"),
        (46, 0, "Orchestral Harp"),
        # Brass / Reed / Pipe
        (56, 0, "Trumpet"), (60, 0, "French Horn"),
        (64, 0, "Soprano Sax"), (65, 0, "Alto Sax"),
        (72, 0, "Piccolo"), (73, 0, "Flute"), (79, 0, "Ocarina"),
        # Synth Lead
        (80, 0, "Square Wave Lead"), (81, 0, "Sawtooth Lead"),
        (82, 0, "Calliope Lead"), (84, 0, "Charang Lead"),
        (86, 0, "Fifths Lead"),
        # Guitar
        (24, 0, "Nylon Guitar"), (25, 0, "Steel Guitar"),
        (26, 0, "Jazz Guitar"), (27, 0, "Clean Guitar"),
    ],

    "chords_pad": [
        # Synth Pad
        (88, 0, "New Age Pad"), (89, 0, "Warm Pad"), (90, 0, "Polysynth Pad"),
        (91, 0, "Choir Pad"),   (92, 0, "Bowed Pad"), (93, 0, "Metallic Pad"),
        (94, 0, "Halo Pad"),    (95, 0, "Sweep Pad"),
        # Organ
        (16, 0, "Drawbar Organ"), (17, 0, "Percussive Organ"),
        (18, 0, "Rock Organ"),    (19, 0, "Church Organ"),
        # Piano / Ensemble
        (0,  0, "Acoustic Grand Piano"), (48, 0, "String Ensemble 1"),
        (52, 0, "Choir Aahs"),  (54, 0, "Synth Choir"),
        # Synth Lead (for stabs)
        (80, 0, "Square Wave Lead"), (81, 0, "Sawtooth Lead"),
        (87, 0, "Bass + Lead"),
    ],

    "fx": [
        # Synth Effects
        (96, 0, "Rain"),       (97, 0, "Soundtrack"), (98, 0, "Crystal"),
        (99, 0, "Atmosphere"), (100, 0, "Brightness"), (101, 0, "Goblins"),
        (102, 0, "Echoes"),    (103, 0, "Sci-fi"),
        # Percussive
        (112, 0, "Tinkle Bell"), (113, 0, "Agogo"), (114, 0, "Steel Drums"),
        (116, 0, "Taiko Drum"),  (118, 0, "Synth Drum"), (119, 0, "Reverse Cymbal"),
        # Sound Effects
        (122, 0, "Seashore"),    (125, 0, "Helicopter"), (127, 0, "Gunshot"),
    ],
}

# ── Track-name → role mapping ─────────────────────────────────────────────────

# Each entry: (prefix_pattern_lower, role_key)
# Checked in order; first match wins.
_TRACK_ROLE_TABLE = [
    ("01_kick",      "drums"),
    ("kick",         "drums"),
    ("02_perc",      "drums"),
    ("percussion",   "drums"),
    ("03_bass",      "bass"),
    ("bass",         "bass"),
    ("04_melody",    "melody"),
    ("melody",       "melody"),
    ("07_arp",       "melody"),
    ("arp",          "melody"),
    ("09_texture",   "melody"),
    ("texture",      "melody"),
    ("05_chord",     "chords_pad"),
    ("chord",        "chords_pad"),
    ("06_pad",       "chords_pad"),
    ("pad",          "chords_pad"),
    ("08_stab",      "chords_pad"),
    ("stab",         "chords_pad"),
    ("10_fx",        "fx"),
    ("_fx",          "fx"),
]

# ── Folder → role mapping (case-insensitive subfolder names) ─────────────────

_FOLDER_ROLE_MAP: Dict[str, List[str]] = {
    "drums":      ["drums"],
    "drum":       ["drums"],
    "808":        ["bass"],
    "bass":       ["bass"],
    "leads":      ["melody"],
    "lead":       ["melody"],
    "plucks":     ["melody"],
    "pluck":      ["melody"],
    "orchestral": ["melody"],
    "strings":    ["melody"],
    "piano":      ["melody", "chords_pad"],
    "keys":       ["melody", "chords_pad"],
    "synths":     ["chords_pad"],
    "synth":      ["chords_pad"],
    "pads":       ["chords_pad"],
    "organs":     ["chords_pad"],
    "organ":      ["chords_pad"],
    "fx":         ["fx"],
    "sfx":        ["fx"],
    "effects":    ["fx"],
    "effect":     ["fx"],
    "percussion": ["fx"],
}

_SF2_EXTS = {".sf2", ".sfz"}


# ── SoundfontLibrary ──────────────────────────────────────────────────────────

class SoundfontLibrary:
    """
    Scans one or more root directories for soundfont files organized into
    role-tagged subfolders.  Falls back to flat GM-preset pools when no
    categorised files are found.

    Attributes
    ----------
    role_files : Dict[str, List[str]]
        Maps role key → list of absolute SF2/SFZ paths.
    flat_sf2 : Optional[str]
        Path to a general-purpose SF2 used for GM-pool fallback, or None.
    """

    def __init__(self, scan_paths: List[str]) -> None:
        self.role_files: Dict[str, List[str]] = {k: [] for k in _GM_POOLS}
        self.flat_sf2:   Optional[str]        = None
        self._scan(scan_paths)

    def _scan(self, paths: List[str]) -> None:
        flat_candidates: List[str] = []

        for root in paths:
            if not os.path.isdir(root):
                continue
            try:
                entries = os.listdir(root)
            except PermissionError:
                continue

            for entry in sorted(entries):
                full = os.path.join(root, entry)
                if os.path.isdir(full):
                    # Check if it's a recognized role folder.
                    key = entry.lower()
                    roles = _FOLDER_ROLE_MAP.get(key)
                    if roles:
                        for fname in sorted(os.listdir(full)):
                            if os.path.splitext(fname)[1].lower() in _SF2_EXTS:
                                fpath = os.path.normpath(os.path.join(full, fname))
                                for role in roles:
                                    if fpath not in self.role_files[role]:
                                        self.role_files[role].append(fpath)
                else:
                    ext = os.path.splitext(entry)[1].lower()
                    if ext in _SF2_EXTS:
                        flat_candidates.append(os.path.normpath(full))

        # Assign a flat fallback SF2 (first found, prefer GeneralUser-GS).
        for path in flat_candidates:
            if "generaluser" in os.path.basename(path).lower():
                self.flat_sf2 = path
                break
        if self.flat_sf2 is None and flat_candidates:
            self.flat_sf2 = flat_candidates[0]

        total_cat = sum(len(v) for v in self.role_files.values())
        logger.info(
            "SoundfontLibrary: %d categorised file(s), flat_sf2=%s",
            total_cat, self.flat_sf2,
        )

    def has_categorised(self) -> bool:
        return any(self.role_files.values())

    def pick_file(self, role: str) -> Optional[str]:
        """Return a random SF2/SFZ path for *role*, or None."""
        files = self.role_files.get(role, [])
        return random.choice(files) if files else None


# ── InstrumentRandomizer ──────────────────────────────────────────────────────

class InstrumentRandomizer:
    """
    Picks a random instrument (sf2_path, bank, preset, name) for a track
    based on the track name's embedded role.

    Strategy
    --------
    1.  If the library has categorised role-folders, pick a random SF2 from
        the matching folder.  Preset = 0 (first patch) since internal presets
        of unknown SF2 files cannot be enumerated without loading them.
    2.  Otherwise use the flat GM fallback SF2 with a random preset drawn
        from the GM pool for that role.
    3.  If no SF2 is found at all, returns None.
    """

    def __init__(self, library: SoundfontLibrary) -> None:
        self._lib = library
        # Track last picks so we can guarantee uniqueness across 10 tracks.
        self._used_presets: Dict[str, set] = {k: set() for k in _GM_POOLS}

    def reset_uniqueness(self) -> None:
        """Call before a fresh randomization pass to allow full-pool reuse."""
        for s in self._used_presets.values():
            s.clear()

    @staticmethod
    def detect_role(track_name: str) -> str:
        """Return the role key for a track name, or 'melody' as catch-all."""
        lower = track_name.lower()
        for pattern, role in _TRACK_ROLE_TABLE:
            if pattern in lower:
                return role
        return "melody"

    def pick(
        self, track_name: str
    ) -> Optional[Tuple[str, int, int, str]]:
        """
        Return ``(sf2_path, bank, preset, display_name)`` for *track_name*,
        or ``None`` if no soundfont is available.
        """
        role = self.detect_role(track_name)
        lib  = self._lib

        # ── Path A: categorised folder library ────────────────────────────────
        if lib.has_categorised():
            sf2 = lib.pick_file(role)
            if sf2 and os.path.isfile(sf2):
                basename = os.path.splitext(os.path.basename(sf2))[0]
                bank     = 128 if role == "drums" else 0
                return sf2, bank, 0, basename
            # Folder present but empty → fall through to GM pool if possible.

        # ── Path B: GM pool from flat SF2 ─────────────────────────────────────
        if lib.flat_sf2 and os.path.isfile(lib.flat_sf2):
            pool   = _GM_POOLS.get(role, _GM_POOLS["melody"])
            used   = self._used_presets[role]
            avail  = [e for e in pool if e[0] not in used]
            if not avail:          # pool exhausted → allow repeats
                avail = pool
            entry  = random.choice(avail)
            preset, bank, name = entry
            used.add(preset)
            return lib.flat_sf2, bank, preset, name

        logger.warning(
            "InstrumentRandomizer: no soundfont available for role '%s' (track '%s').",
            role, track_name,
        )
        return None


# ── Convenience factory ───────────────────────────────────────────────────────

def build_library_from_engine() -> SoundfontLibrary:
    """
    Build a SoundfontLibrary using the same search paths as
    AudioEngine.get_available_sf2_files(), which includes the project-local
    soundfonts/ directory as the highest-priority location.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = os.path.expanduser("~")
    paths = [
        os.path.join(project_root, "soundfonts"),
        os.path.join(project_root, "assets", "soundfonts"),
        "/usr/share/sounds/sf2",
        os.path.join(home, "Library", "Audio", "Sounds", "Banks"),
        os.path.join(home, "Music", "soundfonts"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "soundfonts"),
        os.path.join(os.environ.get("APPDATA", ""), "soundfonts"),
        r"C:\soundfonts",
        os.path.join(home, "Documents", "soundfonts"),
        os.path.join(home, "Documents", "SoundFonts"),
    ]
    return SoundfontLibrary(paths)
