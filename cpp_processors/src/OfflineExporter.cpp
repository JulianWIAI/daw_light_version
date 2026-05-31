/*
 * OfflineExporter.cpp  --  32-bit Float Mix Bus + WAV Writer
 * ===========================================================
 * See OfflineExporter.h for the public API.
 *
 * WAV format notes:
 *   - 16-bit: PCM signed int16, TPDF dithered, little-endian.
 *   - 24-bit: PCM signed int24 packed as 3 bytes, TPDF dithered.
 *   - 32-bit: IEEE 754 float32, no dithering (wFormatTag = 3).
 *
 * TPDF dithering:
 *   Triangular-PDF dither is formed by adding two independent uniform
 *   random values each in [-0.5, +0.5) LSB, producing a triangular
 *   distribution over [-1, +1) LSB.  This whitens quantisation noise
 *   and removes low-level distortion artefacts without audible hiss
 *   at normal listening levels for 16-bit and 24-bit output.
 */

#include "OfflineExporter.h"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <random>
#include <stdexcept>

// ─────────────────────────────────────────────────────────────────────────────
// Construction
// ─────────────────────────────────────────────────────────────────────────────

OfflineExporter::OfflineExporter() = default;

// ─────────────────────────────────────────────────────────────────────────────
// Lifecycle
// ─────────────────────────────────────────────────────────────────────────────

void OfflineExporter::prepare(int sample_rate, int total_frames) {
    sample_rate_  = (sample_rate  > 0) ? sample_rate  : 44100;
    total_frames_ = (total_frames > 0) ? total_frames : 0;

    mix_l_.assign(static_cast<size_t>(total_frames_), 0.0f);
    mix_r_.assign(static_cast<size_t>(total_frames_), 0.0f);
}

void OfflineExporter::reset() {
    std::fill(mix_l_.begin(), mix_l_.end(), 0.0f);
    std::fill(mix_r_.begin(), mix_r_.end(), 0.0f);
}

// ─────────────────────────────────────────────────────────────────────────────
// Mixing
// ─────────────────────────────────────────────────────────────────────────────

void OfflineExporter::mix_in(const float* left, const float* right,
                             int n_frames, int at_frame, float volume) {
    if (!left || !right || n_frames <= 0 || at_frame < 0) return;

    // Clamp to the allocated bus length so callers cannot overrun.
    const int available = total_frames_ - at_frame;
    const int n         = std::min(n_frames, available);
    if (n <= 0) return;

    const float* src_l = left;
    const float* src_r = right;
    float*       dst_l = mix_l_.data() + at_frame;
    float*       dst_r = mix_r_.data() + at_frame;

    for (int i = 0; i < n; ++i) {
        dst_l[i] += src_l[i] * volume;
        dst_r[i] += src_r[i] * volume;
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Metering
// ─────────────────────────────────────────────────────────────────────────────

float OfflineExporter::peak_left() const {
    float pk = 0.0f;
    for (float s : mix_l_) pk = std::max(pk, std::abs(s));
    return pk;
}

float OfflineExporter::peak_right() const {
    float pk = 0.0f;
    for (float s : mix_r_) pk = std::max(pk, std::abs(s));
    return pk;
}

// ─────────────────────────────────────────────────────────────────────────────
// WAV output helpers
// ─────────────────────────────────────────────────────────────────────────────

void OfflineExporter::_append_le32(std::vector<uint8_t>& buf, uint32_t v) {
    buf.push_back(static_cast<uint8_t>( v         & 0xFF));
    buf.push_back(static_cast<uint8_t>((v >>  8)  & 0xFF));
    buf.push_back(static_cast<uint8_t>((v >> 16)  & 0xFF));
    buf.push_back(static_cast<uint8_t>((v >> 24)  & 0xFF));
}

void OfflineExporter::_append_le16(std::vector<uint8_t>& buf, uint16_t v) {
    buf.push_back(static_cast<uint8_t>( v        & 0xFF));
    buf.push_back(static_cast<uint8_t>((v >>  8) & 0xFF));
}

// Thread-local TPDF generator: sum of two uniform [-0.5, +0.5) values.
float OfflineExporter::_tpdf_sample() {
    static thread_local std::mt19937 rng{std::random_device{}()};
    static thread_local std::uniform_real_distribution<float> dist(-0.5f, 0.5f);
    return dist(rng) + dist(rng);   // triangular distribution over [-1, +1)
}

// ─────────────────────────────────────────────────────────────────────────────
// WAV writing
// ─────────────────────────────────────────────────────────────────────────────

bool OfflineExporter::write_wav(const std::string& path, int bit_depth) const {
    if (mix_l_.empty() || total_frames_ == 0) return false;

    // Clamp bit_depth to valid values; default to 24.
    if (bit_depth != 16 && bit_depth != 24 && bit_depth != 32)
        bit_depth = 24;

    const int    n_frames          = total_frames_;
    const int    n_channels        = 2;
    const int    bytes_per_sample  = bit_depth / 8;
    const int    block_align       = bytes_per_sample * n_channels;
    const int    byte_rate         = sample_rate_ * block_align;
    const size_t data_size_bytes   = static_cast<size_t>(n_frames) * block_align;

    // wFormatTag: 0x0001 = PCM integer, 0x0003 = IEEE float.
    const uint16_t fmt_tag = (bit_depth == 32) ? 0x0003u : 0x0001u;

    // ── Build 44-byte WAV header ──────────────────────────────────────────────
    std::vector<uint8_t> header;
    header.reserve(44);

    // RIFF chunk descriptor
    header.insert(header.end(), {'R', 'I', 'F', 'F'});
    _append_le32(header, static_cast<uint32_t>(36 + data_size_bytes)); // RIFF size
    header.insert(header.end(), {'W', 'A', 'V', 'E'});

    // fmt  sub-chunk (16 bytes for PCM/float)
    header.insert(header.end(), {'f', 'm', 't', ' '});
    _append_le32(header, 16u);                                          // sub-chunk size
    _append_le16(header, fmt_tag);                                      // audio format
    _append_le16(header, static_cast<uint16_t>(n_channels));
    _append_le32(header, static_cast<uint32_t>(sample_rate_));
    _append_le32(header, static_cast<uint32_t>(byte_rate));
    _append_le16(header, static_cast<uint16_t>(block_align));
    _append_le16(header, static_cast<uint16_t>(bit_depth));

    // data sub-chunk header
    header.insert(header.end(), {'d', 'a', 't', 'a'});
    _append_le32(header, static_cast<uint32_t>(data_size_bytes));

    // ── Open output file ──────────────────────────────────────────────────────
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out.is_open()) return false;

    out.write(reinterpret_cast<const char*>(header.data()),
              static_cast<std::streamsize>(header.size()));

    // ── Write interleaved sample data ─────────────────────────────────────────
    if (bit_depth == 32) {
        // 32-bit IEEE float — no dithering or clipping needed.
        for (int i = 0; i < n_frames; ++i) {
            const float l = mix_l_[i];
            const float r = mix_r_[i];
            out.write(reinterpret_cast<const char*>(&l), 4);
            out.write(reinterpret_cast<const char*>(&r), 4);
        }

    } else if (bit_depth == 24) {
        const float scale      = 8388607.0f;  // 2^23 - 1
        const float dither_amp = 1.0f / scale;

        for (int i = 0; i < n_frames; ++i) {
            const float ch[2] = { mix_l_[i], mix_r_[i] };
            for (int c = 0; c < 2; ++c) {
                // Apply TPDF dither at ½ LSB amplitude.
                float s = ch[c] + _tpdf_sample() * dither_amp;
                s = (s < -1.0f) ? -1.0f : (s > 1.0f ? 1.0f : s);
                const int32_t q = static_cast<int32_t>(s * scale);
                out.put(static_cast<char>( q         & 0xFF));
                out.put(static_cast<char>((q >>  8)  & 0xFF));
                out.put(static_cast<char>((q >> 16)  & 0xFF));
            }
        }

    } else {
        // 16-bit PCM with TPDF dither.
        const float scale      = 32767.0f;
        const float dither_amp = 1.0f / scale;

        for (int i = 0; i < n_frames; ++i) {
            const float ch[2] = { mix_l_[i], mix_r_[i] };
            for (int c = 0; c < 2; ++c) {
                float s = ch[c] + _tpdf_sample() * dither_amp;
                s = (s < -1.0f) ? -1.0f : (s > 1.0f ? 1.0f : s);
                const int16_t q = static_cast<int16_t>(s * scale);
                out.put(static_cast<char>( q        & 0xFF));
                out.put(static_cast<char>((q >>  8) & 0xFF));
            }
        }
    }

    return out.good();
}
