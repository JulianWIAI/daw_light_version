"""
mastering_export_worker.py -- Multi-Format Mastering & Export Queue
====================================================================
QThread subclass (MasteringExportWorker) that bounces the complete project —
MIDI tracks, audio clips, automation, and per-track FX chains — into up to
four commercial-target formats in a single offline render queue.

Render pipeline (per export run)
──────────────────────────────────
  1. ProjectRenderPipeline.render()        → raw (2, N) float32 stereo mix
       ├─ FluidSynth  MIDI + step events  (C synthesis via Python bindings)
       ├─ pedalboard  audio clip decode
       ├─ AudioFxChain per-track FX
       └─ C++ FullProjectRenderer         mix bus with per-frame automation
  2. _apply_master_chain()                → mastered (2, N) float32
       ├─ Target A: -7  LUFS + BrickwallLimiter → 320 kbps MP3
       ├─ Target B: -14 LUFS + BrickwallLimiter → 24-bit / 44.1 kHz WAV
       └─ Target C: -3  dBFS peak norm, no limiter → 24-bit WAV
  3. _export_stems()  (Target C only)     → per-track WAV stems

C++ / Python fallback routing
──────────────────────────────
The module imports daw_processors once at load time.  If the import fails
(Win32 error 193, missing runtime) _DP_AVAILABLE is False and every DSP
step falls back to a Python equivalent (pedalboard.Limiter, RMS LUFS, etc.).

Signal contract
───────────────
  progress_updated(int)        0-100 across all targets
  status_changed(str)          human-readable status for the dialog label
  export_finished(bool, str)   True/False success + final summary message
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from PySide6.QtCore import QThread, Signal

from .project_render_info import FullProjectRenderInfo, MidiTrackRenderInfo
from .project_render_pipeline import ProjectRenderPipeline

logger = logging.getLogger(__name__)

# ── Attempt C++ extension import once ────────────────────────────────────────
try:
    import daw_processors as _dp   # type: ignore[import]
    _DP_AVAILABLE: bool = True
except (ImportError, OSError) as _dp_err:
    _dp = None                     # type: ignore[assignment]
    _DP_AVAILABLE = False
    logger.debug(
        "daw_processors unavailable (%s) — mastering will use Python fallbacks.",
        _dp_err,
    )

# ── Export constants ──────────────────────────────────────────────────────────
EXPORT_SR   = 44_100      # all targets share 44.1 kHz output
TAIL_SECS   = 2.0         # extra silence after last clip (reverb tail)
BLOCK_SIZE  = 4_096       # frames per dp.BrickwallLimiter process_block call


# ═════════════════════════════════════════════════════════════════════════════
# Data models
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class TrackRenderInfo:
    """
    All data needed to render one audio file track in the mastering pipeline.

    Populated by the main window before opening ExportDialog; the worker
    never touches live GUI objects.
    """
    track_id:   int
    name:       str
    clips:      list           # list[AudioClip] from midi_logic
    fx_chain:   object         # AudioFxChain or None
    volume:     float = 1.0    # 0..2  linear gain from audio mixer strip
    pan:        float = 0.0    # -1 (L) .. +1 (R)
    automation: list  = field(default_factory=list)  # list[AutomationRenderInfo]


@dataclass
class ExportConfig:
    """
    Parameters for one mastering / export target.

    Class-method constructors build the four preset configurations so the
    dialog and the worker always agree on what each target means.
    """
    target_name:     str
    enabled:         bool  = True

    # Loudness targeting (None = skip LUFS processing).
    lufs_target:     Optional[float] = None
    # Brick-wall ceiling applied after LUFS gain (dBFS).
    limiter_ceil_db: float = -0.3
    # Apply peak normalisation to this level (None = skip).
    normalize_peak_db: Optional[float] = None
    # Whether to run the brickwall limiter at all.
    apply_limiter:   bool  = True

    # File format and quality.
    output_format:   str   = "wav"   # "wav" | "mp3"
    bit_depth:       int   = 24      # 16 | 24 | 32
    sample_rate:     int   = EXPORT_SR
    mp3_bitrate_kbps: int  = 320

    # Whether to also export each track as its own WAV file.
    include_stems:   bool  = False

    # Appended to the project-name base when building the output filename.
    output_suffix:   str   = ""

    # ── Preset factories ──────────────────────────────────────────────────────

    @classmethod
    def preview_mp3(cls) -> "ExportConfig":
        """Target A — Mastered loud, 320 kbps MP3."""
        return cls(
            target_name      = "Preview MP3",
            lufs_target      = -7.0,
            limiter_ceil_db  = -0.5,
            apply_limiter    = True,
            output_format    = "mp3",
            bit_depth        = 16,
            mp3_bitrate_kbps = 320,
            output_suffix    = "_preview",
        )

    @classmethod
    def streaming_wav(cls) -> "ExportConfig":
        """Target B — Streaming-safe, -14 LUFS, -1 dBFS true peak, 24-bit WAV."""
        return cls(
            target_name     = "Streaming WAV",
            lufs_target     = -14.0,
            limiter_ceil_db = -1.0,
            apply_limiter   = True,
            output_format   = "wav",
            bit_depth       = 24,
            output_suffix   = "_streaming",
        )

    @classmethod
    def lease_wav(cls) -> "ExportConfig":
        """Target C — Unmastered mix, -3 dBFS peak headroom, 24-bit WAV."""
        return cls(
            target_name       = "Lease WAV",
            lufs_target       = None,
            normalize_peak_db = -3.0,
            apply_limiter     = False,
            output_format     = "wav",
            bit_depth         = 24,
            output_suffix     = "_lease",
        )

    @classmethod
    def stems_wav(cls) -> "ExportConfig":
        """Target C (stems) — Each track individually, -3 dBFS peak, 24-bit WAV."""
        return cls(
            target_name       = "Trackout Stems",
            lufs_target       = None,
            normalize_peak_db = -3.0,
            apply_limiter     = False,
            output_format     = "wav",
            bit_depth         = 24,
            include_stems     = True,
            output_suffix     = "_stems",
        )


# ═════════════════════════════════════════════════════════════════════════════
# DSP helpers  (module-level so they can be unit-tested independently)
# ═════════════════════════════════════════════════════════════════════════════

def _measure_lufs(audio: np.ndarray, sr: int) -> float:
    """
    Measure integrated loudness (LUFS / LKFS per EBU R128).

    Primary:  pyloudnorm (accurate K-weighted measurement).
    Fallback: RMS + empirical K-weighting offset (~3.5 dB for typical music;
              error < 2 dB for most broadband content).

    Returns a value in dBLUFS; -70.0 indicates silence.
    """
    try:
        import pyloudnorm as pyln  # type: ignore[import]
        meter    = pyln.Meter(sr)
        loudness = meter.integrated_loudness(audio.T.astype(np.float64))
        return float(loudness)
    except (ImportError, Exception):
        pass

    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms < 1e-10:
        return -70.0
    return 20.0 * np.log10(rms) + 3.5   # +3.5 dB empirical K-weighting offset


def _apply_gain_db(audio: np.ndarray, gain_db: float) -> np.ndarray:
    """Multiply audio by a linear gain derived from gain_db."""
    return (audio * (10.0 ** (gain_db / 20.0))).astype(np.float32)


def _apply_brickwall_limiter(
    audio:      np.ndarray,
    ceiling_db: float,
    sr:         int,
    dp_ok:      bool,
) -> np.ndarray:
    """
    Apply a brickwall limiter to a (2, n_frames) float32 buffer.

    Priority:
      1. dp.BrickwallLimiter  (C++ look-ahead — best quality)
      2. pedalboard.Limiter   (Python — good quality, always available)
      3. numpy hard-clip      (last resort — no look-ahead, may distort)

    dp_ok is passed from the module-level _DP_AVAILABLE flag so the caller
    can route to C++ or Python without re-importing.
    """
    # ── C++ BrickwallLimiter ──────────────────────────────────────────────────
    if dp_ok and _dp is not None:
        try:
            limiter = _dp.BrickwallLimiter(float(sr))
            limiter.set_ceiling(ceiling_db)
            limiter.set_lookahead(5.0)
            limiter.set_attack(0.5)
            limiter.set_release(100.0)
            n   = audio.shape[1]
            out = np.zeros_like(audio)
            for i in range(0, n, BLOCK_SIZE):
                j      = min(i + BLOCK_SIZE, n)
                l_in   = np.ascontiguousarray(audio[0, i:j], dtype=np.float32)
                r_in   = np.ascontiguousarray(audio[1, i:j], dtype=np.float32)
                pad    = BLOCK_SIZE - len(l_in)
                actual = len(l_in)
                if pad > 0:
                    l_in = np.pad(l_in, (0, pad))
                    r_in = np.pad(r_in, (0, pad))
                l_out, r_out = limiter.process_block(l_in, r_in)
                out[0, i:i + actual] = l_out[:actual]
                out[1, i:i + actual] = r_out[:actual]
            return out
        except Exception as exc:
            logger.debug("dp.BrickwallLimiter failed: %s — trying pedalboard.", exc)

    # ── pedalboard.Limiter ────────────────────────────────────────────────────
    try:
        from pedalboard import Pedalboard, Limiter  # type: ignore[import]
        board = Pedalboard([Limiter(
            threshold_db = ceiling_db,
            release_ms   = 250.0,
        )])
        return board(audio.astype(np.float32), sr).astype(np.float32)
    except Exception as exc:
        logger.debug("pedalboard.Limiter failed: %s — using hard clip.", exc)

    # ── Hard clip (last resort) ───────────────────────────────────────────────
    ceil_lin = 10.0 ** (ceiling_db / 20.0)
    return np.clip(audio, -ceil_lin, ceil_lin).astype(np.float32)


def _normalize_peak(audio: np.ndarray, target_db: float) -> np.ndarray:
    """Scale audio so its peak sample equals target_db (dBFS)."""
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-10:
        return audio
    return _apply_gain_db(audio, target_db - 20.0 * np.log10(peak))


def _write_wav_file(
    audio:     np.ndarray,   # (2, n_frames) float32
    path:      str,
    sr:        int,
    bit_depth: int,
) -> bool:
    """Write a (2, n_frames) float32 buffer as a WAV file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = np.ascontiguousarray(audio.T, dtype=np.float32)

    # soundfile (broadest subtype support).
    try:
        import soundfile as sf  # type: ignore[import]
        subtype_map = {16: "PCM_16", 24: "PCM_24", 32: "PCM_32"}
        sf.write(path, data, sr, subtype=subtype_map.get(bit_depth, "PCM_24"))
        return True
    except Exception as exc:
        logger.debug("soundfile.write failed: %s — trying OfflineExporterPython.", exc)

    # OfflineExporterPython fallback (no external dependencies).
    try:
        from .offline_exporter_python import OfflineExporterPython
        exp = OfflineExporterPython()
        exp.prepare(sr, audio.shape[1])
        exp.mix_in(
            np.ascontiguousarray(audio[0]),
            np.ascontiguousarray(audio[1]),
            0, 1.0,
        )
        return exp.write_wav(path, bit_depth)
    except Exception as exc:
        logger.error("_write_wav_file: all backends failed for %s: %s", path, exc)
        return False


def _write_mp3_file(
    audio:        np.ndarray,   # (2, n_frames) float32
    path:         str,
    sr:           int,
    bitrate_kbps: int,
) -> bool:
    """Write a (2, n_frames) float32 buffer as a 320 kbps MP3."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    # pedalboard.io.AudioFile (native MP3 encoder — no external tools).
    try:
        from pedalboard.io import AudioFile  # type: ignore[import]
        with AudioFile(path, "w", samplerate=sr, num_channels=2,
                       quality=bitrate_kbps) as f:
            f.write(audio.astype(np.float32))
        return True
    except Exception as exc:
        logger.debug("pedalboard.io MP3 write failed: %s — trying ffmpeg.", exc)

    # ffmpeg fallback.
    tmp_wav = path + "_tmp_master.wav"
    if not _write_wav_file(audio, tmp_wav, sr, 16):
        return False

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        for candidate in (
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            "/usr/local/bin/ffmpeg",
            "/opt/homebrew/bin/ffmpeg",
        ):
            if os.path.isfile(candidate):
                ffmpeg = candidate
                break

    if ffmpeg is None:
        logger.warning("ffmpeg not found — MP3 export requires pedalboard or ffmpeg.")
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass
        return False

    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", tmp_wav, "-b:a", f"{bitrate_kbps}k", path],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode(errors="replace") if exc.stderr else ""
        logger.error("ffmpeg MP3 encode failed: %s", err[:300])
        return False
    finally:
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# MasteringExportWorker
# ═════════════════════════════════════════════════════════════════════════════

class MasteringExportWorker(QThread):
    """
    QThread that executes the multi-target mastering and export pipeline.

    For each enabled ExportConfig:
      1. Render the full project to a raw stereo mix via ProjectRenderPipeline
         (MIDI via FluidSynth, audio clips via pedalboard, FX chains via
         AudioFxChain, mixed in C++ FullProjectRenderer with automation).
      2. Apply the target-specific master chain (LUFS gain + limiter or
         peak normalisation).
      3. Write the output file (WAV via soundfile, MP3 via pedalboard/ffmpeg).
      4. If include_stems: render and export each track individually.

    All UI feedback travels via Qt signals so the GUI thread is never blocked.
    """

    # ── Qt signals ────────────────────────────────────────────────────────────
    progress_updated = Signal(int)          # 0-100 across all targets
    status_changed   = Signal(str)          # log line / status text
    export_finished  = Signal(bool, str)    # success flag + final summary

    def __init__(
        self,
        render_info:  FullProjectRenderInfo,
        configs:      List[ExportConfig],
        output_dir:   str,
        project_name: str,
        flavor_id:    int  = 0,
        parent             = None,
    ) -> None:
        super().__init__(parent)
        self._render_info  = render_info
        self._configs      = [c for c in configs if c.enabled]
        self._output_dir   = output_dir
        self._project_name = project_name or "export"
        self._flavor_id    = max(0, min(2, int(flavor_id)))

    # ── QThread entry point ───────────────────────────────────────────────────

    def run(self) -> None:
        try:
            ok, summary = self._do_export()
            self.export_finished.emit(ok, summary)
        except Exception as exc:
            logger.error("MasteringExportWorker crashed: %s", exc, exc_info=True)
            self.status_changed.emit(f"Fatal error: {exc}")
            self.export_finished.emit(False, str(exc))

    # ── Top-level export loop ─────────────────────────────────────────────────

    def _do_export(self) -> tuple:
        """
        Pre-render the raw mix once, then iterate over targets.

        Returns (overall_success: bool, summary_message: str).
        """
        if not self._configs:
            return False, "No export targets selected."

        os.makedirs(self._output_dir, exist_ok=True)

        # Build the render pipeline from the project snapshot.
        pipeline = ProjectRenderPipeline(self._render_info)

        # ── Pre-render the raw mix ────────────────────────────────────────────
        self.status_changed.emit("Rendering full project mix…")
        raw_mix = pipeline.render(
            status_fn    = lambda msg: self.status_changed.emit(f"  {msg}"),
            cancelled_fn = self.isInterruptionRequested,
        )

        if self.isInterruptionRequested():
            return False, "Export cancelled."

        if raw_mix is None:
            return False, "No audio found — nothing to export."

        # Apply mastering flavor once on the shared render.
        # All targets branch off the same flavored buffer.
        _FLAVOR_NAMES = ("Transparent", "Analog Warmth", "Club/Festival")
        if self._flavor_id != 0:
            self.status_changed.emit(
                f"Applying mastering flavor: {_FLAVOR_NAMES[self._flavor_id]}…"
            )
        flavored_mix = self._apply_flavor(raw_mix)

        n_targets    = len(self._configs)
        ok_count     = 0
        failed_names = []

        for idx, cfg in enumerate(self._configs):
            if self.isInterruptionRequested():
                break

            base_pct = int(idx / n_targets * 100)
            self.progress_updated.emit(base_pct)
            self.status_changed.emit(
                f"[{idx + 1}/{n_targets}] Mastering: {cfg.target_name}…"
            )

            ok = self._export_one_target(flavored_mix, cfg, pipeline, idx, n_targets)
            if ok:
                ok_count += 1
            else:
                failed_names.append(cfg.target_name)

        self.progress_updated.emit(100)

        if failed_names:
            summary = (
                f"{ok_count}/{n_targets} targets completed. "
                f"Failed: {', '.join(failed_names)}"
            )
        else:
            summary = f"All {n_targets} target(s) exported to: {self._output_dir}"

        return len(failed_names) == 0, summary

    def _export_one_target(
        self,
        raw_mix:  np.ndarray,
        cfg:      ExportConfig,
        pipeline: ProjectRenderPipeline,
        idx:      int,
        n_total:  int,
    ) -> bool:
        """Apply master chain for cfg and write the output file(s)."""
        # Apply mastering (LUFS targeting or peak normalisation).
        mastered = self._apply_master_chain(raw_mix.copy(), cfg)

        # Build output path.
        ext  = "mp3" if cfg.output_format == "mp3" else "wav"
        name = f"{self._project_name}{cfg.output_suffix}.{ext}"
        path = os.path.join(self._output_dir, name)

        # Write the full mix.
        ok = self._write_audio(mastered, path, cfg)
        self.status_changed.emit(
            f"  {'✔' if ok else '✘'} {name}"
        )

        # Stem export (optional, per-target).
        if cfg.include_stems and ok:
            ok = ok and self._export_stems(cfg, pipeline)

        return ok

    # ── Mastering flavor (applied once, shared across all targets) ────────────

    def _apply_flavor(self, audio: np.ndarray) -> np.ndarray:
        """
        Apply the selected mastering flavor to the raw stereo mix.

        Returns the input array unmodified for flavor 0 (TRANSPARENT).
        Falls back gracefully if daw_processors is unavailable.
        """
        if self._flavor_id == 0:
            return audio

        if _DP_AVAILABLE and _dp is not None:
            try:
                proc = _dp.MasteringFlavorProcessor(float(EXPORT_SR))
                proc.set_flavor(self._flavor_id)
                n   = audio.shape[1]
                out = np.zeros_like(audio)
                for i in range(0, n, BLOCK_SIZE):
                    j      = min(i + BLOCK_SIZE, n)
                    l_in   = np.ascontiguousarray(audio[0, i:j], dtype=np.float32)
                    r_in   = np.ascontiguousarray(audio[1, i:j], dtype=np.float32)
                    pad    = BLOCK_SIZE - len(l_in)
                    actual = len(l_in)
                    if pad > 0:
                        l_in = np.pad(l_in, (0, pad))
                        r_in = np.pad(r_in, (0, pad))
                    l_out, r_out = proc.process_block(l_in, r_in)
                    out[0, i:i + actual] = l_out[:actual]
                    out[1, i:i + actual] = r_out[:actual]
                return out
            except Exception as exc:
                logger.debug(
                    "MasteringFlavorProcessor failed: %s — skipping flavor.", exc
                )

        return audio  # C++ unavailable: pass through

    # ── Master chain ──────────────────────────────────────────────────────────

    def _apply_master_chain(
        self,
        audio: np.ndarray,
        cfg:   ExportConfig,
    ) -> np.ndarray:
        """
        Apply the correct mastering signal chain based on ExportConfig.

        Target A / B  (LUFS targeting):
            measure LUFS → apply makeup gain → brickwall limiter

        Target C  (peak normalisation):
            measure peak → apply gain to reach normalize_peak_db
        """
        if cfg.lufs_target is not None and cfg.apply_limiter:
            # Targets A & B — LUFS + limiter.
            current_lufs = _measure_lufs(audio, cfg.sample_rate)
            self.status_changed.emit(
                f"  LUFS: {current_lufs:.1f} → target {cfg.lufs_target:.0f}"
            )
            if current_lufs > -69.0:   # not silence
                audio = _apply_gain_db(audio, cfg.lufs_target - current_lufs)
            audio = _apply_brickwall_limiter(
                audio, cfg.limiter_ceil_db, cfg.sample_rate, _DP_AVAILABLE
            )

        elif cfg.normalize_peak_db is not None:
            # Target C — peak normalisation, no limiter.
            audio = _normalize_peak(audio, cfg.normalize_peak_db)

        return audio

    # ── Stem export ───────────────────────────────────────────────────────────

    def _export_stems(
        self,
        cfg:      ExportConfig,
        pipeline: ProjectRenderPipeline,
    ) -> bool:
        """
        Render and write each track individually as a separate WAV stem.

        Audio tracks and MIDI tracks are both included.
        Stems are placed in: <output_dir>/<project>_stems/
        """
        stems_dir = os.path.join(
            self._output_dir, f"{self._project_name}_stems"
        )
        os.makedirs(stems_dir, exist_ok=True)

        all_ok  = True
        n_total = (
            len(self._render_info.audio_tracks)
            + len(self._render_info.midi_tracks)
        )
        done = 0

        # ── Audio track stems ─────────────────────────────────────────────────
        for track in self._render_info.audio_tracks:
            if self.isInterruptionRequested():
                break
            done += 1
            pct = int(done / max(1, n_total) * 100)
            self.progress_updated.emit(pct)
            self.status_changed.emit(
                f"  Stem {done}/{n_total}: {track.name} (audio)"
            )

            stem_audio = pipeline.render_single_audio_track(track)
            if stem_audio is None:
                self.status_changed.emit("    (no audio — skipped)")
                continue

            if cfg.normalize_peak_db is not None:
                stem_audio = _normalize_peak(stem_audio, cfg.normalize_peak_db)

            safe_name = _safe_filename(track.name)
            stem_path = os.path.join(stems_dir, f"{safe_name}.wav")
            ok = _write_wav_file(stem_audio, stem_path, cfg.sample_rate, cfg.bit_depth)
            self.status_changed.emit(
                f"    {'✔' if ok else '✘'} {os.path.basename(stem_path)}"
            )
            if not ok:
                all_ok = False

        # ── MIDI track stems (one FluidSynth pass per channel) ────────────────
        n_frames = pipeline._compute_n_frames()
        for midi_track in self._render_info.midi_tracks:
            if self.isInterruptionRequested():
                break
            done += 1
            pct = int(done / max(1, n_total) * 100)
            self.progress_updated.emit(pct)
            self.status_changed.emit(
                f"  Stem {done}/{n_total}: {midi_track.name} (MIDI)"
            )

            stem_audio = pipeline.render_single_midi_track(midi_track, n_frames)
            if stem_audio is None:
                self.status_changed.emit("    (no MIDI — skipped)")
                continue

            if cfg.normalize_peak_db is not None:
                stem_audio = _normalize_peak(stem_audio, cfg.normalize_peak_db)

            safe_name = _safe_filename(midi_track.name)
            stem_path = os.path.join(stems_dir, f"{safe_name}_midi.wav")
            ok = _write_wav_file(stem_audio, stem_path, cfg.sample_rate, cfg.bit_depth)
            self.status_changed.emit(
                f"    {'✔' if ok else '✘'} {os.path.basename(stem_path)}"
            )
            if not ok:
                all_ok = False

        return all_ok

    # ── File writing ──────────────────────────────────────────────────────────

    def _write_audio(
        self,
        audio: np.ndarray,
        path:  str,
        cfg:   ExportConfig,
    ) -> bool:
        """Dispatch to WAV or MP3 writer based on ExportConfig.output_format."""
        if cfg.output_format == "mp3":
            return _write_mp3_file(
                audio, path, cfg.sample_rate, cfg.mp3_bitrate_kbps
            )
        return _write_wav_file(audio, path, cfg.sample_rate, cfg.bit_depth)


# ── Utility ───────────────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    """Strip characters that are invalid in Windows / POSIX filenames."""
    return "".join(
        c if c.isalnum() or c in " _-" else "_"
        for c in name
    ).strip() or "track"
