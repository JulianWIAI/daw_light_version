/**
 * TelemetryAnalyzer.h
 *
 * Background DSP analyzer for real-time audio telemetry.
 * The audio thread (producer) pushes raw mono chunks into a lock-free SPSC ring
 * buffer.  A low-priority background thread (consumer) pops chunks and computes:
 *   - RMS loudness
 *   - 7 frequency bands (Sub-Bass to Brilliance)
 *   - 12-bin pitch-class chroma
 *   - Harmonic / Percussive energy ratio (lightweight HPSS)
 *   - 480-sample waveform snapshot (oscilloscope view)
 *
 * Python polls get_frame() at 30 FPS — the call is always instant (mutex-only,
 * no DSP on the Python side).
 */

#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
#include <thread>
#include <vector>

#include "TelemetryNoiseFloor.h"

// Number of waveform sample points stored in each telemetry frame
// (matches the typical canvas pixel width of the waveform panel).
static constexpr int TELEMETRY_WAVE_POINTS = 480;

/** Snapshot of all telemetry data for one analysis window.
 *  Copied atomically under a mutex; safe to read from any thread.
 */
struct TelemetryFrame {
    float    rms                           = 0.f;
    float    bands[7]                      = {};  // Sub-Bass → Brilliance, 0-1
    float    chroma[12]                    = {};  // pitch-class energy, normalised
    float    harmonic                      = 0.f; // H / (H+P) ratio
    float    percussive                    = 0.f; // P / (H+P) ratio
    float    waveform[TELEMETRY_WAVE_POINTS] = {}; // recent oscilloscope samples
    uint64_t tick                          = 0;   // monotonic update counter
};

// ─────────────────────────────────────────────────────────────────────────────
// Lock-free SPSC ring buffer
// ─────────────────────────────────────────────────────────────────────────────

/** Single-producer / single-consumer ring buffer for raw audio chunks.
 *  Audio thread calls push(); background analyzer thread calls pop().
 *  Both operations are wait-free (no spin loops, no mutexes).
 */
class AudioRingBuffer {
public:
    static constexpr int SLOTS     = 64;
    static constexpr int MAX_CHUNK = 4096;

    struct Slot {
        float data[MAX_CHUNK];
        int   size;
    };

    /** Push up to MAX_CHUNK samples.  Returns false if the buffer is full. */
    bool push(const float* samples, int count) noexcept;

    /** Pop the next available slot.  Returns false if the buffer is empty. */
    bool pop(Slot& out) noexcept;

private:
    Slot             _slots[SLOTS];
    std::atomic<int> _write{0};  // owned by the producer
    std::atomic<int> _read{0};   // owned by the consumer
};

// ─────────────────────────────────────────────────────────────────────────────
// TelemetryAnalyzer
// ─────────────────────────────────────────────────────────────────────────────

/** Real-time audio telemetry engine.
 *
 *  Usage:
 *    TelemetryAnalyzer ta(44100);
 *    ta.start();
 *    // from audio callback:
 *    ta.push(mono_buffer, num_samples);
 *    // from Python at 30 FPS:
 *    TelemetryFrame f = ta.get_frame();
 */
class TelemetryAnalyzer {
public:
    // FFT window size — power of 2, determines frequency resolution.
    static constexpr int FFT_SIZE  = 2048;
    // Number of STFT frames retained for the HPSS median filters.
    static constexpr int HP_FRAMES = 8;

    explicit TelemetryAnalyzer(int sample_rate = 44100);
    ~TelemetryAnalyzer();

    /** Push raw mono float32 samples from the audio callback.
     *  Non-blocking and wait-free — safe to call from a real-time thread.
     */
    void push(const float* mono, int num_samples) noexcept;

    /** Start the background analyzer thread. */
    void start();

    /** Stop the background analyzer thread (blocks until the thread exits). */
    void stop();

    /** Return a copy of the most recent telemetry frame.
     *  Acquires a mutex only — always fast, safe from any thread.
     */
    TelemetryFrame get_frame() const;

private:
    // Background thread entry point.
    void _worker();

    // Top-level processing pipeline called from the background thread.
    void _process(const float* samples, int n);

    // DSP sub-stages — all run on the background thread only.
    void _accumulate_rms(const float* samples, int n);
    void _apply_hanning(float* buf) const;  // uses precomputed _hanning
    void _run_fft(const float* windowed, float* mags_out) const;
    void _compute_bands(const float* mags, int n_bins, float* out) const;
    void _compute_chroma(const float* mags, int n_bins, float* out) const;
    void _update_hpss(const float* mags, int n_bins);
    void _read_hpss(float& harm_out, float& perc_out) const;
    void _snapshot_waveform(float* out) const;

    // Iterative Cooley-Tukey radix-2 complex FFT (in-place, interleaved re/im).
    static void _fft_inplace(float* re, float* im, int n);

    // Median via std::nth_element — mutates the supplied vector.
    static float _median(std::vector<float>& v);

    int _sr;

    // Shared ring buffer (audio thread → background thread).
    AudioRingBuffer _ring;

    // Per-FFT accumulation: collect FFT_SIZE samples before running the pipeline.
    std::vector<float> _accum;
    int                _accum_pos = 0;

    // Pre-computed Hanning coefficients for FFT_SIZE samples.
    std::vector<float> _hanning;

    // RMS accumulation across the current accumulation window.
    double _rms_sq_sum = 0.0;
    int    _rms_count  = 0;

    // Oscilloscope wave ring: last WAVE_POINTS samples in a circular buffer.
    float _wave_ring[TELEMETRY_WAVE_POINTS] = {};
    int   _wave_pos = 0;  // next write index (modulo WAVE_POINTS)

    // HPSS history: HP_FRAMES most recent FFT magnitude vectors.
    std::array<std::vector<float>, HP_FRAMES> _hp_frames;
    int _hp_idx   = 0;  // next write slot
    int _hp_count = 0;  // frames stored so far (ramps up to HP_FRAMES)

    // Published telemetry state — guarded by _frame_mutex.
    mutable std::mutex _frame_mutex;
    TelemetryFrame     _frame;

    // Background thread lifecycle.
    std::thread       _thread;
    std::atomic<bool> _running{false};

    // ── Noise-floor calibrator ─────────────────────────────────────────────────
    // Self-calibrates during silence frames; subtracts the floor from active
    // spectra before band/chroma computation so only DAW content is analysed.
    // Not guarded by a mutex — used exclusively on the background worker thread.
    TelemetryNoiseFloor _noise_floor{FFT_SIZE / 2 + 1};

    // RMS thresholds for silence detection (background thread only).
    // EPSILON_RMS : input is effectively all-zeros (Python gate closed) —
    //               publish a zero frame; do NOT feed zeros into calibration.
    // SILENCE_RMS : genuine ambient noise (-60 dBFS) — calibrate floor, zero frame.
    // SIGNAL_RMS  : active DAW signal (-50 dBFS) — subtract floor, full analysis.
    static constexpr float EPSILON_RMS = 1e-8f;   // practically zero
    static constexpr float SILENCE_RMS = 1e-3f;   // -60 dBFS
    static constexpr float SIGNAL_RMS  = 3.16e-3f; // -50 dBFS  (6 dB above SILENCE)
};
