"""
vst3_bus_manager.py -- Python bridge for VST3 multi-bus routing.
=================================================================
Wraps the C++ Vst3BusManager class exposed via daw_processors (when the
extension is built with HAVE_VST3_SDK).  Provides query helpers so the
GUI and project manager can inspect a plugin's bus topology without
touching audio routing logic.

All audio work (buffer allocation, per-bus mixing) lives in the C++ layer.
Python only reads topology and forwards configuration changes.

When daw_processors is unavailable or was built without VST3 SDK support,
every class degrades to a no-op stub that returns safe empty defaults.

Public API
----------
  BusInfo           -- data class describing one VST3 bus
  Vst3BusManager    -- query/configure bus topology for one plugin instance
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── Try to import the C++ extension ──────────────────────────────────────────

try:
    import daw_processors as _dp                        # type: ignore[import]
    _HAS_CPP = hasattr(_dp, "Vst3BusManager")
except (ImportError, OSError):
    _dp     = None                                      # type: ignore[assignment]
    _HAS_CPP = False

if not _HAS_CPP:
    logger.debug(
        "vst3_bus_manager: C++ Vst3BusManager not available — "
        "using Python stubs."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Data class mirroring the C++ BusInfo struct
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BusInfo:
    """
    Describes one audio bus of a VST3 plugin.

    Fields mirror the VST3 BusInfo / AudioBusBuffers structures:
      name         -- human-readable bus label (from the plugin)
      channel_count-- number of audio channels (1 = mono, 2 = stereo, 6 = 5.1…)
      bus_type     -- "main" or "aux" (kMain / kAux in VST3 terms)
      direction    -- "input" or "output"
      default_active -- whether the bus is active by default
    """
    name:           str   = ""
    channel_count:  int   = 2
    bus_type:       str   = "main"    # "main" | "aux"
    direction:      str   = "output"  # "input" | "output"
    default_active: bool  = True


# ═══════════════════════════════════════════════════════════════════════════════
# Main class
# ═══════════════════════════════════════════════════════════════════════════════

class Vst3BusManager:
    """
    Query and configure the bus topology of one VST3 plugin instance.

    Lifecycle
    ---------
    1. Create: Vst3BusManager(plugin_path_or_id)
    2. After the C++ host loads the plugin: call attach(cpp_manager)
       where cpp_manager is the dp.Vst3BusManager returned by your C++ host
       bridge.  Before attach(), all methods return safe empty defaults.
    3. Query: input_buses(), output_buses(), total_output_channels()
    4. Configure routing: set_bus_active(direction, index, active)
    5. Before render: activate_default_buses() to restore default state.

    If the C++ extension is unavailable the entire class operates as a stub
    that remembers the last activation request but performs no real action.
    """

    def __init__(self, plugin_id: str = "") -> None:
        # Human-readable identifier for logging.
        self._plugin_id = plugin_id
        # The underlying C++ Vst3BusManager, set via attach().
        self._cpp: Optional[object] = None
        # Cached Python-side bus lists (populated after attach).
        self._input_buses:  List[BusInfo] = []
        self._output_buses: List[BusInfo] = []
        # Track activation state for each (direction, index).
        self._active: dict[tuple, bool] = {}

    # ── Attachment ─────────────────────────────────────────────────────────────

    def attach(self, cpp_manager: object) -> None:
        """
        Connect this Python object to a C++ dp.Vst3BusManager.

        Call this after the C++ host has successfully loaded the plugin so
        the bus topology can be queried from the C++ side.
        """
        self._cpp = cpp_manager
        self._refresh_bus_cache()

    def detach(self) -> None:
        """Disconnect from the C++ manager (e.g. when a plugin is unloaded)."""
        self._cpp = None
        self._input_buses.clear()
        self._output_buses.clear()
        self._active.clear()

    # ── Bus queries ────────────────────────────────────────────────────────────

    def input_buses(self) -> List[BusInfo]:
        """Return a list of BusInfo for every input bus."""
        return list(self._input_buses)

    def output_buses(self) -> List[BusInfo]:
        """Return a list of BusInfo for every output bus."""
        return list(self._output_buses)

    def total_output_channels(self) -> int:
        """Sum of channel counts across all active output buses."""
        return sum(
            b.channel_count for b in self._output_buses
            if self._active.get(("output", i), b.default_active)
            for i, b in enumerate(self._output_buses)
            if self._active.get(("output", i), b.default_active)
        )

    def total_input_channels(self) -> int:
        """Sum of channel counts across all active input buses."""
        return sum(
            b.channel_count for i, b in enumerate(self._input_buses)
            if self._active.get(("input", i), b.default_active)
        )

    # ── Activation control ─────────────────────────────────────────────────────

    def set_bus_active(self, direction: str, index: int, active: bool) -> None:
        """
        Activate or deactivate one bus.

        direction -- "input" or "output"
        index     -- 0-based bus index
        active    -- True to activate, False to deactivate

        Forwards to the C++ manager when attached; otherwise records the
        change in the Python-side activation table.
        """
        key = (direction, index)
        self._active[key] = active

        if not _HAS_CPP or self._cpp is None:
            return
        try:
            self._cpp.set_bus_active(direction == "input", index, active)
        except Exception as exc:
            logger.warning(
                "Vst3BusManager[%s]: set_bus_active(%s, %d, %s) failed: %s",
                self._plugin_id, direction, index, active, exc)

    def activate_default_buses(self) -> None:
        """Re-apply each bus's default_active flag (resets any manual changes)."""
        for i, b in enumerate(self._input_buses):
            self.set_bus_active("input", i, b.default_active)
        for i, b in enumerate(self._output_buses):
            self.set_bus_active("output", i, b.default_active)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _refresh_bus_cache(self) -> None:
        """Pull bus topology from the C++ manager and populate the Python cache."""
        self._input_buses.clear()
        self._output_buses.clear()

        if not _HAS_CPP or self._cpp is None:
            return

        try:
            for raw in self._cpp.get_input_buses():
                self._input_buses.append(self._from_cpp_bus(raw, "input"))
            for raw in self._cpp.get_output_buses():
                self._output_buses.append(self._from_cpp_bus(raw, "output"))
            logger.debug(
                "Vst3BusManager[%s]: %d input bus(es), %d output bus(es)",
                self._plugin_id,
                len(self._input_buses),
                len(self._output_buses),
            )
        except Exception as exc:
            logger.warning(
                "Vst3BusManager[%s]: bus query failed: %s",
                self._plugin_id, exc)

    @staticmethod
    def _from_cpp_bus(raw: object, direction: str) -> BusInfo:
        """Convert a C++ BusInfo struct to a Python BusInfo dataclass."""
        return BusInfo(
            name          = getattr(raw, "name",          ""),
            channel_count = getattr(raw, "channel_count", 2),
            bus_type      = getattr(raw, "bus_type",      "main"),
            direction     = direction,
            default_active= getattr(raw, "default_active", True),
        )

    # ── Fallback bus creation (no C++ engine) ──────────────────────────────────

    @classmethod
    def stereo_stub(cls, plugin_id: str = "") -> "Vst3BusManager":
        """
        Return a manager pre-configured with one stereo output bus.
        Useful as a safe default when no plugin is loaded.
        """
        mgr = cls(plugin_id)
        mgr._output_buses = [BusInfo(
            name="Stereo Out", channel_count=2,
            bus_type="main", direction="output", default_active=True,
        )]
        return mgr
