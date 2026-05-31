#pragma once
/*
 * OfflineExporter.h  --  32-bit Float Mix Bus + WAV Writer
 * =========================================================
 * Accumulates stereo audio from multiple sources into one in-memory 32-bit
 * float mix bus, then writes the result to a WAV file with optional TPDF
 * dithering for 16-bit or 24-bit integer output formats.
 *
 * Typical Python usage (via pybind11 bindings):
 *
 *     exporter = daw_processors.OfflineExporter()
 *     exporter.prepare(44100, total_frames)
 *
 *     # For each audio track:
 *     exporter.mix_in(left_f32, right_f32, at_frame, volume=1.0)
 *
 *     exporter.write_wav("output.wav", bit_depth=24)  # or 16, 32
 */

#include <cstdint>
#include <string>
#include <vector>

class OfflineExporter {
public:
    OfflineExporter();

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    /**
     * Allocate the mix bus and configure the timeline.
     * Must be called before mix_in().
     *
     * @param sample_rate   Host sample rate in Hz (e.g. 44100).
     * @param total_frames  Number of stereo frames to allocate (determines
     *                      the maximum export duration).
     */
    void prepare(int sample_rate, int total_frames);

    /** Zero the mix bus without reallocating memory. */
    void reset();

    // ── Mixing ────────────────────────────────────────────────────────────────

    /**
     * Accumulate one audio block into the mix bus.
     *
     * @param left      Pointer to left-channel float32 samples.
     * @param right     Pointer to right-channel float32 samples.
     * @param n_frames  Number of frames in this block.
     * @param at_frame  Timeline offset in frames (clip start position).
     * @param volume    Linear gain applied before accumulation (1.0 = unity).
     */
    void mix_in(const float* left, const float* right,
                int n_frames, int at_frame, float volume = 1.0f);

    // ── WAV output ────────────────────────────────────────────────────────────

    /**
     * Write the accumulated mix to a standard WAV file.
     *
     * For 16-bit and 24-bit output, TPDF (Triangular Probability Density
     * Function) dithering is applied before quantisation to preserve noise-
     * floor linearity and suppress quantisation distortion.
     *
     * @param path       Absolute or relative output file path (UTF-8).
     * @param bit_depth  PCM bit depth: 16 (CD), 24 (broadcast), or 32 (IEEE
     *                   float — no dithering needed).  Defaults to 24.
     * @return           true on success; false if the file cannot be written
     *                   or the mix bus is empty.
     */
    bool write_wav(const std::string& path, int bit_depth = 24) const;

    // ── Metering ──────────────────────────────────────────────────────────────

    /** Peak sample magnitude in the left channel (0..∞). */
    float peak_left()  const;
    /** Peak sample magnitude in the right channel (0..∞). */
    float peak_right() const;

    // ── Accessors ─────────────────────────────────────────────────────────────

    int sample_rate()  const { return sample_rate_;  }
    int total_frames() const { return total_frames_; }

private:
    int sample_rate_  = 44100;
    int total_frames_ = 0;

    std::vector<float> mix_l_;  // left-channel mix bus (float32, total_frames_)
    std::vector<float> mix_r_;  // right-channel mix bus

    // TPDF dither: two uniform [-1,+1) samples summed → triangular [-2,+2).
    // Scaled to ½ LSB at the target bit depth before use.
    static float _tpdf_sample();

    // Little-endian byte helpers for building the WAV header in memory.
    static void _append_le32(std::vector<uint8_t>& buf, uint32_t v);
    static void _append_le16(std::vector<uint8_t>& buf, uint16_t v);
};
