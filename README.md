# SBS-Synth Master

A Digital Audio Workstation built in Python — MIDI sequencing, multi-track mixing, live keyboard and gamepad input, a piano roll editor, per-track DSP effects, VST3/Audio Unit plugin hosting, and WAV export.

---

## Screenshots

### Mixer
![Mixer](assets/screenshots/mixer.png)

### Piano Roll
![Piano Roll](assets/screenshots/piano_roll.png)

### Velocity Editor
![Velocity Editor](assets/screenshots/velocity_editor.png)

---

## Features

| Feature | Description |
|---|---|
| **Multi-Track Mixer** | Up to 16 MIDI channels, each with its own instrument, volume, pan, reverb, mute, and solo |
| **Piano Roll** | Click-and-drag to draw notes, right-click to erase, scroll to navigate |
| **Velocity Editor** | Per-note velocity bars below the piano roll — chord notes shown side-by-side with pitch labels |
| **Ghost Notes** | Inactive tracks render as translucent ghost notes so you can compose in context |
| **Live Recording** | Play keys in real time and capture notes with beat-accurate timing |
| **Audio Export** | Render the full composition to **WAV** (lossless), **MP3** (192 kbps via ffmpeg), or **AAC / .m4a** (192 kbps via built-in `afconvert`) |
| **New Project** | One click clears all tracks and resets the sequencer state |
| **SoundFont Support** | Drop any GM-compatible `.sf2` file into `assets/soundfonts/` and it is loaded automatically |
| **VST3 / Audio Unit Support** | Host third-party instrument and effect plugins via the *pedalboard* library (optional) |
| **Per-Track Effects** | 5-band EQ, Reverb, Compressor, and Chorus for every MIDI track |
| **QWERTY Keyboard** | Three rows of keys map to a musical scale (bottom row = low, top row = high) |
| **PS5 DualSense** | 8 face/shoulder buttons map to scale degrees; Options = play/stop, Share = record |
| **Scale & Root Selector** | Major, Minor, Pentatonic, Blues, Chromatic — all key mappings rebuild instantly |
| **Panic Button** | One click silences every note on every channel |
| **Dark Theme** | Bioluminescent crystal UI — deep-black backgrounds with glowing cyan and hot-pink neon accents |
| **Low-Latency Audio** | FluidSynth runs its own C-level audio thread; the GUI thread never blocks |

---

## Installation

### Prerequisites (macOS)

```bash
brew install fluid-synth python@3.11
```

### Clone and install

```bash
git clone https://github.com/JulianWIAI/daw_light_version.git
cd daw_light_version
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### MP3 export (optional)

MP3 encoding requires ffmpeg. Install it via Homebrew:

```bash
/opt/homebrew/bin/brew install ffmpeg
```

AAC (.m4a) export works on every Mac without any additional tools.

### VST plugin support (optional)

```bash
pip install pedalboard sounddevice
```

### Add a SoundFont

Download a free General MIDI SoundFont (e.g. **GeneralUser GS**) and place the `.sf2` file in `assets/soundfonts/`. The app discovers it automatically on startup.

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

## Project Structure

```
daw_light_version/
├── main.py               # Entry point — bootstraps subsystems, sets taskbar icon
├── requirements.txt
├── assets/
│   ├── icons/            # Application icon (icon.png)
│   ├── soundfonts/       # Drop .sf2 files here
│   ├── screenshots/      # README images
│   └── stylesheets/      # dark_theme.qss
└── core/
    ├── audio_engine.py   # FluidSynth wrapper — synthesis, effects, routing
    ├── midi_logic.py     # Sequencer — tracks, notes, recording, playback
    ├── controller.py     # Input mediator — QWERTY + PS5 → note events
    ├── effects.py        # Per-track DSP chain — EQ, Reverb, Compressor, Chorus
    ├── vst_engine.py     # VST3/AU plugin host via pedalboard (optional)
    ├── macos_key_hook.py # Global key capture on macOS
    └── gui_windows.py    # PySide6 UI — MainWindow, PianoRoll, Mixer, Transport
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `PySide6` | Qt6 GUI |
| `pyfluidsynth` | FluidSynth bindings |
| `pygame` | Gamepad input |
| `mido` | MIDI file I/O |
| `python-rtmidi` | External MIDI device support |
| `numpy` | Audio buffer math |
| `pedalboard` *(optional)* | VST3 / Audio Unit plugin hosting |
| `sounddevice` *(optional)* | Audio device I/O for VST rendering |

---

## License

MIT — free for personal, educational, and commercial use.

*Built for a school project — SBS Studio, 2026.*

---

## AI Disclosure

This project was developed in cooperation with AI assistance (Claude by Anthropic). AI was used to help design architecture, generate boilerplate, debug logic, and refine documentation. All creative decisions, feature design, and final code review were done by the author.
