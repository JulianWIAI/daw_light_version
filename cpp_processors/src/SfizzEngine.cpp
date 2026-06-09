/**
 * SfizzEngine.cpp -- sfizz C API wrapper implementation.
 * =============================================================================
 * sfizz.h is included ONLY in this translation unit via the pImpl struct,
 * keeping the sfizz dependency isolated from the pybind11 binding compiler.
 *
 * sfizz build requirements (CMakeLists.txt):
 *   FetchContent_Declare(sfizz GIT_REPOSITORY https://github.com/sfztools/sfizz ...)
 *   target_link_libraries(daw_processors PRIVATE sfizz::sfizz)
 *
 * sfizz C API pitch convention: -8192 (full down) to +8191 (full up).
 * sfizz C API velocity: integer 0-127 for note on/off.
 * sfizz C API CC value: integer 0-127.
 */

#include "SfizzEngine.h"
#include "SfzParser.h"

// sfizz C API — only included here, not in the public header.
#include <sfizz.h>

#include <cstring>   // memset
#include <algorithm> // clamp

// ── pImpl: wraps sfizz_synth_t with RAII cleanup ──────────────────────────────

struct SfizzEngine::Impl {
    // sfizz_synth_t is managed as a unique_ptr with a custom deleter.
    struct SfizzDeleter {
        void operator()(sfizz_synth_t* s) const noexcept {
            if (s) sfizz_free(s);
        }
    };
    std::unique_ptr<sfizz_synth_t, SfizzDeleter> synth;

    // Cached metadata parsed from the last successful load_sfz() call.
    SfzInstrumentInfo metadata;
    std::string       loaded_path;

    Impl() : synth(sfizz_create_synth()) {}
};

// ── Construction / destruction ─────────────────────────────────────────────────

SfizzEngine::SfizzEngine(float sample_rate, int block_size)
    : impl_(std::make_unique<Impl>())
    , sample_rate_(sample_rate)
    , block_size_(block_size)
{
    sfizz_set_sample_rate       (impl_->synth.get(), sample_rate_);
    sfizz_set_samples_per_block (impl_->synth.get(), block_size_);
}

SfizzEngine::~SfizzEngine() = default;

// ── Setup ─────────────────────────────────────────────────────────────────────

void SfizzEngine::set_sample_rate(float sr) {
    sample_rate_ = sr;
    sfizz_set_sample_rate(impl_->synth.get(), sr);
}

void SfizzEngine::set_block_size(int block_size) {
    block_size_ = block_size;
    sfizz_set_samples_per_block(impl_->synth.get(), block_size);
}

// ── Instrument loading ────────────────────────────────────────────────────────

bool SfizzEngine::load_sfz(const std::string& path) {
    // sfizz_load_file returns true on success.
    loaded_ = sfizz_load_file(impl_->synth.get(), path.c_str());
    if (!loaded_) return false;

    // Parse metadata separately via SfzParser (lightweight text scan).
    // sfizz's C API does not expose region introspection, so we do this ourselves.
    impl_->loaded_path = path;
    impl_->metadata    = SfzParser::parse(path);
    return true;
}

bool SfizzEngine::is_loaded() const noexcept { return loaded_; }

SfzInstrumentInfo SfizzEngine::get_metadata() const {
    return impl_->metadata;
}

// ── MIDI event input ──────────────────────────────────────────────────────────

void SfizzEngine::note_on(int delay, int note, int velocity, int /*channel*/) {
    sfizz_send_note_on(impl_->synth.get(), delay, note, velocity);
}

void SfizzEngine::note_off(int delay, int note, int velocity, int /*channel*/) {
    sfizz_send_note_off(impl_->synth.get(), delay, note, velocity);
}

void SfizzEngine::control_change(int delay, int cc, int cc_value, int channel) {
    sfizz_send_cc(impl_->synth.get(), delay, cc, cc_value);
}

void SfizzEngine::pitch_wheel(int delay, int pitch, int channel) {
    // sfizz pitch: -8192 to +8191
    int clamped = std::clamp(pitch, -8192, 8191);
    sfizz_send_pitch_wheel(impl_->synth.get(), delay, channel, clamped);
}

void SfizzEngine::aftertouch(int delay, int pressure, int channel) {
    sfizz_send_channel_aftertouch(impl_->synth.get(), delay, channel, pressure);
}

void SfizzEngine::all_notes_off(int delay) {
    sfizz_all_notes_off(impl_->synth.get(), delay);
}

// ── Audio rendering ───────────────────────────────────────────────────────────

void SfizzEngine::render(float* left, float* right, int num_samples) {
    if (num_samples <= 0) return;

    if (!loaded_) {
        // Output silence when no instrument is loaded.
        std::memset(left,  0, sizeof(float) * static_cast<size_t>(num_samples));
        std::memset(right, 0, sizeof(float) * static_cast<size_t>(num_samples));
        return;
    }

    // sfizz_render_block expects a pointer-to-pointer (channel array).
    // channels[0] = left, channels[1] = right.
    float* channels[2] = { left, right };
    sfizz_render_block(impl_->synth.get(), channels, 2, num_samples);
}
