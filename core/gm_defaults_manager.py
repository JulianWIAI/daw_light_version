"""
gm_defaults_manager.py — User-configurable GM instrument defaults
=================================================================

Stores the user's preferred SFZ / VST3 file for each General MIDI
instrument group.  Settings are persisted in a tiny JSON file inside the
user's home directory so they survive project reloads and app restarts.

Settings file location
----------------------
    Windows : C:\\Users\\<user>\\.sbs_synth_master\\gm_defaults.json
    macOS   : /Users/<user>/.sbs_synth_master/gm_defaults.json
    Linux   : /home/<user>/.sbs_synth_master/gm_defaults.json

JSON format
-----------
Only keys with *user overrides* are stored.  Missing keys fall back to the
hardcoded relative default path silently.

    {
        "piano":   "C:/MySamples/grand_piano.sfz",
        "drums":   "C:/VST3/BFD4.vst3"
    }

Usage
-----
    from core.gm_defaults_manager import GmDefaultsManager, GM_CATEGORIES

    mgr = GmDefaultsManager()
    overrides = mgr.load()                       # dict of user paths
    path = mgr.get_sfz_path(0, overrides)        # Acoustic Grand Piano → resolved path
    path = mgr.get_sfz_path(128, overrides)      # Drums              → resolved path
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Absolute path to the app root (folder that contains main.py).
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─────────────────────────────────────────────────────────────────────────────
# GM category registry
# ─────────────────────────────────────────────────────────────────────────────

# Structure per entry:
#   key: str  →  (display_name, gm_ids, default_relative_sfz_path)
#
# The *key* is what gets stored in gm_defaults.json.
# *gm_ids* lists every GM program number (0-127) that belongs to this group,
#   plus 128 for the drums sentinel.
# *default_relative_sfz_path* is resolved relative to _APP_ROOT at runtime.

GM_CATEGORIES: OrderedDict[str, Tuple[str, List[int], str]] = OrderedDict([
    ("piano",   (
        "Piano  (+ catch-all)",
        list(range(0, 8))
        + list(range(8, 24))    # chromatic perc, organs
        + list(range(48, 56))   # ensemble
        + list(range(64, 80))   # reed, pipe
        + list(range(96, 128)), # ethnic, percussive, SFX
        "system/defaults/default_piano.sfz",
    )),
    ("guitar",  (
        "Guitar",
        list(range(24, 32)),
        "system/defaults/default_guitar.sfz",
    )),
    ("bass",    (
        "Bass",
        list(range(32, 40)),
        "system/defaults/default_bass.sfz",
    )),
    ("strings", (
        "Strings",
        list(range(40, 48)),
        "system/defaults/default_strings.sfz",
    )),
    ("brass",   (
        "Brass",
        list(range(56, 64)),
        "system/defaults/default_brass.sfz",
    )),
    ("synth",   (
        "Synth Lead & Pad",
        list(range(80, 96)),
        "system/defaults/default_synth.sfz",
    )),
    ("drums",   (
        "Drums  (MIDI Channel 10)",
        [128],
        "system/defaults/default_drums.sfz",
    )),
])

# Reverse lookup: gm_id → category key (built once at import time).
_GM_ID_TO_KEY: Dict[int, str] = {
    gm_id: key
    for key, (_name, ids, _path) in GM_CATEGORIES.items()
    for gm_id in ids
}


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve(path: str) -> str:
    """
    Return an absolute path.

    Absolute paths (user overrides) are returned unchanged.
    Relative paths (hardcoded defaults) are anchored at the app root.
    """
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(_APP_ROOT, path))


# ─────────────────────────────────────────────────────────────────────────────
# GmDefaultsManager
# ─────────────────────────────────────────────────────────────────────────────

class GmDefaultsManager:
    """
    Manages user-configurable GM instrument defaults with JSON persistence.

    Typical usage
    -------------
    Reading (once per import, on the worker thread)::

        mgr       = GmDefaultsManager()
        overrides = mgr.load()
        for payload in payloads:
            payload.sfz_path = mgr.get_sfz_path(payload.gm_program_id, overrides)

    Writing (from the settings dialog)::

        mgr = GmDefaultsManager()
        overrides = mgr.load()
        overrides["piano"] = "/path/to/my_piano.sfz"
        mgr.save(overrides)
    """

    SETTINGS_DIR:  str = os.path.join(os.path.expanduser("~"), ".sbs_synth_master")
    SETTINGS_FILE: str = os.path.join(SETTINGS_DIR, "gm_defaults.json")

    # ── IO ────────────────────────────────────────────────────────────────────

    def load(self) -> Dict[str, Any]:
        """
        Read user overrides from disk.

        Returns a ``{key: absolute_path}`` dict containing ONLY the categories
        the user has customised.  Missing keys fall back to the hardcoded
        defaults inside ``get_sfz_path()``.

        Returns an empty dict if the settings file does not exist yet.
        """
        if not os.path.isfile(self.SETTINGS_FILE):
            return {}
        try:
            with open(self.SETTINGS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            result = {}
            for k, v in data.items():
                if k not in GM_CATEGORIES:
                    continue
                if isinstance(v, str) and v.strip():
                    result[k] = v  # backward-compat plain SFZ path
                elif isinstance(v, dict) and v.get("type") and v.get("path"):
                    result[k] = v  # new-style override dict
            return result
        except Exception as exc:
            logger.warning("gm_defaults_manager: could not load '%s': %s",
                           self.SETTINGS_FILE, exc)
            return {}

    def save(self, overrides: Dict[str, Any]) -> None:
        """
        Persist user overrides to disk.

        Values may be a plain SFZ path string (backward compat) or a dict of
        the form ``{"type": "sfz"|"sf2"|"vst3", "path": ..., ...}``.  Only
        entries with a non-empty path are written.

        Args:
            overrides: ``{key: entry}`` dict.  Pass ``{}`` to clear all overrides.
        """
        os.makedirs(self.SETTINGS_DIR, exist_ok=True)

        def _is_valid(v: Any) -> bool:
            if isinstance(v, str):
                return bool(v.strip())
            if isinstance(v, dict):
                return bool(v.get("path", ""))
            return False

        clean = {k: v for k, v in overrides.items() if _is_valid(v)}
        try:
            with open(self.SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(clean, fh, indent=2, ensure_ascii=False)
            logger.info("gm_defaults_manager: saved %d override(s) → '%s'",
                        len(clean), self.SETTINGS_FILE)
        except Exception as exc:
            logger.error("gm_defaults_manager: could not save '%s': %s",
                         self.SETTINGS_FILE, exc)

    def reset_to_defaults(self) -> None:
        """Erase all user overrides (restores hardcoded defaults)."""
        self.save({})

    # ── Lookups ───────────────────────────────────────────────────────────────

    @staticmethod
    def get_category_key(gm_id: int) -> str:
        """
        Map a GM program ID (0-127) or the drum sentinel (128) to its
        category key (e.g. ``"bass"``, ``"drums"``).

        Unknown IDs fall back to ``"piano"`` (the catch-all).
        """
        return _GM_ID_TO_KEY.get(gm_id, "piano")

    @staticmethod
    def get_default_path(key: str) -> str:
        """
        Return the hardcoded default path for *key*, resolved to absolute.

        Args:
            key: One of the keys in ``GM_CATEGORIES``.
        """
        _name, _ids, rel = GM_CATEGORIES.get(key, GM_CATEGORIES["piano"])
        return _resolve(rel)

    def get_sfz_path(self, gm_id: int,
                     overrides: Optional[Dict[str, str]] = None) -> str:
        """
        Return the instrument path to use for *gm_id*.

        Priority:
            1. User override for this category (from *overrides* dict).
            2. Hardcoded default path, resolved to absolute.

        Args:
            gm_id:     GM program number 0-127, or 128 for drums.
            overrides: Pre-loaded override dict from ``load()``.  Pass ``None``
                       to load from disk on every call (convenient for one-offs,
                       but use ``load()`` + pass the dict when calling in a loop).

        Returns:
            Absolute path string.  The file may or may not exist yet.
        """
        if overrides is None:
            overrides = self.load()
        key = self.get_category_key(gm_id)
        raw = overrides.get(key, "")
        if isinstance(raw, str) and raw.strip():
            return raw
        if isinstance(raw, dict) and raw.get("type") == "sfz":
            return raw.get("path", "")
        return self.get_default_path(key)

    def get_override_entry(self, gm_id: int,
                           overrides: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        Return the full override dict for *gm_id*, or ``None`` if no override.

        The returned dict always has the shape::

            {"type": "sfz"|"sf2"|"vst3", "path": str, ...}

        For SF2 entries it additionally contains ``"bank"`` and ``"preset"`` keys.
        Old-style plain-string values (written by earlier versions) are treated
        as SFZ entries for backward compatibility.
        """
        if overrides is None:
            overrides = self.load()
        key = self.get_category_key(gm_id)
        raw = overrides.get(key)
        if not raw:
            return None
        if isinstance(raw, str) and raw.strip():
            return {"type": "sfz", "path": raw}
        if isinstance(raw, dict) and raw.get("type") and raw.get("path"):
            return raw
        return None
