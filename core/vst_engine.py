"""
vst_engine.py — VST3 / Audio Unit Plugin Host
==============================================
Hosts third-party instrument and effect plugins (VST3 on all platforms,
Audio Units on macOS) through Spotify's *pedalboard* library.

Each VstTrack stores one loaded plugin instance together with the MIDI
notes recorded in the piano roll.  VstManager owns all tracks and exposes
methods for:
    • scanning the system for installed plugins
    • loading / unloading plugin instances
    • reading and writing plugin parameters
    • opening the plugin's native GUI editor
    • offline-rendering MIDI notes to a numpy audio array

Graceful degradation:
    If pedalboard is not installed, VstManager.is_available() returns False
    and every mutating method silently does nothing.  The rest of the
    application continues to work without VST support.

    Install the dependency with:
        pip install pedalboard sounddevice
"""

from __future__ import annotations

import logging
import os
import platform
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import numpy as _np
    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False

try:
    import sounddevice as _sd
    _SD_OK = True
except ImportError:
    _SD_OK = False

logger = logging.getLogger(__name__)

# ─── Optional pedalboard import ────────────────────────────────────────────

try:
    import pedalboard as _pb
    from pedalboard import load_plugin as _load_plugin
    _PEDALBOARD_OK = True
except ImportError:
    _PEDALBOARD_OK = False
    logger.warning(
        "pedalboard is not installed — VST/AU support disabled. "
        "Run:  pip install pedalboard sounddevice"
    )


# ─── System VST path tables (per operating system) ─────────────────────────

_VST3_PATHS: Dict[str, List[str]] = {
    "Darwin": [
        "/Library/Audio/Plug-Ins/VST3",
        os.path.expanduser("~/Library/Audio/Plug-Ins/VST3"),
    ],
    "Windows": [
        r"C:\Program Files\Common Files\VST3",
        r"C:\Program Files (x86)\Common Files\VST3",
    ],
    "Linux": [
        "/usr/lib/vst3",
        "/usr/local/lib/vst3",
        os.path.expanduser("~/.vst3"),
    ],
}

_AU_PATHS: List[str] = [
    "/Library/Audio/Plug-Ins/Components",
    os.path.expanduser("~/Library/Audio/Plug-Ins/Components"),
]


def scan_vst_paths() -> List[str]:
    """
    Scan the standard OS plugin directories and return all found plugin paths.

    Includes VST3 bundles on every platform and Audio Unit (.component)
    bundles on macOS.  Paths are returned in alphabetical order.
    """
    found: List[str] = []
    system = platform.system()

    for base in _VST3_PATHS.get(system, []):
        if not os.path.isdir(base):
            continue
        try:
            for entry in os.scandir(base):
                if entry.name.endswith(".vst3"):
                    found.append(entry.path)
        except PermissionError:
            pass

    if system == "Darwin":
        for base in _AU_PATHS:
            if not os.path.isdir(base):
                continue
            try:
                for entry in os.scandir(base):
                    if entry.name.endswith(".component"):
                        found.append(entry.path)
            except PermissionError:
                pass

    return sorted(found)


# ─── Real-time VST audio player ────────────────────────────────────────────

class VstRealTimePlayer:
    """
    Streams real-time audio from a VST instrument plugin via sounddevice.

    The audio callback runs on a dedicated thread owned by sounddevice.
    MIDI events are placed in a thread-safe list by note_on/note_off and
    consumed per block in the callback.  Each event is timestamped at 0.0
    within the current block (i.e. played at the start of the next block),
    giving at most one block of latency (~11 ms at 44100 Hz / 512 samples).

    Requires:  pip install sounddevice numpy pedalboard
    """

    SAMPLE_RATE: int = 44100
    BLOCK_SIZE:  int = 512

    def __init__(self, plugin: object) -> None:
        self._plugin = plugin
        self._pending: List[Tuple[bytes, float]] = []
        self._lock = threading.Lock()
        # Held by the main thread while show_editor() is running.
        # The audio callback tries a non-blocking acquire; if it fails it outputs
        # silence instead of calling plugin.process() concurrently — that concurrent
        # access is what causes the segfault.
        self._editor_lock = threading.Lock()
        self._stream: Optional[object] = None

    def note_on(self, pitch: int, velocity: int = 100) -> None:
        # Pedalboard expects (midi_bytes: bytes, timestamp_seconds: float) tuples.
        with self._lock:
            self._pending.append((bytes([0x90, max(0, min(127, pitch)),
                                         max(0, min(127, velocity))]), 0.0))

    def note_off(self, pitch: int) -> None:
        with self._lock:
            self._pending.append((bytes([0x80, max(0, min(127, pitch)), 0]), 0.0))

    def _callback(self, outdata, frames, time_info, status) -> None:
        with self._lock:
            midi = list(self._pending)
            self._pending.clear()
        # Non-blocking acquire: if the editor lock is held by the main thread
        # (show_editor is open) we must NOT call plugin.process() — that's the
        # concurrent access that segfaults.  Output silence for this block instead.
        if not self._editor_lock.acquire(blocking=False):
            outdata.fill(0)
            return
        try:
            # Use pedalboard's MIDI→audio overload: process(midi_messages, duration, sample_rate).
            # reset=False is essential for streaming — True would wipe voice state every block.
            block_duration = frames / self.SAMPLE_RATE
            audio = self._plugin.process(
                midi,
                duration=block_duration,
                sample_rate=self.SAMPLE_RATE,
                num_channels=2,
                reset=False,
            )
            # pedalboard returns (channels, samples); sounddevice wants (samples, channels).
            out = _np.asarray(audio, dtype=_np.float32)
            if out.ndim == 1:
                out = _np.stack([out, out])          # 1-D mono → (2, N)
            elif out.ndim == 2 and out.shape[0] == 1:
                out = _np.concatenate([out, out], axis=0)   # (1, N) mono → (2, N)
            # Pad if the plugin returned fewer samples than requested.
            n = out.shape[1]
            if n < frames:
                out = _np.concatenate(
                    [out, _np.zeros((2, frames - n), dtype=_np.float32)], axis=1)
            outdata[:] = out.T[:frames]
        except Exception as exc:
            logger.warning("VstRealTimePlayer callback error: %s", exc)
            outdata.fill(0)
        finally:
            self._editor_lock.release()

    def pause_for_editor(self) -> None:
        """Block until the running audio block finishes, then hold the editor lock.

        Call this before plugin.show_editor().  The audio callback will output
        silence until resume_after_editor() is called.
        """
        self._editor_lock.acquire()

    def resume_after_editor(self) -> None:
        """Discard buffered MIDI events and resume normal audio processing.

        Call this after plugin.show_editor() returns.
        """
        with self._lock:
            self._pending.clear()
        self._editor_lock.release()

    def start(self) -> bool:
        """Open the sounddevice output stream.  Returns True on success."""
        if not (_PEDALBOARD_OK and _SD_OK and _NUMPY_OK):
            logger.warning(
                "VST real-time player unavailable — "
                "install: pip install sounddevice numpy pedalboard"
            )
            return False
        # Discard any MIDI events that accumulated while the stream was stopped
        # (e.g. during show_editor()) so they don't replay on the first block.
        with self._lock:
            self._pending.clear()
        try:
            self._stream = _sd.OutputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=self.BLOCK_SIZE,
                channels=2,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            logger.info("VstRealTimePlayer started (%.0f Hz, block=%d)",
                        self.SAMPLE_RATE, self.BLOCK_SIZE)
            return True
        except Exception as exc:
            logger.error("VstRealTimePlayer start failed: %s", exc)
            return False

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


# ─── Data classes ──────────────────────────────────────────────────────────

@dataclass
class VstTrack:
    """
    One VST instrument track.

    Attributes:
        name         — display name shown in the arrangement and piano roll.
        plugin_path  — absolute path to the .vst3 or .component bundle.
        channel      — MIDI channel index (0–15) used as a unique key.
        color        — hex colour string for lane / note colouring.
        notes        — MIDI notes shared with the piano roll.  Same objects
                       as MidiNote but stored here for offline rendering.
        _plugin      — the loaded pedalboard plugin instance (internal).
    """
    name:        str
    plugin_path: str
    channel:     int
    color:       str  = "#9945FF"            # crystal purple — VST default
    notes:       list = field(default_factory=list, repr=False)
    _plugin:     object = field(default=None, repr=False, compare=False)


# ─── VstManager ────────────────────────────────────────────────────────────

class VstManager:
    """
    Owns and orchestrates all loaded VST plugin instances.

    Usage pattern:
        manager = VstManager()
        if manager.is_available():
            manager.add_track(track)
            params = manager.get_parameters(channel)
            manager.open_editor(channel)   # blocks — run in a thread
    """

    def __init__(self) -> None:
        self._tracks: Dict[int, VstTrack] = {}
        self._rt_players: Dict[int, VstRealTimePlayer] = {}
        # Prevents simultaneous renders from corrupting plugin state.
        self._render_lock = threading.Lock()

    # ── Availability ───────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """Return True when pedalboard is installed and ready."""
        return _PEDALBOARD_OK

    # ── Track lifecycle ────────────────────────────────────────────────────

    def add_track(self, track: VstTrack) -> bool:
        """
        Load the plugin bundle for *track* and register it.

        Returns True on success; False if pedalboard is missing or the
        plugin file cannot be parsed.
        """
        if not _PEDALBOARD_OK:
            logger.warning("pedalboard not installed — cannot load VST track.")
            return False
        try:
            plugin = _load_plugin(track.plugin_path)
            track._plugin = plugin
            self._tracks[track.channel] = track
            logger.info("VST loaded: '%s'  channel=%d", track.name, track.channel)
            return True
        except Exception as exc:
            logger.error("VST load failed (%s): %s", track.plugin_path, exc)
            return False

    def remove_track(self, channel: int) -> None:
        """Unregister and attempt to cleanly shut down the plugin."""
        self.stop_realtime(channel)
        track = self._tracks.pop(channel, None)
        if track and track._plugin:
            try:
                track._plugin.reset()
            except Exception:
                pass

    # ── Real-time audio playback ───────────────────────────────────────────

    def start_realtime(self, channel: int) -> bool:
        """
        Start streaming real-time audio from the plugin on *channel*.

        Creates a VstRealTimePlayer that opens a sounddevice OutputStream.
        Returns True on success.
        """
        track = self._tracks.get(channel)
        if not track or not track._plugin:
            return False
        self.stop_realtime(channel)   # clean up any previous player
        player = VstRealTimePlayer(track._plugin)
        if player.start():
            self._rt_players[channel] = player
            return True
        return False

    def stop_realtime(self, channel: int) -> None:
        """Stop and release the real-time player for *channel*."""
        player = self._rt_players.pop(channel, None)
        if player:
            player.stop()

    def vst_note_on(self, channel: int, pitch: int, velocity: int = 100) -> None:
        """Send a note-on to the real-time VST player for *channel*."""
        player = self._rt_players.get(channel)
        if player:
            player.note_on(pitch, velocity)

    def vst_note_off(self, channel: int, pitch: int) -> None:
        """Send a note-off to the real-time VST player for *channel*."""
        player = self._rt_players.get(channel)
        if player:
            player.note_off(pitch)

    def get_track(self, channel: int) -> Optional[VstTrack]:
        """Return the VstTrack for *channel*, or None if it is not a VST track."""
        return self._tracks.get(channel)

    def get_all_tracks(self) -> List[VstTrack]:
        return list(self._tracks.values())

    # ── Parameter access ───────────────────────────────────────────────────

    def get_parameters(self, channel: int) -> Dict[str, float]:
        """
        Return {parameter_name: current_value} for all plugin parameters.

        Values are normalised to [0.0, 1.0] where applicable.  Returns an
        empty dict when no plugin is loaded on *channel*.
        """
        track = self._tracks.get(channel)
        if not track or not track._plugin:
            return {}
        try:
            return {k: float(v) for k, v in track._plugin.parameters.items()}
        except Exception:
            return {}

    def set_parameter(self, channel: int, name: str, value: float) -> None:
        """Write *value* (normalised float) to a named plugin parameter."""
        track = self._tracks.get(channel)
        if not track or not track._plugin:
            return
        try:
            track._plugin.parameters[name] = value
        except Exception as exc:
            logger.warning("VST set_parameter '%s': %s", name, exc)

    # ── Native editor ──────────────────────────────────────────────────────

    def open_editor(self, channel: int) -> None:
        """
        Open the plugin's native GUI editor in a platform-native window.

        This call blocks until the editor is closed, so it must be invoked
        from a background thread to keep the Qt event loop responsive.
        """
        track = self._tracks.get(channel)
        if not track or not track._plugin:
            logger.warning("open_editor: no VST plugin on channel %d", channel)
            return
        try:
            track._plugin.show_editor()
        except AttributeError:
            logger.warning(
                "Plugin '%s' does not expose a native editor.", track.name)
        except Exception as exc:
            logger.error("VST editor error: %s", exc)

    # ── Offline audio rendering ────────────────────────────────────────────

    def render_notes(
        self,
        channel:     int,
        notes:       list,          # List[MidiNote]
        bpm:         float,
        sample_rate: int = 44100,
    ) -> Optional[object]:          # numpy ndarray (2, N) float32 or None
        """
        Offline-render *notes* through the VST plugin.

        Returns a stereo float32 numpy array, or None when pedalboard is
        unavailable, the channel has no plugin, or rendering fails.

        The caller is responsible for saving or playing the returned audio.
        """
        if not _PEDALBOARD_OK:
            return None
        track = self._tracks.get(channel)
        if not track or not track._plugin:
            return None

        secs_per_beat = 60.0 / max(1.0, bpm)
        duration = 4.0
        if notes:
            last = max(n.start_beat + n.duration for n in notes)
            duration = last * secs_per_beat + 1.5   # 1.5-second release tail

        # Build timed MIDI messages: pedalboard wants (bytes, timestamp_seconds) tuples.
        midi_messages: List[Tuple[bytes, float]] = []
        for note in sorted(notes, key=lambda n: n.start_beat):
            t_on  = note.start_beat * secs_per_beat
            t_off = (note.start_beat + note.duration) * secs_per_beat
            midi_messages.append((
                bytes([0x90, max(0, min(127, note.pitch)),
                       max(0, min(127, note.velocity))]),
                t_on,
            ))
            midi_messages.append((
                bytes([0x80, max(0, min(127, note.pitch)), 0]),
                t_off,
            ))

        try:
            with self._render_lock:
                audio = track._plugin.process(
                    midi_messages,
                    duration=duration,
                    sample_rate=sample_rate,
                    reset=True,
                )
            return audio
        except Exception as exc:
            logger.error("VST render error (channel %d): %s", channel, exc)
            return None