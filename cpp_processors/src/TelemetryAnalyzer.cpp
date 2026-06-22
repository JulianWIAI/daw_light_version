/**
 * TelemetryAnalyzer.cpp
 *
 * Implementation of TelemetryAnalyzer and AudioRingBuffer.
 *
 * DSP pipeline (background thread only):
 *   1. Update oscilloscope wave ring from every incoming chunk.
 *   2. Accumulate RMS (sum of squares).
 *   3. Fill FFT accumulation buffer; when FFT_SIZE samples are ready:
 *      a. Apply Hanning window.
 *      b. Run Cooley-Tukey radix-2 FFT → magnitude spectrum.
 *      c. Compute 7 frequency bands.
 *      d. Compute 12-bin pitch-class chroma.
 *      e. Update HPSS frame history; derive H/P ratio via median filters.
 *      f. Publish TelemetryFrame under mutex (also includes RMS + waveform).
 */

#include "TelemetryAnalyzer.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <numeric>

#ifndef M_PI
static constexpr double M_PI = 3.14159265358979323846;
#endif

// Frequency band definitions: { lo_hz, hi_hz } (matching Visualizer.py _7BANDS).
static constexpr float BAND_RANGES[7][2] = {
    {   20.f,    60.f },   // 0  Sub Bass
    {   60.f,   250.f },   // 1  Bass
    {  250.f,   500.f },   // 2  Low Mid
    {  500.f,  2000.f },   // 3  Mid
    { 2000.f,  4000.f },   // 4  High Mid
    { 4000.f,  8000.f },   // 5  High
    { 8000.f, 20000.f },   // 6  Brilliance
};

// ─────────────────────────────────────────────────────────────────────────────
// AudioRingBuffer
// ─────────────────────────────────────────────────────────────────────────────

bool AudioRingBuffer::push(const float* samples, int count) noexcept {
    const int w      = _write.load(std::memory_order_relaxed);
    const int next_w = (w + 1) % SLOTS;
    // Full check: next write position would collide with read position.
    if (next_w == _read.load(std::memory_order_acquire)) return false;

    const int n = std::min(count, MAX_CHUNK);
    std::memcpy(_slots[w].data, samples, static_cast<std::size_t>(n) * sizeof(float));
    _slots[w].size = n;
    _write.store(next_w, std::memory_order_release);
    return true;
}

bool AudioRingBuffer::pop(Slot& out) noexcept {
    const int r = _read.load(std::memory_order_relaxed);
    // Empty check.
    if (r == _write.load(std::memory_order_acquire)) return false;

    out = _slots[r];
    _read.store((r + 1) % SLOTS, std::memory_order_release);
    return true;
}

// ─────────────────────────────────────────────────────────────────────────────
// TelemetryAnalyzer — construction / lifecycle
// ─────────────────────────────────────────────────────────────────────────────

TelemetryAnalyzer::TelemetryAnalyzer(int sample_rate)
    : _sr(sample_rate)
    , _accum(FFT_SIZE, 0.f)
    , _accum_pos(0)
{
    // Pre-compute the Hanning window once to avoid repeated trig per block.
    _hanning.resize(FFT_SIZE);
    for (int i = 0; i < FFT_SIZE; ++i)
        _hanning[i] = 0.5f * (1.f - std::cos(
            2.f * static_cast<float>(M_PI) * i / (FFT_SIZE - 1)));

    // Initialise HPSS frame slots to the correct bin count.
    const int n_bins = FFT_SIZE / 2 + 1;
    for (auto& frame : _hp_frames)
        frame.assign(n_bins, 0.f);
}

TelemetryAnalyzer::~TelemetryAnalyzer() { stop(); }

void TelemetryAnalyzer::start() {
    // Idempotent: ignore if already running.
    if (_running.exchange(true)) return;
    _thread = std::thread(&TelemetryAnalyzer::_worker, this);
}

void TelemetryAnalyzer::stop() {
    if (!_running.exchange(false)) return;
    if (_thread.joinable()) _thread.join();
}

TelemetryFrame TelemetryAnalyzer::get_frame() const {
    std::lock_guard<std::mutex> lk(_frame_mutex);
    return _frame;  // copy under lock; always instant (no DSP here)
}

// ─────────────────────────────────────────────────────────────────────────────
// push — audio thread side (producer)
// ─────────────────────────────────────────────────────────────────────────────

void TelemetryAnalyzer::push(const float* mono, int num_samples) noexcept {
    // Split large buffers into MAX_CHUNK-sized ring slots.
    while (num_samples > 0) {
        const int n = std::min(num_samples, AudioRingBuffer::MAX_CHUNK);
        _ring.push(mono, n);
        mono        += n;
        num_samples -= n;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Background thread
// ─────────────────────────────────────────────────────────────────────────────

void TelemetryAnalyzer::_worker() {
    AudioRingBuffer::Slot slot;
    while (_running.load(std::memory_order_relaxed)) {
        if (_ring.pop(slot)) {
            _process(slot.data, slot.size);
        } else {
            // Buffer empty — sleep briefly to avoid busy-waiting.
            std::this_thread::sleep_for(std::chrono::microseconds(500));
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// _process — top-level DSP pipeline (background thread only)
// ─────────────────────────────────────────────────────────────────────────────

void TelemetryAnalyzer::_process(const float* samples, int n) {
    // 1. Always update the oscilloscope wave ring and RMS accumulator.
    _accumulate_rms(samples, n);
    for (int i = 0; i < n; ++i) {
        _wave_ring[_wave_pos % TELEMETRY_WAVE_POINTS] = samples[i];
        ++_wave_pos;
    }

    // 2. Fill FFT accumulation buffer; trigger pipeline when full.
    const float* src  = samples;
    int remaining     = n;
    while (remaining > 0) {
        const int space = FFT_SIZE - _accum_pos;
        const int copy  = std::min(remaining, space);
        std::memcpy(_accum.data() + _accum_pos, src,
                    static_cast<std::size_t>(copy) * sizeof(float));
        _accum_pos += copy;
        src        += copy;
        remaining  -= copy;

        if (_accum_pos < FFT_SIZE) break;

        // ── FFT block ready ──────────────────────────────────────────────────
        static constexpr int N_BINS = FFT_SIZE / 2 + 1;

        // 3a. Apply Hanning window.
        float windowed[FFT_SIZE];
        std::memcpy(windowed, _accum.data(), FFT_SIZE * sizeof(float));
        _apply_hanning(windowed);

        // 3b. Run FFT → magnitude spectrum.
        float mags[N_BINS];
        _run_fft(windowed, mags);

        // 3c-e. Compute all telemetry fields locally (no lock held yet).
        float rms_val = 0.f;
        if (_rms_count > 0) {
            rms_val     = static_cast<float>(std::sqrt(_rms_sq_sum / _rms_count));
            _rms_sq_sum = 0.0;
            _rms_count  = 0;
        }

        float bands[7]   = {};
        float chroma[12] = {};
        _compute_bands (mags, N_BINS, bands);
        _compute_chroma(mags, N_BINS, chroma);
        _update_hpss   (mags, N_BINS);

        float harm = 0.f, perc = 0.f;
        _read_hpss(harm, perc);

        float waveform[TELEMETRY_WAVE_POINTS];
        _snapshot_waveform(waveform);

        // 3f. Publish under mutex.
        {
            std::lock_guard<std::mutex> lk(_frame_mutex);
            _frame.rms        = rms_val;
            _frame.harmonic   = harm;
            _frame.percussive = perc;
            std::memcpy(_frame.bands,    bands,    sizeof(bands));
            std::memcpy(_frame.chroma,   chroma,   sizeof(chroma));
            std::memcpy(_frame.waveform, waveform, sizeof(waveform));
            ++_frame.tick;
        }

        _accum_pos = 0;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// DSP sub-stages
// ─────────────────────────────────────────────────────────────────────────────

void TelemetryAnalyzer::_accumulate_rms(const float* samples, int n) {
    for (int i = 0; i < n; ++i) {
        const double s = samples[i];
        _rms_sq_sum += s * s;
    }
    _rms_count += n;
}

void TelemetryAnalyzer::_apply_hanning(float* buf) const {
    for (int i = 0; i < FFT_SIZE; ++i)
        buf[i] *= _hanning[i];
}

/** Compute FFT magnitudes for a pre-windowed real input.
 *  Uses thread_local temp buffers to avoid per-call heap allocation.
 */
void TelemetryAnalyzer::_run_fft(const float* windowed, float* mags_out) const {
    // Thread-local to avoid repeated allocation in the background loop.
    static thread_local std::vector<float> re_buf, im_buf;
    re_buf.assign(windowed, windowed + FFT_SIZE);
    im_buf.assign(FFT_SIZE, 0.f);

    _fft_inplace(re_buf.data(), im_buf.data(), FFT_SIZE);

    const int n_bins = FFT_SIZE / 2 + 1;
    for (int i = 0; i < n_bins; ++i)
        mags_out[i] = std::sqrt(re_buf[i] * re_buf[i] + im_buf[i] * im_buf[i]);
}

/** Compute per-band energies.
 *  Each band value = (mean magnitude in band) / (mean magnitude overall),
 *  clamped to [0, 3] then normalised to [0, 1].  This replicates the Python
 *  prototype's normalisation exactly.
 */
void TelemetryAnalyzer::_compute_bands(const float* mags, int n_bins,
                                        float* out) const {
    const float bin_hz = static_cast<float>(_sr) / FFT_SIZE;

    // Global mean magnitude (denominator for per-band normalisation).
    double global_sum = 0.0;
    for (int i = 0; i < n_bins; ++i) global_sum += mags[i];
    const float global_mean = (n_bins > 0)
        ? std::max(static_cast<float>(global_sum / n_bins), 1e-9f) : 1e-9f;

    for (int b = 0; b < 7; ++b) {
        const float lo = BAND_RANGES[b][0];
        const float hi = BAND_RANGES[b][1];
        float band_sum = 0.f;
        int   band_n   = 0;
        for (int i = 0; i < n_bins; ++i) {
            const float f = i * bin_hz;
            if (f >= lo && f < hi) { band_sum += mags[i]; ++band_n; }
        }
        const float band_mean = (band_n > 0) ? band_sum / band_n : 0.f;
        const float raw_val   = band_mean / global_mean;
        out[b] = std::min(raw_val, 3.f) / 3.f;
    }
}

/** Compute 12-bin pitch-class chroma.
 *  Only bins whose frequency falls between 27.5 Hz (A0) and 4186 Hz (C8)
 *  are mapped.  The formula matches the Python prototype:
 *    pc = round(12 * log2(f / 440) + 57) % 12
 */
void TelemetryAnalyzer::_compute_chroma(const float* mags, int n_bins,
                                         float* out) const {
    const float bin_hz = static_cast<float>(_sr) / FFT_SIZE;
    float total        = 0.f;
    std::fill(out, out + 12, 0.f);

    for (int i = 1; i < n_bins; ++i) {  // skip DC bin (i=0)
        const float f = i * bin_hz;
        if (f < 27.5f || f > 4186.f) continue;
        int pc = static_cast<int>(
            std::round(12.f * std::log2(f / 440.f) + 57.f)) % 12;
        if (pc < 0) pc += 12;
        out[pc] += mags[i];
        total   += mags[i];
    }

    // Normalise so chroma sums to 1.
    const float inv = 1.f / std::max(total, 1e-9f);
    for (int pc = 0; pc < 12; ++pc) out[pc] *= inv;
}

/** Store the current FFT magnitude frame in the HPSS circular history. */
void TelemetryAnalyzer::_update_hpss(const float* mags, int n_bins) {
    _hp_frames[_hp_idx].assign(mags, mags + n_bins);
    _hp_idx = (_hp_idx + 1) % HP_FRAMES;
    if (_hp_count < HP_FRAMES) ++_hp_count;
}

/** Derive harmonic and percussive energy ratios from the HPSS frame history.
 *
 *  Harmonic  ≈ mean of (per-frame median across freq bins).
 *  Percussive ≈ mean of (per-bin median across frames).
 *
 *  This replicates numpy's np.median(frames, axis=1) and np.median(frames, axis=0)
 *  from the Python prototype, using std::nth_element for O(n) median.
 */
void TelemetryAnalyzer::_read_hpss(float& harm_out, float& perc_out) const {
    if (_hp_count < 2) { harm_out = 0.f; perc_out = 0.f; return; }

    const int n_frames = _hp_count;
    const int n_bins   = static_cast<int>(_hp_frames[0].size());

    std::vector<float> tmp;
    tmp.reserve(std::max(n_bins, n_frames));

    // Harmonic: median of each frame's frequency magnitudes, averaged over frames.
    float harm_sum = 0.f;
    tmp.resize(n_bins);
    for (int fi = 0; fi < n_frames; ++fi) {
        std::copy(_hp_frames[fi].begin(), _hp_frames[fi].end(), tmp.begin());
        harm_sum += _median(tmp);
    }
    const float harm_energy = harm_sum / n_frames;

    // Percussive: median of each frequency bin across frames, averaged over bins.
    float perc_sum = 0.f;
    tmp.resize(n_frames);
    for (int bi = 0; bi < n_bins; ++bi) {
        for (int fi = 0; fi < n_frames; ++fi)
            tmp[fi] = _hp_frames[fi][bi];
        perc_sum += _median(tmp);
    }
    const float perc_energy = perc_sum / n_bins;

    const float total = harm_energy + perc_energy + 1e-9f;
    harm_out = harm_energy / total;
    perc_out = perc_energy / total;
}

/** Copy the oscilloscope wave ring into a linear display buffer (oldest → newest). */
void TelemetryAnalyzer::_snapshot_waveform(float* out) const {
    const int start = _wave_pos % TELEMETRY_WAVE_POINTS;
    for (int i = 0; i < TELEMETRY_WAVE_POINTS; ++i)
        out[i] = _wave_ring[(start + i) % TELEMETRY_WAVE_POINTS];
}

// ─────────────────────────────────────────────────────────────────────────────
// Static DSP utilities
// ─────────────────────────────────────────────────────────────────────────────

/** Iterative Cooley-Tukey radix-2 complex FFT.
 *  Input/output: interleaved (re[], im[]).  n must be a power of 2.
 */
void TelemetryAnalyzer::_fft_inplace(float* re, float* im, int n) {
    // Bit-reversal permutation.
    for (int i = 1, j = 0; i < n; ++i) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            std::swap(re[i], re[j]);
            std::swap(im[i], im[j]);
        }
    }

    // Butterfly stages.
    for (int len = 2; len <= n; len <<= 1) {
        const float ang = -2.f * static_cast<float>(M_PI) / len;
        const float wr0 = std::cos(ang);
        const float wi0 = std::sin(ang);

        for (int i = 0; i < n; i += len) {
            float wr = 1.f, wi = 0.f;
            for (int j = 0; j < len / 2; ++j) {
                const float ur  = re[i + j];
                const float ui  = im[i + j];
                const float xr  = re[i + j + len / 2];
                const float xi  = im[i + j + len / 2];
                const float tvr = xr * wr - xi * wi;
                const float tvi = xr * wi + xi * wr;

                re[i + j]           = ur + tvr;
                im[i + j]           = ui + tvi;
                re[i + j + len / 2] = ur - tvr;
                im[i + j + len / 2] = ui - tvi;

                // Advance twiddle factor.
                const float new_wr = wr * wr0 - wi * wi0;
                wi = wr * wi0 + wi * wr0;
                wr = new_wr;
            }
        }
    }
}

/** O(n) median using std::nth_element.  Mutates v (makes a sort-partial copy). */
float TelemetryAnalyzer::_median(std::vector<float>& v) {
    if (v.empty()) return 0.f;
    const int mid = static_cast<int>(v.size()) / 2;
    std::nth_element(v.begin(), v.begin() + mid, v.end());
    return v[static_cast<std::size_t>(mid)];
}
