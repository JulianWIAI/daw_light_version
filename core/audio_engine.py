"""
audio_engine.py — SBS-Synth Master Audio Engine
=================================================
Central audio subsystem.  All sound production flows through this class.

Architecture decision:
    We wrap FluidSynth (a real-time SF2 synthesiser) through the
    `pyfluidsynth` Python bindings.  FluidSynth runs its own low-latency
    audio thread internally, so PySide6's GUI thread never blocks on audio
    rendering — a critical requirement for a responsive DAW.

Plugin pattern:
    Every instrument is registered as an `InstrumentPlugin` data-class.
    Adding a new instrument = creating a new InstrumentPlugin instance and
    calling `register_instrument()`.  The engine does NOT need to change.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Apple Silicon / Homebrew library path fix
# ---------------------------------------------------------------------------

def _patch_ctypes_for_homebrew() -> None:
    """
    Monkey-patch ctypes.util.find_library so it finds libfluidsynth installed
    by Homebrew on Apple Silicon Macs (/opt/homebrew/lib/).

    Why is this needed?
        On macOS arm64, Homebrew installs libraries to /opt/homebrew/lib,
        which is NOT in the default dyld search path that ctypes uses.
        pyfluidsynth calls find_library('fluidsynth') at import time; if
        it returns None the whole module raises ImportError.  Patching here
        lets us add our known paths without modifying system config.
    """
    _original = ctypes.util.find_library

    def _patched(name: str) -> Optional[str]:
        result = _original(name)
        if result is None and name in ("fluidsynth", "fluidsynth-3"):
            candidates = [
                "/opt/homebrew/lib/libfluidsynth.dylib",   # Apple Silicon
                "/usr/local/lib/libfluidsynth.dylib",       # Intel Homebrew
                "/usr/lib/libfluidsynth.so.3",              # Linux
                "/usr/lib/x86_64-linux-gnu/libfluidsynth.so.3",
            ]
            for path in candidates:
                if os.path.isfile(path):
                    return path
        return result

    ctypes.util.find_library = _patched


# ---------------------------------------------------------------------------
# General MIDI instrument catalogue (128 presets + drums)
# ---------------------------------------------------------------------------

# Structure: category → list of (preset, bank, display_name)
# bank=0 for all melodic instruments, bank=128 for GM drums.
GM_INSTRUMENTS: Dict[str, List[Tuple[int, int, str]]] = {
    "Piano": [
        (0,  0, "Acoustic Grand Piano"),
        (1,  0, "Bright Acoustic Piano"),
        (2,  0, "Electric Grand Piano"),
        (3,  0, "Honky-tonk Piano"),
        (4,  0, "Electric Piano 1"),
        (5,  0, "Electric Piano 2"),
        (6,  0, "Harpsichord"),
        (7,  0, "Clavinet"),
    ],
    "Chromatic Perc": [
        (8,  0, "Celesta"),
        (9,  0, "Glockenspiel"),
        (10, 0, "Music Box"),
        (11, 0, "Vibraphone"),
        (12, 0, "Marimba"),
        (13, 0, "Xylophone"),
        (14, 0, "Tubular Bells"),
        (15, 0, "Dulcimer"),
    ],
    "Organ": [
        (16, 0, "Drawbar Organ"),
        (17, 0, "Percussive Organ"),
        (18, 0, "Rock Organ"),
        (19, 0, "Church Organ"),
        (20, 0, "Reed Organ"),
        (21, 0, "Accordion"),
        (22, 0, "Harmonica"),
        (23, 0, "Tango Accordion"),
    ],
    "Guitar": [
        (24, 0, "Nylon Guitar"),
        (25, 0, "Steel Guitar"),
        (26, 0, "Jazz Guitar"),
        (27, 0, "Clean Guitar"),
        (28, 0, "Muted Guitar"),
        (29, 0, "Overdriven Guitar"),
        (30, 0, "Distortion Guitar"),
        (31, 0, "Guitar Harmonics"),
    ],
    "Bass": [
        (32, 0, "Acoustic Bass"),
        (33, 0, "Electric Bass (finger)"),
        (34, 0, "Electric Bass (pick)"),
        (35, 0, "Fretless Bass"),
        (36, 0, "Slap Bass 1"),
        (37, 0, "Slap Bass 2"),
        (38, 0, "Synth Bass 1"),
        (39, 0, "Synth Bass 2"),
    ],
    "Strings": [
        (40, 0, "Violin"),
        (41, 0, "Viola"),
        (42, 0, "Cello"),
        (43, 0, "Contrabass"),
        (44, 0, "Tremolo Strings"),
        (45, 0, "Pizzicato Strings"),
        (46, 0, "Orchestral Harp"),
        (47, 0, "Timpani"),
    ],
    "Ensemble": [
        (48, 0, "String Ensemble 1"),
        (49, 0, "String Ensemble 2"),
        (50, 0, "Synth Strings 1"),
        (51, 0, "Synth Strings 2"),
        (52, 0, "Choir Aahs"),
        (53, 0, "Voice Oohs"),
        (54, 0, "Synth Choir"),
        (55, 0, "Orchestra Hit"),
    ],
    "Brass": [
        (56, 0, "Trumpet"),
        (57, 0, "Trombone"),
        (58, 0, "Tuba"),
        (59, 0, "Muted Trumpet"),
        (60, 0, "French Horn"),
        (61, 0, "Brass Section"),
        (62, 0, "Synth Brass 1"),
        (63, 0, "Synth Brass 2"),
    ],
    "Reed": [
        (64, 0, "Soprano Sax"),
        (65, 0, "Alto Sax"),
        (66, 0, "Tenor Sax"),
        (67, 0, "Baritone Sax"),
        (68, 0, "Oboe"),
        (69, 0, "English Horn"),
        (70, 0, "Bassoon"),
        (71, 0, "Clarinet"),
    ],
    "Pipe": [
        (72, 0, "Piccolo"),
        (73, 0, "Flute"),
        (74, 0, "Recorder"),
        (75, 0, "Pan Flute"),
        (76, 0, "Blown Bottle"),
        (77, 0, "Shakuhachi"),
        (78, 0, "Whistle"),
        (79, 0, "Ocarina"),
    ],
    "Synth Lead": [
        (80, 0, "Square Wave Lead"),
        (81, 0, "Sawtooth Lead"),
        (82, 0, "Calliope Lead"),
        (83, 0, "Chiff Lead"),
        (84, 0, "Charang Lead"),
        (85, 0, "Voice Lead"),
        (86, 0, "Fifths Lead"),
        (87, 0, "Bass + Lead"),
    ],
    "Synth Pad": [
        (88, 0, "New Age Pad"),
        (89, 0, "Warm Pad"),
        (90, 0, "Polysynth Pad"),
        (91, 0, "Choir Pad"),
        (92, 0, "Bowed Pad"),
        (93, 0, "Metallic Pad"),
        (94, 0, "Halo Pad"),
        (95, 0, "Sweep Pad"),
    ],
    "Synth Effects": [
        (96,  0, "Rain"),
        (97,  0, "Soundtrack"),
        (98,  0, "Crystal"),
        (99,  0, "Atmosphere"),
        (100, 0, "Brightness"),
        (101, 0, "Goblins"),
        (102, 0, "Echoes"),
        (103, 0, "Sci-fi"),
    ],
    "Ethnic": [
        (104, 0, "Sitar"),
        (105, 0, "Banjo"),
        (106, 0, "Shamisen"),
        (107, 0, "Koto"),
        (108, 0, "Kalimba"),
        (109, 0, "Bag Pipe"),
        (110, 0, "Fiddle"),
        (111, 0, "Shanai"),
    ],
    "Percussive": [
        (112, 0, "Tinkle Bell"),
        (113, 0, "Agogo"),
        (114, 0, "Steel Drums"),
        (115, 0, "Woodblock"),
        (116, 0, "Taiko Drum"),
        (117, 0, "Melodic Tom"),
        (118, 0, "Synth Drum"),
        (119, 0, "Reverse Cymbal"),
    ],
    "Sound Effects": [
        (120, 0, "Guitar Fret Noise"),
        (121, 0, "Breath Noise"),
        (122, 0, "Seashore"),
        (123, 0, "Bird Tweet"),
        (124, 0, "Telephone Ring"),
        (125, 0, "Helicopter"),
        (126, 0, "Applause"),
        (127, 0, "Gunshot"),
    ],
    "Drums": [
        (0, 128, "Standard Drums"),   # GM drum kit — always channel 9
    ],
}

# ---------------------------------------------------------------------------
# Plugin data model
# ---------------------------------------------------------------------------

@dataclass
class InstrumentPlugin:
    """
    Describes one instrument slot inside the engine.

    Why a dataclass?  It gives us free __repr__, __eq__, and type-checked
    construction without boilerplate.  Think of it as a typed dict.

    Attributes:
        name        : Human-readable label shown in the mixer strip.
        sf2_path    : Absolute path to the SoundFont (.sf2) file on disk.
        bank        : GM bank number (0 = General MIDI melodic bank).
        preset      : GM program/patch number inside the bank (0-127).
        channel     : FluidSynth MIDI channel assigned to this instrument
                      (0-15).  Each track gets its own channel so effects
                      and volume can be controlled independently.
        gain        : Output gain multiplier (1.0 = unity, 0.0 = silence).
        pan         : Stereo panning (-1.0 = full left, +1.0 = full right).
        reverb_send : 0.0–1.0 wet amount sent to the built-in reverb unit.
        muted       : When True the channel output is silenced.
        soloed      : When True only soloed tracks are audible.
    """
    name: str
    sf2_path: str
    bank: int = 0
    preset: int = 0
    channel: int = 0
    gain: float = 1.0
    pan: float = 0.0
    reverb_send: float = 0.2
    muted: bool = False
    soloed: bool = False
    # Internal FluidSynth soundfont id — set by the engine after loading.
    _sfid: int = field(default=-1, repr=False)


# ---------------------------------------------------------------------------
# AudioEngine
# ---------------------------------------------------------------------------

class AudioEngine:
    """
    Manages all real-time audio synthesis via FluidSynth.

    Responsibilities:
        1. Initialise and tear-down the FluidSynth synthesiser.
        2. Load SoundFont files and map them to MIDI channels.
        3. Provide a clean API for note-on / note-off / CC messages.
        4. Manage per-channel effects (gain, pan, reverb).
        5. Support a plugin-style instrument registry so new sounds can be
           added without touching engine internals.

    Thread safety:
        FluidSynth's C library is thread-safe for note events.  The Python
        GIL protects the `_instruments` dict.  A dedicated `_lock` guards
        any multi-step operations that must be atomic from the GUI's point
        of view (e.g. loading a new SF2 while playback is running).
    """

    # Middle C on a standard piano keyboard.
    MIDDLE_C: int = 60

    # Default velocity when none is specified (0–127 scale).
    DEFAULT_VELOCITY: int = 100

    def __init__(self) -> None:
        """
        Prepare internal state.  FluidSynth is NOT started here — call
        `start()` explicitly so the caller can handle startup errors
        gracefully without crashing the constructor.
        """
        self._fs: Optional[object] = None          # fluidsynth.Synth instance
        self._instruments: Dict[int, InstrumentPlugin] = {}  # channel → plugin
        self._lock = threading.Lock()
        self._running = False
        self._next_channel = 0                     # auto-assign channels (0-15)
        self._vst_players: Dict[int, object] = {}  # channel → VstRealTimePlayer

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, sample_rate: int = 44100, gain: float = 0.8) -> bool:
        """
        Initialise FluidSynth and open an audio output stream.

        Why 44 100 Hz?  It is the CD-quality standard; virtually every
        audio driver supports it and it gives us ~23 ms of time resolution
        per sample block — more than enough for real-time MIDI events.

        Driver fallback order:
            coreaudio → macOS native (lowest latency on Mac)
            pulseaudio → Linux desktop
            alsa       → Linux server / embedded
            dsound     → Windows
            portaudio  → cross-platform fallback

        Args:
            sample_rate: PCM sample rate passed to FluidSynth (Hz).
            gain:        Master output gain (0.0–1.0).

        Returns:
            True if the engine started successfully, False on failure.
        """
        # Patch ctypes so pyfluidsynth finds libfluidsynth on Homebrew paths.
        _patch_ctypes_for_homebrew()

        try:
            import fluidsynth  # imported here so the app can run without it
        except ImportError:
            logger.warning(
                "pyfluidsynth not installed — audio engine running in SILENT mode. "
                "Install it with: pip install pyfluidsynth"
            )
            self._running = False
            return False

        try:
            self._fs = fluidsynth.Synth(gain=gain, samplerate=float(sample_rate))
        except Exception as exc:
            logger.error("FluidSynth Synth() constructor failed: %s", exc)
            self._running = False
            return False

        # Try drivers in preference order; stop at the first that works.
        drivers = ["coreaudio", "pulseaudio", "alsa", "dsound", "portaudio"]
        for driver in drivers:
            try:
                self._fs.start(driver=driver)
                self._running = True
                logger.info(
                    "AudioEngine started via '%s' driver (%.0f Hz, gain=%.2f)",
                    driver, sample_rate, gain,
                )
                return True
            except Exception:
                continue

        logger.error("FluidSynth: no audio driver worked — tried %s", drivers)
        self._running = False
        return False

    def stop(self) -> None:
        """
        Shut down FluidSynth and release audio hardware resources.

        Why call delete() explicitly?  FluidSynth allocates C-level memory
        that the Python GC cannot see; we must free it ourselves.
        """
        if self._fs is not None:
            try:
                self._fs.delete()
            except Exception as exc:
                logger.warning("Error during FluidSynth teardown: %s", exc)
            self._fs = None
        self._running = False
        logger.info("AudioEngine stopped.")

    # ------------------------------------------------------------------
    # Instrument / Plugin management
    # ------------------------------------------------------------------

    def register_instrument(self, plugin: InstrumentPlugin) -> bool:
        """
        Load a SoundFont and bind it to the plugin's MIDI channel.

        Plugin pattern explanation:
            Callers create an `InstrumentPlugin` with their desired settings
            and hand it to the engine.  The engine assigns a FluidSynth
            soundfont id (`_sfid`) and program-changes the channel to the
            right bank/preset.  Future instruments follow the exact same
            path — zero engine code changes needed.

        Args:
            plugin: Fully configured InstrumentPlugin instance.

        Returns:
            True if the instrument loaded correctly.
        """
        if not self._running or self._fs is None:
            logger.warning("Cannot register instrument — engine not running.")
            return False

        if not os.path.isfile(plugin.sf2_path):
            logger.error("SF2 file not found: %s", plugin.sf2_path)
            return False

        with self._lock:
            try:
                sfid = self._fs.sfload(plugin.sf2_path)
                if sfid == -1:
                    logger.error("FluidSynth could not load: %s", plugin.sf2_path)
                    return False

                plugin._sfid = sfid
                # Tell FluidSynth which patch to use on this channel.
                self._fs.program_select(plugin.channel, sfid, plugin.bank, plugin.preset)
                self._apply_channel_effects(plugin)
                self._instruments[plugin.channel] = plugin
                logger.info("Loaded instrument '%s' on channel %d", plugin.name, plugin.channel)
                return True
            except Exception as exc:
                logger.error("Failed to register instrument '%s': %s", plugin.name, exc)
                return False

    def unregister_instrument(self, channel: int) -> None:
        """
        Remove an instrument from the engine and unload its SoundFont.

        Args:
            channel: MIDI channel number (0–15) to remove.
        """
        with self._lock:
            plugin = self._instruments.pop(channel, None)
            if plugin and self._fs and plugin._sfid != -1:
                self._fs.sfunload(plugin._sfid)
                logger.info("Unloaded instrument '%s'", plugin.name)

    # ── VST real-time player registration ─────────────────────────────────

    def register_vst_player(self, channel: int, player: object) -> None:
        """Register a VstRealTimePlayer for *channel*; note_on/off route there."""
        self._vst_players[channel] = player

    def unregister_vst_player(self, channel: int) -> None:
        """Remove VST routing for *channel* (FluidSynth takes over again)."""
        self._vst_players.pop(channel, None)

    def get_instruments(self) -> List[InstrumentPlugin]:
        """Return a snapshot of all registered instruments."""
        return list(self._instruments.values())

    def next_free_channel(self, is_drums: bool = False) -> int:
        """
        Return the next unused MIDI channel (0–15), or -1 if all are taken.

        GM convention: channel 9 is percussion only.  Melodic tracks skip it;
        drum tracks request it directly via is_drums=True.

        Args:
            is_drums: If True, always return channel 9 (the GM drum channel).

        Returns:
            Available channel number, or -1 when all 16 channels are occupied.
        """
        if is_drums:
            return 9  # GM standard: drums live on channel 9

        # Walk channels 0-15, skipping 9 and any already occupied.
        # _vst_players holds VST channels that don't register FluidSynth instruments,
        # so they must be excluded here to prevent channel collisions.
        occupied = set(self._instruments.keys()) | set(self._vst_players.keys())
        for candidate in list(range(0, 9)) + list(range(10, 16)):
            if candidate not in occupied:
                return candidate
        return -1  # all melodic channels full

    # ------------------------------------------------------------------
    # Effect chain integration
    # ------------------------------------------------------------------

    def apply_effect_chain(self, effect_chain) -> None:
        """
        Push an EffectChain's parameters to FluidSynth for its channel.

        Called by the GUI whenever a slider or knob changes.  Also pushes
        global reverb/chorus parameters so the shared DSP unit reflects
        the currently focused track's settings.

        Args:
            effect_chain: An `effects.EffectChain` instance.
        """
        effect_chain.apply(self._fs)
        effect_chain.apply_reverb_global(self._fs)
        effect_chain.apply_chorus_global(self._fs)

    def note_on_with_effects(
        self, channel: int, pitch: int,
        velocity: int = DEFAULT_VELOCITY,
        effect_chain=None,
    ) -> None:
        """
        note_on that optionally routes velocity through a compressor first.

        Args:
            channel:      MIDI channel (0-15).
            pitch:        MIDI note number (0-127).
            velocity:     Raw strike velocity (0-127).
            effect_chain: Optional EffectChain whose compressor is applied.
        """
        if effect_chain is not None:
            velocity = effect_chain.compress_velocity(velocity)
        self.note_on(channel, pitch, velocity)

    # ------------------------------------------------------------------
    # WAV / audio export
    # ------------------------------------------------------------------

    def export_to_wav(
        self,
        wav_path: str,
        midi_path: str,
        sf2_path: str,
    ) -> bool:
        """
        Render a MIDI file to WAV using the FluidSynth command-line tool.

        Why command-line instead of pyfluidsynth's streaming API?
            The CLI approach is simpler, supports the full SF2 standard, and
            produces identical output to real-time playback.  It also handles
            multi-track MIDI natively without us managing a sample-accurate
            event loop.

        Requires:
            FluidSynth ≥ 2.0 installed (brew install fluid-synth on macOS).

        Args:
            wav_path:   Output WAV file path.
            midi_path:  Standard MIDI file to render.
            sf2_path:   SoundFont to load for rendering.

        Returns:
            True on success.
        """
        import shutil, subprocess

        binary = None
        for candidate in [
            "/opt/homebrew/bin/fluidsynth",
            "/usr/local/bin/fluidsynth",
            shutil.which("fluidsynth") or "",
        ]:
            if candidate and os.path.isfile(candidate):
                binary = candidate
                break

        if not binary:
            logger.error("fluidsynth binary not found — cannot export WAV.")
            return False

        if not os.path.isfile(sf2_path):
            logger.error("SF2 not found for WAV export: %s", sf2_path)
            return False

        cmd = [
            binary, "-ni",                       # non-interactive, no MIDI in
            "-F", wav_path,                      # output file
            "-r", "44100",                       # sample rate
            "-g", "0.8",                         # gain
            "-o", "synth.reverb.active=no",      # match DAW default (reverb off)
            "-o", "synth.chorus.active=no",      # match DAW default (chorus off)
            sf2_path, midi_path,                 # soundfont + MIDI
        ]

        logger.info("Rendering WAV: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300
            )
            if result.returncode == 0:
                logger.info("WAV export OK → %s", wav_path)
                return True
            else:
                logger.error("FluidSynth error: %s", result.stderr[:500])
                return False
        except subprocess.TimeoutExpired:
            logger.error("WAV export timed out.")
            return False
        except Exception as exc:
            logger.error("WAV export exception: %s", exc)
            return False

    @staticmethod
    def get_available_sf2_files() -> List[str]:
        """
        Scan common paths for SoundFont (.sf2) files and return their paths.

        Searches in this order:
            1. assets/soundfonts/ next to this module.
            2. /usr/share/sounds/sf2/ (Linux distributions).
            3. ~/Library/Audio/Sounds/Banks/ (macOS user sounds).
            4. /Library/Audio/Sounds/Banks/ (macOS system sounds).

        Returns:
            List of absolute paths to discovered .sf2 files.
        """
        project_root = os.path.dirname(os.path.dirname(__file__))
        search_dirs = [
            os.path.join(project_root, "assets", "soundfonts"),
            "/usr/share/sounds/sf2",
            os.path.expanduser("~/Library/Audio/Sounds/Banks"),
            "/Library/Audio/Sounds/Banks",
        ]
        found: List[str] = []
        for directory in search_dirs:
            if not os.path.isdir(directory):
                continue
            for fname in sorted(os.listdir(directory)):
                if fname.lower().endswith(".sf2"):
                    found.append(os.path.join(directory, fname))
        return found

    # ------------------------------------------------------------------
    # Real-time note events
    # ------------------------------------------------------------------

    def note_on(self, channel: int, pitch: int, velocity: int = 100) -> None:
        """
        Send a MIDI Note-On message to the synthesiser or VST player.

        If a VstRealTimePlayer is registered for *channel* it receives the
        event; otherwise FluidSynth handles it as before.
        """
        # Route to VST real-time player when one is registered for this channel.
        vst_player = self._vst_players.get(channel)
        if vst_player is not None:
            vst_player.note_on(pitch, velocity)
            return

        if not self._running or self._fs is None:
            return

        plugin = self._instruments.get(channel)
        if plugin and plugin.muted:
            return

        any_soloed = any(p.soloed for p in self._instruments.values())
        if any_soloed and plugin and not plugin.soloed:
            return

        try:
            self._fs.noteon(channel, pitch, velocity)
        except Exception as exc:
            logger.debug("note_on error ch=%d pitch=%d: %s", channel, pitch, exc)

    def note_off(self, channel: int, pitch: int) -> None:
        """Send a MIDI Note-Off message to stop a sustained note."""
        vst_player = self._vst_players.get(channel)
        if vst_player is not None:
            vst_player.note_off(pitch)
            return

        if not self._running or self._fs is None:
            return
        try:
            self._fs.noteoff(channel, pitch)
        except Exception as exc:
            logger.debug("note_off error ch=%d pitch=%d: %s", channel, pitch, exc)

    def all_notes_off(self, channel: Optional[int] = None) -> None:
        """
        Immediately silence all notes (panic button).

        Args:
            channel: If given, silence only that channel; otherwise silence
                     every channel in the engine.
        """
        # Silence VST players
        vst_channels = ([channel] if channel is not None
                        else list(self._vst_players.keys()))
        for ch in vst_channels:
            player = self._vst_players.get(ch)
            if player:
                try:
                    # Send note-off for all 128 pitches to flush any held notes
                    for p in range(128):
                        player.note_off(p)
                except Exception:
                    pass

        if not self._running or self._fs is None:
            return
        channels = [channel] if channel is not None else list(self._instruments.keys())
        for ch in channels:
            try:
                # CC 123 = All Notes Off (GM spec).
                self._fs.cc(ch, 123, 0)
            except Exception as exc:
                logger.debug("all_notes_off error ch=%d: %s", ch, exc)

    # ------------------------------------------------------------------
    # Effects & mixing
    # ------------------------------------------------------------------

    def set_gain(self, channel: int, gain: float) -> None:
        """
        Adjust the output volume of a single track.

        FluidSynth uses CC 7 (Volume) on a 0-127 scale.  We normalise the
        0.0–1.0 float range the GUI provides to that integer range.

        Args:
            channel: MIDI channel (0–15).
            gain:    Normalised volume (0.0 = silence, 1.0 = full).
        """
        plugin = self._instruments.get(channel)
        if plugin:
            plugin.gain = max(0.0, min(1.0, gain))
            self._apply_channel_effects(plugin)

    def set_pan(self, channel: int, pan: float) -> None:
        """
        Set stereo pan position for a track.

        CC 10 (Pan) maps -1.0→0, 0.0→64, +1.0→127 in MIDI integers.

        Args:
            channel: MIDI channel (0–15).
            pan:     Position from -1.0 (hard left) to +1.0 (hard right).
        """
        plugin = self._instruments.get(channel)
        if plugin:
            plugin.pan = max(-1.0, min(1.0, pan))
            self._apply_channel_effects(plugin)

    def set_reverb(self, channel: int, amount: float) -> None:
        """
        Control how much of a track's signal is sent to the reverb bus.

        CC 91 (Reverb Send Level) in the GM standard.

        Args:
            channel: MIDI channel (0–15).
            amount:  Reverb wet amount (0.0 = dry, 1.0 = fully wet).
        """
        plugin = self._instruments.get(channel)
        if plugin:
            plugin.reverb_send = max(0.0, min(1.0, amount))
            self._apply_channel_effects(plugin)

    def set_mute(self, channel: int, muted: bool) -> None:
        """Mute or un-mute a channel. Muting is handled in note_on()."""
        plugin = self._instruments.get(channel)
        if plugin:
            plugin.muted = muted
            if muted:
                self.all_notes_off(channel)

    def set_solo(self, channel: int, soloed: bool) -> None:
        """Solo a channel.  All non-soloed channels are silenced in note_on()."""
        plugin = self._instruments.get(channel)
        if plugin:
            plugin.soloed = soloed

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _apply_channel_effects(self, plugin: InstrumentPlugin) -> None:
        """
        Push current gain / pan / reverb values to FluidSynth via CC messages.

        Why CC messages?  They are the standard MIDI way to control mixer
        parameters at the channel level — FluidSynth handles the DSP maths.
        """
        if not self._running or self._fs is None:
            return

        ch = plugin.channel
        # Volume: CC 7, range 0-127.
        volume_cc = int(plugin.gain * 127)
        # Pan: CC 10, centre = 64.
        pan_cc = int((plugin.pan + 1.0) / 2.0 * 127)
        # Reverb send: CC 91.
        reverb_cc = int(plugin.reverb_send * 127)

        try:
            self._fs.cc(ch, 7, volume_cc)
            self._fs.cc(ch, 10, pan_cc)
            self._fs.cc(ch, 91, reverb_cc)
        except Exception as exc:
            logger.debug("Effect CC error on ch=%d: %s", ch, exc)

    # ------------------------------------------------------------------
    # Utility / introspection
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True if the synthesiser is active and ready to accept events."""
        return self._running

    def __repr__(self) -> str:
        return (
            f"AudioEngine(running={self._running}, "
            f"instruments={len(self._instruments)})"
        )