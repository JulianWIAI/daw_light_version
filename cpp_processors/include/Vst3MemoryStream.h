/**
 * Vst3MemoryStream.h -- VST3 IBStream implementation backed by std::vector<uint8_t>.
 * ===================================================================================
 * Provides a read/write in-memory byte stream compatible with the VST3 SDK's
 * IBStream interface.  Used by Vst3StateManager to serialise and restore plugin
 * state without touching the filesystem.
 *
 * Lifetime model:
 *   Instances are always created on the stack or as a local unique_ptr.
 *   The COM ref-count is kept at 1 and self-deletion is suppressed in release()
 *   so the object is safe to pass to VST3 plugins that call addRef/release
 *   during the state save/load call (all well-behaved plugins release before
 *   returning from setState / getState).
 *
 * Usage — writing:
 *   Vst3MemoryStream out;
 *   component->getState(&out);
 *   auto bytes = out.get_data();          // copy to std::vector<uint8_t>
 *
 * Usage — reading:
 *   Vst3MemoryStream in(saved_bytes);     // construct from existing bytes
 *   component->setState(&in);
 */

#pragma once

#ifdef HAVE_VST3_SDK

#include "pluginterfaces/base/ibstream.h"  // Steinberg::IBStream, tresult, int32
#include "pluginterfaces/base/funknown.h"  // Steinberg::FUnknown, TUID

#include <algorithm>  // std::min, std::clamp
#include <cstring>    // memcpy
#include <cstdint>
#include <vector>

class Vst3MemoryStream final : public Steinberg::IBStream {
public:
    // ── Constructors ─────────────────────────────────────────────────────────

    // Empty stream, ready for writing (getState use case).
    Vst3MemoryStream() = default;

    // Pre-filled stream, ready for reading (setState use case).
    explicit Vst3MemoryStream(std::vector<uint8_t> data)
        : data_(std::move(data)) {}

    // ── Data access ──────────────────────────────────────────────────────────

    // Return the written bytes (valid after getState / writing calls).
    const std::vector<uint8_t>& get_data() const noexcept { return data_; }

    // Reset read cursor to the beginning (call before passing to setState).
    void rewind() noexcept { pos_ = 0; }

    // ── IBStream interface ────────────────────────────────────────────────────

    Steinberg::tresult PLUGIN_API read(void* buffer,
                                       Steinberg::int32 numBytes,
                                       Steinberg::int32* numBytesRead) override
    {
        auto available = static_cast<Steinberg::int32>(data_.size()) - static_cast<Steinberg::int32>(pos_);
        Steinberg::int32 to_read = std::max(Steinberg::int32{0},
                                             std::min(numBytes, available));
        if (to_read > 0) {
            std::memcpy(buffer, data_.data() + pos_, static_cast<size_t>(to_read));
            pos_ += static_cast<size_t>(to_read);
        }
        if (numBytesRead) *numBytesRead = to_read;
        return Steinberg::kResultOk;
    }

    Steinberg::tresult PLUGIN_API write(void* buffer,
                                        Steinberg::int32 numBytes,
                                        Steinberg::int32* numBytesWritten) override
    {
        if (numBytes <= 0) { if (numBytesWritten) *numBytesWritten = 0; return Steinberg::kResultOk; }
        size_t needed = pos_ + static_cast<size_t>(numBytes);
        if (needed > data_.size()) data_.resize(needed);
        std::memcpy(data_.data() + pos_, buffer, static_cast<size_t>(numBytes));
        pos_ += static_cast<size_t>(numBytes);
        if (numBytesWritten) *numBytesWritten = numBytes;
        return Steinberg::kResultOk;
    }

    Steinberg::tresult PLUGIN_API seek(Steinberg::int64 pos,
                                       Steinberg::int32 mode,
                                       Steinberg::int64* result) override
    {
        Steinberg::int64 new_pos;
        switch (mode) {
            case kIBSeekSet: new_pos = pos; break;
            case kIBSeekCur: new_pos = static_cast<Steinberg::int64>(pos_) + pos; break;
            case kIBSeekEnd: new_pos = static_cast<Steinberg::int64>(data_.size()) + pos; break;
            default:         return Steinberg::kInvalidArgument;
        }
        // Clamp to valid range.
        pos_ = static_cast<size_t>(
                   std::clamp(new_pos, Steinberg::int64{0},
                              static_cast<Steinberg::int64>(data_.size())));
        if (result) *result = static_cast<Steinberg::int64>(pos_);
        return Steinberg::kResultOk;
    }

    Steinberg::tresult PLUGIN_API tell(Steinberg::int64* pos) override {
        if (pos) *pos = static_cast<Steinberg::int64>(pos_);
        return Steinberg::kResultOk;
    }

    // ── FUnknown interface ────────────────────────────────────────────────────
    // This object is stack-managed; ref-counting suppresses self-deletion.

    Steinberg::tresult PLUGIN_API queryInterface(const Steinberg::TUID iid,
                                                 void** obj) override
    {
        if (Steinberg::FUnknownPrivate::iidEqual(iid, IBStream::iid) ||
            Steinberg::FUnknownPrivate::iidEqual(iid, Steinberg::FUnknown::iid)) {
            *obj = static_cast<IBStream*>(this);
            return Steinberg::kResultOk;
        }
        *obj = nullptr;
        return Steinberg::kNoInterface;
    }

    // Suppress self-deletion: this object is not heap-allocated via COM.
    Steinberg::uint32 PLUGIN_API addRef()  override { return 1; }
    Steinberg::uint32 PLUGIN_API release() override { return 1; }

private:
    std::vector<uint8_t> data_;
    size_t               pos_ = 0;
};

#endif // HAVE_VST3_SDK
