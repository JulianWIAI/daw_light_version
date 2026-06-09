# SBS-Synth Master

A full-featured Digital Audio Workstation built with a **C++ audio core** and a **PySide6 GUI layer**. MIDI sequencing, multi-track mixing, a timeline arranger, a step sequencer, a channel rack, per-track C++ DSP effects, full-project offline mastering export, a real-time master bus with audition modes, AI-assisted tools, and live keyboard / gamepad input — all in one application.

Four instrument backends are supported simultaneously on separate MIDI channels: **SF2 SoundFont**, **SFZ**, **Decent Sampler (.dspreset)**, and **VST3 / Audio Unit** plugins.

---

## Screenshots

### Mixer
![Mixer](assets/screenshots/mixer.png)

### Piano Roll
![Piano Roll](assets/screenshots/piano_roll.png)

### Velocity Editor
![Velocity Editor](assets/screenshots/velocity_editor.png)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       PySide6 GUI Layer                         │
│  MainWindow · PianoRoll · Mixer · Transport · DsPresetPanel     │
│  InstrumentSelectorDialog (SF2 / SFZ / VST3 / DS tabs)         │
└──────────────────────────┬──────────────────────────────────────┘
                           │  pybind11 bindings (GIL released)
┌──────────────────────────▼──────────────────────────────────────┐
│                 C++ daw_processors Extension                     │
│  DecentSamplerEngine · SfizzEngine · SfzParser                  │
│  Vst3BusManager · Vst3StateManager · Vst3AutomationQueue        │
│  Vst3TransportContext · MasterBus · AuditionProcessor           │
│  BrickwallLimiter · AutomationProcessor · FullProjectRenderer   │
│  40+ DSP processors (compressor, EQ, reverb, …)                 │
└──────────────────────────┬──────────────────────────────────────┘
                           │  MIDI routing priority chain
┌──────────────────────────▼──────────────────────────────────────┐
│             Instrument Backend Layer (per MIDI channel)         │
│  VST → SFZ (sfizz) → DS (DecentSampler) → SF2 (FluidSynth)    │
│  sounddevice real-time streams · pygame audio fallback          │
│  pedalboard VST3 / AU hosting                                   │
└─────────────────────────────────────────────────────────────────┘
```

**Design rule:** all audio processing and DSP run in C++ (GIL released via `py::call_guard<py::gil_scoped_release>()`). Python is strictly the GUI and orchestration layer — no audio buffers, no ADSR, no WAV decoding in Python.

---

## Features

### Core MIDI & Sequencing

| Feature | Description |
|---|---|
| **Multi-Track MIDI** | Up to 16 instrument channels, each with its own instrument backend, volume, pan, reverb, mute, and solo |
| **Piano Roll** | Click-and-drag to draw notes; right-click to erase; scroll to navigate; active track vivid, others shown as ghost notes |
| **Velocity Editor** | Per-note velocity bars rendered below the piano roll; chord notes shown side-by-side with pitch labels |
| **Live Recording** | Play keys in real time and capture notes with beat-accurate timing |
| **Step Sequencer / Channel Rack** | 16-step pattern grid for drum and melodic patterns; patterns loop continuously alongside timeline clips |
| **Clip-Based Arrangement** | MIDI and audio clips placed on a scrollable timeline; drag to reposition, right-click to delete |
| **Grid Snap** | Configurable quantise grid (1/4, 1/8, 1/16, 1/32 note, triplets); visual grid lines in the arrange view |
| **Velocity Humanizer** | Gaussian-distributed velocity and timing micro-variation for natural-sounding playback (C++ `VelocityHumanizer`) |

### Instrument Backends

All four backends can be active at the same time on different MIDI channels. The `AudioEngine` routing chain checks VST → SFZ → DS → SF2 per channel.

#### SF2 — General MIDI SoundFont

| Feature | Description |
|---|---|
| **FluidSynth synthesis** | Full General MIDI (128 patches + drums) rendered by FluidSynth in its own audio thread |
| **SoundFont browser** | Category → patch list with live preview keyboard; supports multiple `.sf2` files |
| **GM Instrument catalogue** | All 128 GM patches grouped by family (Piano, Strings, Brass, Synth, …); drums on CH 9 |
| **Per-channel FX** | FluidSynth reverb, chorus, gain, and pan applied per channel from the mixer strip |

#### SFZ — Open Sampler Format

| Feature | Description |
|---|---|
| **C++ SfizzEngine** | Wraps the *sfizz* library for full SFZ v1 / most SFZ v2 opcode support |
| **SIMD-accelerated DSP** | sfizz provides disk-streaming, multi-velocity layers, round-robin cycling, and SIMD mixing |
| **Real-time audio stream** | `SfzRealTimePlayer` opens a *sounddevice* `OutputStream`; audio rendered in the C++ callback with the GIL released |
| **Key-range visualiser** | `SfzKeyRangeWidget` displays sample zones on a colour-coded keyboard graphic |
| **Region inspector** | Scrollable table listing every SFZ region: key range, velocity range, sample path |
| **Python fallback** | `SfizzEnginePython` uses *pygame.mixer* for root-pitch-only playback when the C++ extension is absent |

#### DS — Decent Sampler (.dspreset)

| Feature | Description |
|---|---|
| **C++ DecentSamplerEngine** | Hand-rolled WAV decoder (PCM 16-bit, 24-bit, IEEE float32) + polyphonic voice pool |
| **ADSR envelopes** | Per-voice linear attack/decay/sustain/release in C++; configurable per zone from the preset XML |
| **Pitch-shift** | Playback-rate scaling — `rate = 2^((note − root_note) / 12)` — with linear interpolation between frames |
| **Round-robin sequencing** | C++ `seq_position`/`seq_length` counters per zone group; correct RR zone selected each note-on |
| **Sample looping** | Loop start/end frame with sub-sample accurate wrap-around for sustain loops |
| **Dynamic GUI panel** | `DsPresetPanel` (dock widget) builds native Qt knobs, sliders, and buttons from the `<ui>` block in the XML; auto-updates on load |
| **XML parser** | `dspreset_parser.py` reads the `.dspreset` format into `DsInstrumentInfo` / `DsZoneData` dataclasses — no audio code in Python |
| **MIDI CC automation** | Knobs and sliders declared with a `cc=` attribute in the preset are automatically bound; `apply_cc()` remaps 0–127 to the control's native range |
| **Real-time player** | `DsRealTimePlayer` mirrors `SfzRealTimePlayer`; opens a *sounddevice* stream and feeds the C++ engine's `render()` from the audio callback |
| **Python fallback** | `DsEnginePython` uses *pygame.mixer* for root-pitch-only playback; no pitch shifting in the fallback path |

#### VST3 / Audio Unit

| Feature | Description |
|---|---|
| **Plugin hosting** | *pedalboard* library loads VST3 (Windows/macOS/Linux) and Audio Unit (macOS) plugins |
| **Real-time audio** | `VstRealTimePlayer` opens a *sounddevice* stream and processes MIDI through the plugin in the audio callback |
| **Plugin browser** | Auto-scans standard system plugin directories; manual file picker as fallback |
| **Multi-bus routing** | C++ `Vst3BusManager` queries `IComponent::getBusCount()` / `getBusInfo()` for the full bus topology; activates buses via `IComponent::activateBus()` |
| **State save/restore** | C++ `Vst3StateManager` serialises `IComponent` + `IEditController` state to bytes for project files |
| **Sample-accurate automation** | C++ `Vst3AutomationQueue` implements `IParameterChanges` / `IParamValueQueue`; `Vst3AutomationCurve` maps beat positions to per-block sample offsets |
| **Transport sync** | C++ `Vst3TransportContext` maintains a `ProcessContext` with tempo, time signature, and sample position; updated once per block |
| **Python bridges** | `vst3_host_extensions.py` wraps state/automation/transport; `vst3_bus_manager.py` wraps bus topology — all degrade to no-op stubs without the VST3 SDK |

### Instrument Selector Dialog

The unified **"Change Instrument"** dialog (`InstrumentSelectorDialog`) has four tabs, each colour-coded:

| Tab | Colour | Backend |
|---|---|---|
| SF2  SOUNDFONT | Cyan | FluidSynth + .sf2 file |
| SFZ  INSTRUMENT | Lime green | sfizz C++ engine + .sfz file |
| VST3  PLUGIN | Purple | pedalboard + .vst3 / .component file |
| DS  DECENT SAMPLER | Gold | DecentSamplerEngine + .dspreset file |

The dialog is opened from the mixer strip "Change Instrument" button or from the `Add Instrument Track` workflow.

### Audio Tracks & Import

| Feature | Description |
|---|---|
| **Audio File Tracks** | Import WAV, MP3, FLAC, OGG clips onto the timeline; each clip shows a waveform thumbnail |
| **Waveform Display** | Peak data generated by C++ `WaveformGenerator` for accurate clip previews at any zoom level |
| **Multi-Format Import** | `ImportManager` detects audio vs. MIDI files automatically; drag-and-drop from the file system |
| **Per-Clip DSP** | Each audio track has its own `AudioFxChain`; processed offline before playback via *pedalboard* |

### Mixing & Signal Routing

| Feature | Description |
|---|---|
| **MIDI Mixer Strips** | Volume fader, pan slider, mute, solo, FX button, instrument selector — one strip per MIDI channel |
| **Audio Mixer Strips** | Identical strip layout for audio file tracks; separate gain staging (0–200 %) |
| **Automation Lanes** | Per-track automation curves drawn directly in the arrange view; piecewise-linear interpolation; parameters: volume, pan, any FX plugin parameter |
| **Real-Time Loudness Automation** | C++ `LoudnessAutomation` (RMS analyser + envelope follower + PID controller) keeps integrated loudness on target during playback |
| **Spectral Panning** | C++ `SpectralPanningProcessor` — frequency-dependent stereo positioning via FFT; unique spatial field per frequency band |

### Master Bus

| Feature | Description |
|---|---|
| **C++ MasterBus** | Real-time stereo sum bus; `add_track()` / `process()` run entirely in C++ with the Python GIL released |
| **Master Gain** | Continuous gain control 0–+6 dB applied before the limiter stage |
| **BrickwallLimiter** | Embedded C++ limiter with Catmull-Rom true-peak inter-sample detection; configurable ceiling and timing |
| **Stereo VU Meter** | Dual-channel peak meter with dBFS gradient (cyan → orange → pink); 20 Hz GUI polling; instant-attack / 200 ms decay hold |
| **Audition Mode: Mix** | Normal path — user gain + user limiter; full creative control |
| **Audition Mode: Preview (-7 LUFS)** | Intercepts the normal chain; routes through a hardcoded C++ `AuditionProcessor` (+7 dB pre-gain, −1.0 dBFS true-peak ceiling, 50 ms release) |
| **Audition Mode: Streaming (-14 LUFS)** | Intercepts the normal chain; routes through a dedicated `AuditionProcessor` (0 dB pre-gain, −1.0 dBFS TP ceiling, 150 ms release — transparent streaming limiter) |
| **Thread-Safe Mode Switching** | `set_audition_mode()` writes a `std::atomic<int>` from the GUI thread; audio thread picks it up within one block with zero dropout |

### C++ DSP Effects (daw_processors)

All processors compiled as a single `daw_processors` pybind11 extension; every `process()` call releases the Python GIL.

| Category | Processors |
|---|---|
| **Dynamics** | BrickwallLimiter · MultibandCompressor · DynamicEQ · DeEsser · TransientShaper · GateExpander |
| **Spatial / Time** | DelayEcho · Flanger · Phaser · StereoImager |
| **Harmonic / Character** | Saturation · Overdrive · Bitcrusher · Exciter |
| **Filter / Pitch** | AutoFilter · PitchCorrector · PitchShifter |
| **Sampler** | Sampler (offline wavetable, polyphonic) |
| **Instrument Engines** | DecentSamplerEngine · SfizzEngine (sfizz wrapper) |
| **Metering / Analysis** | RmsAnalyzer · SpectralAnalyzer · EnvelopeFollower · WaveformGenerator |
| **Loudness** | LoudnessAutomation (RMS + PID) |
| **Panning** | SpectralPanningProcessor · SpectralMaskingManager |
| **Grid / Timing** | GridSnapper · QuantizeEngine · TimelineRuler · AudioLoopScheduler |
| **Humanization** | VelocityHumanizer · GaussianRng · TimingWeightFunction |
| **Render Pipeline** | AutomationProcessor · FullProjectRenderer · OfflineExporter · MasterBus · AuditionProcessor |
| **VST3 Hosting** | Vst3BusManager · Vst3StateManager · Vst3AutomationQueue · Vst3TransportContext |

Pure-Python fallbacks (matching the same API) are provided for every C++ class so the application runs without a compiled extension.

### Export & Mastering

| Feature | Description |
|---|---|
| **Multi-Format Mastering Export** | One-click export dialog offering four simultaneous targets |
| **Preview MP3** | −7 LUFS · 320 kbps · heavy limiting — commercial release loudness |
| **Streaming WAV** | −14 LUFS · −1 dBFS true peak · 24-bit — Spotify / Apple Music compliant |
| **Lease WAV** | −3 dBFS peak · 24-bit — headroom-preserved beat lease |
| **Trackout Stems** | Per-track 24-bit WAV files (MIDI stems via FluidSynth; audio stems with FX applied) |
| **Full Project Offline Render** | One FluidSynth pass for all MIDI channels + step-sequencer events; per-track audio decode with FX chains; everything summed via C++ `FullProjectRenderer` with per-frame automation |
| **Per-Frame Automation** | C++ `AutomationProcessor` (binary-search piecewise linear) provides sample-accurate volume and pan curves during export |
| **Progress Tracking** | `MasteringExportWorker` (QThread) emits live progress and status signals; cancellable at any point |
| **LUFS Measurement** | pyloudnorm primary; RMS + 3.5 dB offset fallback |
| **Simple WAV / MP3 Export** | Legacy single-format export for quick bounces |

### AI Tools

| Feature | Description |
|---|---|
| **AI Smart EQ** | Analyses frequency content and suggests per-band EQ settings |
| **AI Generative Reverb** | Generates reverb impulse responses tuned to the project context |
| **AI Smart Master** | One-click AI mastering pipeline |
| **AI Stem Splitter** | Source-separation to split an audio file into instrument stems |

### Timeline & Navigation

| Feature | Description |
|---|---|
| **Track Arrange View** | Scrollable timeline with MIDI clips and audio clips; zoom with mouse wheel |
| **Audio Loop Scheduler** | C++ `AudioLoopScheduler` computes precise loop boundary positions for glitch-free looping |
| **Timeline Engine** | C++ `TimelineEngine` tracks playback position and fires clip-start callbacks |
| **Project Manager** | Save and load the full project state (tracks, clips, automation, FX chain settings) |

### Input Devices

| Feature | Description |
|---|---|
| **QWERTY Keyboard** | Three rows of keys map to a musical scale; layout rebuilds when scale or root changes |
| **PS5 DualSense** | 8 face / shoulder buttons → scale degrees; Options = play/stop; Share = record |
| **Scale & Root Selector** | Major, Minor, Pentatonic, Blues, Chromatic — all key mappings rebuild instantly |
| **Panic Button** | Silences every note on every channel immediately |

### UI & Workflow

| Feature | Description |
|---|---|
| **Bioluminescent Dark Theme** | Deep-black backgrounds with glowing cyan and hot-pink neon accents; consistent across all panels |
| **DS Preset Panel** | Dynamic dock widget that reads the `<ui>` block of a `.dspreset` file and builds native Qt knobs/sliders/buttons; supports MIDI CC automation |
| **Humanizer Panel** | GUI for the C++ `VelocityHumanizer`; per-track Gaussian spread and timing jitter controls |
| **Grid Settings Panel** | Visual grid resolution picker with live preview |
| **Instrument Preview** | Audition any GM patch before assigning it to a track |
| **FX Rack Widget** | Drag-and-drop insert effect slots; each slot shows the C++ processor name, bypass toggle, and parameter controls |
| **FX Slot Widget** | Individual plugin slot with expand / collapse parameter view |

---

## Installation

### Prerequisites

**Windows (primary)**

```powershell
# Python 3.11+
winget install Python.Python.3.11

# FluidSynth (required for SF2 / MIDI synthesis)
# Download installer from https://www.fluidsynth.org/ and add to PATH

# C++ build tools (required to compile daw_processors)
# Install Visual Studio Build Tools 2022 with "Desktop development with C++"
# CMake 3.18+ and pybind11 also required
```

**macOS**

```bash
brew install fluid-synth python@3.11 cmake pybind11
```

### Clone and install Python dependencies

```bash
git clone https://github.com/JulianWIAI/daw_light_version.git
cd daw_light_version
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

### Build the C++ daw_processors extension (recommended)

```powershell
# Windows (PowerShell)
cd cpp_processors
.\build_win.ps1
```

```bash
# macOS / Linux
cd cpp_processors
pip install .
```

The C++ extension provides GIL-free real-time DSP including the `DecentSamplerEngine` and `SfizzEngine`. If compilation fails the app falls back to pure-Python equivalents automatically — all features remain functional.

### SFZ support (sfizz)

The C++ SFZ engine is built via `FetchContent` in CMakeLists.txt — it downloads **sfizz** from GitHub automatically at configure time. No manual step is required. To use a local sfizz build instead:

```bash
cmake -B build -DSFIZZ_ROOT=/path/to/sfizz
```

### VST3 / Audio Unit support (optional)

```bash
# Hosting via pedalboard (VST3 on all platforms, AU on macOS)
pip install pedalboard sounddevice

# Advanced hosting extensions (state, automation, transport) require the VST3 SDK:
cmake -B build -DVST3_SDK_ROOT=/path/to/vst3sdk
# Without the SDK the Vst3BusManager / Vst3StateManager classes compile as no-op stubs.
```

### Decent Sampler presets

Download any free `.dspreset` instrument pack (e.g. from **Decent Samples**). No installation needed — use **"◈ Load DS Preset…"** in the Add Track dialog or open the **DS INSTRUMENT** dock panel and click **Load .dspreset…**.

### Add a SoundFont (SF2)

Download a free General MIDI SoundFont (e.g. **GeneralUser GS** or **FluidR3 GM**) and place the `.sf2` file in `assets/soundfonts/`. The app discovers it automatically on startup.

### Optional dependencies

```bash
# MP3 export (Windows: install ffmpeg and add to PATH)
# macOS: brew install ffmpeg

# AI tools
pip install torch torchaudio demucs
```

### Run

```bash
python main.py
```

---

## Keyboard Controls

| Keys | Action |
|---|---|
| `Z X C V B N M , . /` | Scale degrees, lowest octave |
| `A S D F G H J K L ; '` | Scale degrees, middle octave |
| `Q W E R T Y U I O P [ ]` | Scale degrees, highest octave |
| `Space` | Play / Stop |

---

## PS5 DualSense Mapping

| Button | Scale Degree | Example (C Major) |
|---|---|---|
| Cross (✗) | Root | C |
| Circle (○) | 2nd | D |
| Square (□) | 3rd | E |
| Triangle (△) | 4th | F |
| L1 | 5th | G |
| R1 | 6th | A |
| L2 | 7th | B |
| R2 | Octave | C (high) |
| Options | Play / Stop | — |
| Share | Record | — |

Connect via USB or Bluetooth, then click **Connect Gamepad** in the toolbar.

---

## Mastering Export Targets

Click **🎚 MASTER** in the toolbar to open the export dialog.

| Target | Loudness | Peak | Format | Use case |
|---|---|---|---|---|
| Preview MP3 | −7 LUFS | — | 320 kbps MP3 | Social media, SoundCloud, YouTube |
| Streaming WAV | −14 LUFS | −1 dBFS TP | 24-bit WAV | Spotify, Apple Music, Tidal |
| Lease WAV | — | −3 dBFS | 24-bit WAV | Beat lease / licence delivery |
| Trackout Stems | — | −3 dBFS | 24-bit WAV per track | Producer stems delivery |

The same offline render (FluidSynth + audio clips + automation) feeds all four targets simultaneously. Each target applies its own mastering chain (gain normalisation → BrickwallLimiter → dither where applicable).

---

## Audition Modes

The **MASTER** strip in the mixer has three real-time audition modes accessible from the [MIX] / [PREV] / [STRM] button group:

| Mode | Button | Processing | Purpose |
|---|---|---|---|
| Mix (Bypass) | MIX | User gain + user BrickwallLimiter | Normal creative mixing |
| Preview (-7 LUFS) | PREV | +7 dB pre-gain → C++ AuditionProcessor → −1 dBFS TP | Hear how the mix sounds at commercial streaming loudness |
| Streaming (-14 LUFS) | STRM | 0 dB pre-gain → C++ AuditionProcessor → −1 dBFS TP | Hear the reference streaming level |

Switching modes writes a `std::atomic<int>` from the GUI thread — no locks, no dropouts, the audio thread picks it up within one block.

---

## Project Structure

```
daw_light_version/
├── main.py                          # Entry point
├── requirements.txt
├── assets/
│   ├── icons/
│   ├── soundfonts/                  # Drop .sf2 files here
│   └── screenshots/
├── cpp_processors/                  # C++ DSP extension (daw_processors)
│   ├── include/                     # C++ headers
│   │   ├── DecentSamplerEngine.h    # DS polyphonic sampler engine + DsZoneData struct
│   │   ├── SfizzEngine.h            # sfizz-backed SFZ instrument engine
│   │   ├── SfzParser.h              # SFZ metadata parser (no audio dependency)
│   │   ├── Vst3BusManager.h         # VST3 multi-bus topology + CppBusInfo struct
│   │   ├── Vst3StateManager.h       # VST3 IComponent / IEditController state save/restore
│   │   ├── Vst3AutomationQueue.h    # Sample-accurate IParameterChanges implementation
│   │   ├── Vst3TransportContext.h   # ProcessContext manager for tempo/transport sync
│   │   ├── Vst3MemoryStream.h       # IBStream implementation for state serialisation
│   │   ├── MasterBus.h              # Real-time sum bus + audition routing
│   │   ├── AuditionProcessor.h      # Per-mode loudness processor + AuditionMode enum
│   │   ├── BrickwallLimiter.h       # True-peak look-ahead limiter
│   │   ├── AutomationProcessor.h    # Sample-accurate piecewise-linear curves
│   │   ├── FullProjectRenderer.h    # Offline stereo mix bus
│   │   ├── MultibandCompressor.h
│   │   ├── DynamicEQ.h
│   │   └── ...                      # 30+ more processor headers
│   ├── src/                         # .cpp source files + binding modules
│   │   ├── DecentSamplerEngine.cpp  # WAV decode, ADSR, pitch-shift, voice pool
│   │   ├── SfizzEngine.cpp          # pImpl wrapper around sfizz_synth_t
│   │   ├── SfzParser.cpp            # SFZ text parser (regions, key/vel ranges, CC)
│   │   ├── Vst3BusManager.cpp       # IComponent bus query + activateBus(); stubs if no SDK
│   │   ├── Vst3StateManager.cpp     # getState/setState via Vst3MemoryStream
│   │   ├── Vst3AutomationQueue.cpp  # IParamValueQueue per block
│   │   ├── Vst3TransportContext.cpp # ProcessContext update + advance()
│   │   ├── MasterBus.cpp
│   │   ├── AuditionProcessor.cpp
│   │   ├── FullProjectRenderer.cpp
│   │   ├── bindings.cpp             # pybind11 module entry point
│   │   ├── bindings_sfz.cpp         # SfzParser + SfizzEngine bindings
│   │   ├── bindings_vst3.cpp        # Vst3StateManager / Queue / Transport bindings
│   │   ├── bindings_ds.cpp          # DsZoneData + DecentSamplerEngine + Vst3BusManager bindings
│   │   └── ...
│   ├── CMakeLists.txt               # FetchContent sfizz; optional VST3_SDK_ROOT
│   └── build_win.ps1
└── core/                            # Python application layer
    ├── gui_windows.py               # MainWindow — all UI panels wired together
    ├── audio_engine.py              # MIDI routing: VST → SFZ → DS → FluidSynth
    ├── midi_logic.py                # Sequencer — tracks, clips, recording, playback
    │
    ├── # ── Instrument backends ───────────────────────────────────────────
    ├── sfz_engine_python.py         # SFZ factory (C++ SfizzEngine or pygame fallback)
    ├── sfz_panel.py                 # SfzKeyRangeWidget — zone keyboard visualiser
    ├── sfz_realtime_player.py       # sounddevice stream wrapping SfizzEngine
    ├── dspreset_parser.py           # .dspreset XML → DsInstrumentInfo / DsZoneData
    ├── dspreset_panel.py            # DsPresetPanel dock — knobs/sliders from <ui> block
    ├── dspreset_engine.py           # DS factory (C++ DecentSamplerEngine or pygame fallback)
    ├── dspreset_realtime_player.py  # sounddevice stream wrapping DecentSamplerEngine
    ├── vst_engine.py                # VST3 / AU plugin host (pedalboard)
    ├── vst3_host_extensions.py      # Vst3StateStore / AutomationCurve / TransportBridge
    ├── vst3_bus_manager.py          # Python bridge for C++ Vst3BusManager
    │
    ├── # ── Audio tracks & FX ─────────────────────────────────────────────
    ├── audio_file_player.py         # pygame-based multi-track audio playback
    ├── audio_fx_chain.py            # Per-track C++ plugin chain
    ├── audio_fx_panel.py            # FX chain GUI panel
    ├── fx_rack_widget.py            # Insert effect rack widget
    ├── fx_slot_widget.py            # Single FX slot widget
    ├── fx_plugin_base.py            # Plugin base class
    ├── fx_plugin_registry.py        # Plugin discovery and instantiation
    ├── fx_plugins_cpp.py            # C++ DSP node wrappers (dynamics, spatial, …)
    ├── fx_plugins_filter.py         # Filter plugin wrappers
    ├── fx_plugins_harmonic.py       # Saturation / overdrive / exciter wrappers
    ├── fx_plugins_loudness.py       # LoudnessAutomation wrapper
    ├── fx_plugins_pedalboard.py     # pedalboard-based plugin wrappers
    ├── fx_plugins_pitch.py          # PitchCorrector / PitchShifter wrappers
    ├── fx_plugins_sampler.py        # Sampler plugin wrapper
    ├── fx_plugins_spatial.py        # StereoImager / DelayEcho wrappers
    ├── fx_plugins_spectral_panning.py  # SpectralPanningProcessor wrapper
    │
    ├── # ── Mixing & master bus ───────────────────────────────────────────
    ├── audio_mixer_strip.py         # Audio track mixer strip widget
    ├── master_bus_channel.py        # MasterBusChannel strip widget (VU + audition)
    ├── master_bus_python.py         # C++ MasterBus fallback + factory
    │
    ├── # ── Export & mastering ────────────────────────────────────────────
    ├── export_dialog.py             # Multi-format mastering export dialog
    ├── mastering_export_worker.py   # QThread mastering render worker
    ├── project_render_info.py       # Immutable render snapshot dataclasses
    ├── project_render_pipeline.py   # Full offline render orchestrator
    ├── export_worker.py             # Legacy single-format export worker
    │
    ├── # ── Timeline & automation ─────────────────────────────────────────
    ├── automation_lane.py           # AutomationEnvelope + AutomationPanel widget
    ├── channel_rack.py              # Step-sequencer channel rack
    ├── timeline_engine_bridge.py    # TimelineEngine Python bridge
    ├── instrument_renderer.py       # Offline instrument render helper
    ├── rack_sampler_engine.py       # Channel rack sampler engine
    │
    ├── # ── UI helpers ────────────────────────────────────────────────────
    ├── instrument_preview.py        # Instrument audition widget
    ├── humanizer_panel.py           # Velocity humanizer GUI panel
    ├── grid_settings_panel.py       # Grid resolution GUI panel
    ├── import_manager.py            # File import + format detection
    ├── project_manager.py           # Project save / load
    │
    ├── # ── Pure-Python fallbacks (C++ extension absent) ──────────────────
    ├── automation_processor_python.py
    ├── full_project_renderer_python.py
    ├── grid_snapper_python.py
    ├── velocity_humanizer_python.py
    ├── loudness_automation_python.py
    ├── spectral_panning_python.py
    ├── waveform_peaks_python.py
    ├── audio_loop_scheduler_python.py
    ├── sampler_python.py
    ├── offline_exporter_python.py
    │
    ├── # ── Input & control ───────────────────────────────────────────────
    ├── controller.py                # QWERTY + DualSense input mediator
    ├── effects.py                   # Legacy MIDI per-track effects chain
    │
    └── # ── AI tools ──────────────────────────────────────────────────────
        ├── ai_smart_eq.py
        ├── ai_generative_reverb.py
        ├── ai_smart_master.py
        └── ai_stem_splitter.py
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `PySide6` | Qt6 GUI framework |
| `pyfluidsynth` | FluidSynth bindings for SF2 MIDI synthesis |
| `pygame` | Gamepad input + audio fallback output |
| `mido` | MIDI file I/O |
| `python-rtmidi` | External MIDI device support |
| `numpy` | Audio buffer arithmetic |
| `scipy` | Signal processing utilities |
| `pyloudnorm` | LUFS loudness measurement for export targets |
| `soundfile` | WAV / FLAC read-write |
| `pedalboard` *(optional)* | VST3 / Audio Unit plugin hosting + audio file I/O |
| `sounddevice` *(optional)* | Real-time audio I/O for SFZ and DS players |
| `pybind11` | C++ → Python binding (build-time only) |
| `torch` / `torchaudio` / `demucs` *(optional)* | AI stem splitting |

---

## License

MIT — free for personal, educational, and commercial use.

*Built for a school project — SBS Studio, 2026.*

---

## AI Disclosure

This project was developed in cooperation with AI assistance (Claude by Anthropic). AI was used to help design architecture, generate boilerplate, debug logic, and refine documentation. All creative decisions, feature design, and final code review were done by the author.
