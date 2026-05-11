"""
effects.py — Per-instrument DSP Effect Chain
=============================================
Each instrument track owns one EffectChain instance that stores all
effect parameters and knows how to push them to FluidSynth.

Available effects:
    EQ (5-band graphic)  — maps to FluidSynth filter CCs per channel.
    Reverb               — CC 91 send level + global room/damp/width settings.
    Compressor           — velocity-domain gain reduction before note_on.
    Chorus               — CC 93 send level + global speed/depth settings.

Design note:
    FluidSynth's per-channel capabilities are mostly MIDI CCs (0-127).
    Global reverb/chorus DSP parameters affect all channels that send to
    them — just like a real hardware reverb unit on a mixing desk.
    The per-channel send level is what makes each instrument sound different.

    The Compressor is purely a pre-processing step: it scales the velocity
    integer before sending note_on to FluidSynth.  No DSP required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EffectChain:
    """
    All real-time DSP parameters for one instrument track.

    Attributes use float ranges that match the physical world (dB, Hz, ms)
    so the GUI can display meaningful units.  The `apply()` method converts
    them to the integer ranges FluidSynth expects.
    """

    channel: int  # MIDI channel this chain belongs to.

    # ── 5-Band Graphic EQ ───────────────────────────────────────────────────
    # Each band is ±12 dB.  Mapped to FluidSynth filter CCs per channel.
    #   eq_32  → CC 74 low adjust (filter cutoff — affects low-end body)
    #   eq_250 → CC 71 resonance (adds warmth/mud to low-mids)
    #   eq_1k  → CC 11 expression (gentle presence boost)
    #   eq_4k  → CC 74 high adjust (brightness / air)
    #   eq_16k → CC 72 release character (shimmer)
    eq_enabled: bool  = True
    eq_32:      float = 0.0   # -12 … +12 dB
    eq_250:     float = 0.0
    eq_1k:      float = 0.0
    eq_4k:      float = 0.0
    eq_16k:     float = 0.0

    # ── Reverb ───────────────────────────────────────────────────────────────
    reverb_enabled: bool  = False  # off by default — avoids tunnel effect
    reverb_room:    float = 0.3   # 0 … 1  (small room → cathedral)
    reverb_damp:    float = 0.6   # 0 … 1  (0=bright, 1=muffled)
    reverb_width:   float = 0.5   # 0 … 1  (mono → stereo spread)
    reverb_level:   float = 0.15  # 0 … 1  → CC 91 send level

    # ── Compressor ──────────────────────────────────────────────────────────
    comp_enabled:   bool  = False
    comp_threshold: float = 90.0   # MIDI velocity where gain reduction starts
    comp_ratio:     float = 4.0    # compression ratio above threshold
    comp_attack:    float = 10.0   # ms  (shown in UI; velocity-domain has no attack)
    comp_release:   float = 100.0  # ms  (shown in UI)

    # ── Chorus ───────────────────────────────────────────────────────────────
    chorus_enabled: bool  = False
    chorus_level:   float = 0.3   # 0 … 1  → CC 93 send level
    chorus_speed:   float = 0.3   # Hz, 0.1 … 5.0
    chorus_depth:   float = 8.0   # ms, 0 … 30

    # ------------------------------------------------------------------
    # Core methods
    # ------------------------------------------------------------------

    def apply(self, fs) -> None:
        """
        Push all parameters to FluidSynth for this channel.

        Call this whenever any parameter changes.  The CC writes are
        cheap (< 1 µs each) so it is safe to call from a slider callback.

        Args:
            fs: A live `fluidsynth.Synth` instance, or None (silent mode).
        """
        if fs is None:
            return

        try:
            # ── Reverb send (CC 91) ──────────────────────────────────────
            rev_cc = int(self.reverb_level * 127) if self.reverb_enabled else 0
            fs.cc(self.channel, 91, _clamp(rev_cc))

            # ── Chorus send (CC 93) ──────────────────────────────────────
            cho_cc = int(self.chorus_level * 127) if self.chorus_enabled else 0
            fs.cc(self.channel, 93, _clamp(cho_cc))

            if self.eq_enabled:
                # High-end: combine 4 kHz and 16 kHz → filter brightness CC 74.
                high_avg = (self.eq_4k + self.eq_16k) / 2.0   # -12 … +12
                cutoff = int(64 + (high_avg / 12.0) * 63)     # 0 … 127
                fs.cc(self.channel, 74, _clamp(cutoff))

                # Low-end: 32 Hz band → filter resonance CC 71.
                low_norm = self.eq_32 / 12.0                   # -1 … +1
                resonance = int(64 + low_norm * 30)
                fs.cc(self.channel, 71, _clamp(resonance))

                # Mid presence: 1 kHz → expression CC 11.
                mid_norm = self.eq_1k / 12.0
                expression = int(127 + mid_norm * 20)          # 107 … 127 range
                fs.cc(self.channel, 11, _clamp(expression))
            else:
                # Reset filter CCs to neutral.
                fs.cc(self.channel, 74, 64)
                fs.cc(self.channel, 71, 64)
                fs.cc(self.channel, 11, 127)

        except Exception as exc:
            logger.debug("EffectChain.apply error ch=%d: %s", self.channel, exc)

    def apply_reverb_global(self, fs) -> None:
        """
        Update the shared reverb unit's room/damp/width from this chain.

        Because FluidSynth has one global reverb unit, only the currently
        selected track's parameters are pushed here.  Other tracks still
        hear the reverb at their own send level (CC 91).

        When reverb is disabled, the global output level is zeroed so the
        FluidSynth reverb unit produces no audible output regardless of
        the per-channel CC 91 send levels.

        Args:
            fs: A live `fluidsynth.Synth` instance.
        """
        if fs is None:
            return
        try:
            if not self.reverb_enabled:
                fs.set_reverb(0.2, 0.5, 0.5, 0.0)
            else:
                fs.set_reverb(
                    self.reverb_room,
                    self.reverb_damp,
                    self.reverb_width,
                    self.reverb_level,
                )
        except Exception as exc:
            logger.debug("Reverb global apply error: %s", exc)

    def apply_chorus_global(self, fs) -> None:
        """
        Update the shared chorus unit's speed/depth from this chain.

        Same caveat as reverb — one global unit, per-channel send via CC 93.
        """
        if fs is None or not self.chorus_enabled:
            return
        try:
            # nr=3, level, speed (Hz), depth (ms), type=0 (sinusoidal)
            fs.set_chorus(
                3,
                self.chorus_level,
                self.chorus_speed,
                self.chorus_depth,
                0,
            )
        except Exception as exc:
            logger.debug("Chorus global apply error: %s", exc)

    def compress_velocity(self, velocity: int) -> int:
        """
        Apply velocity-domain gain reduction.

        This is called inside AudioEngine.note_on() before sending to
        FluidSynth.  Velocities at or below the threshold pass through
        unchanged; velocities above are squeezed toward the threshold.

        Example with threshold=90, ratio=4:
            velocity=90  → 90  (no change)
            velocity=110 → 90 + (110-90)/4 = 95
            velocity=127 → 90 + (127-90)/4 ≈ 99

        Args:
            velocity: Raw MIDI velocity (0-127).
        Returns:
            Post-compression velocity (0-127).
        """
        if not self.comp_enabled or velocity <= self.comp_threshold:
            return velocity
        excess = velocity - self.comp_threshold
        compressed = self.comp_threshold + excess / max(1.0, self.comp_ratio)
        return int(_clamp(compressed))

    # ------------------------------------------------------------------
    # Convenience factories
    # ------------------------------------------------------------------

    @classmethod
    def default_for_drums(cls, channel: int) -> "EffectChain":
        """Preset tuned for percussive tracks — light reverb, no chorus."""
        ec = cls(channel=channel)
        ec.reverb_room  = 0.3
        ec.reverb_level = 0.15
        ec.eq_32        = 2.0   # slight low boost for punch
        ec.eq_4k        = -1.0  # tame high transients
        return ec

    @classmethod
    def default_for_bass(cls, channel: int) -> "EffectChain":
        """Preset tuned for bass instruments — minimal reverb, low-end boost."""
        ec = cls(channel=channel)
        ec.reverb_level = 0.08
        ec.eq_32        = 4.0
        ec.eq_250       = 2.0
        ec.eq_4k        = -3.0
        ec.eq_16k       = -4.0
        ec.comp_enabled   = True
        ec.comp_threshold = 80.0
        ec.comp_ratio     = 3.0
        return ec


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: int = 0, hi: int = 127) -> int:
    """Clamp a float to the MIDI integer range."""
    return max(lo, min(hi, int(value)))