/**
 * Vst3StateManager.cpp -- VST3 state serialization implementation.
 * =================================================================
 * Uses Vst3MemoryStream (our IBStream) to capture and replay plugin state
 * via IComponent::getState() / setState() and IEditController equivalents.
 *
 * Serialized binary format (all little-endian):
 *   offset 0: uint32_t  processor_bytes_count  (N)
 *   offset 4: uint8_t[N] processor_state_data
 *   offset 4+N: uint32_t controller_bytes_count (M)
 *   offset 4+N+4: uint8_t[M] controller_state_data
 */

#include "Vst3StateManager.h"

#ifdef HAVE_VST3_SDK

#include "Vst3MemoryStream.h"

#include <cstring>  // memcpy

// ── Save: capture plugin state into a Vst3PluginState snapshot ───────────────

Vst3PluginState Vst3StateManager::save(
    Steinberg::Vst::IComponent*      component,
    Steinberg::Vst::IEditController* controller)
{
    Vst3PluginState state;

    // Save the audio-processor state (DSP parameters, filter states, etc.).
    if (component) {
        Vst3MemoryStream out;
        if (component->getState(&out) == Steinberg::kResultOk)
            state.processor = out.get_data();
    }

    // Save the edit-controller state (GUI-side parameters separate from the processor).
    if (controller) {
        Vst3MemoryStream out;
        if (controller->getState(&out) == Steinberg::kResultOk)
            state.controller = out.get_data();
    }

    return state;
}

// ── Restore: apply a snapshot back into the plugin ───────────────────────────

bool Vst3StateManager::restore(
    Steinberg::Vst::IComponent*      component,
    Steinberg::Vst::IEditController* controller,
    const Vst3PluginState&           state)
{
    bool ok = false;

    // Restore processor state — pass the raw bytes back via a read-mode stream.
    if (component && !state.processor.empty()) {
        Vst3MemoryStream in(state.processor);
        ok |= (component->setState(&in) == Steinberg::kResultOk);
    }

    // Restore controller state.
    if (controller && !state.controller.empty()) {
        Vst3MemoryStream in(state.controller);
        ok |= (controller->setState(&in) == Steinberg::kResultOk);

        // After restoring controller state, notify it that the component state
        // has also changed so it can update its parameter mirrors.
        if (!state.processor.empty()) {
            Vst3MemoryStream in2(state.processor);
            controller->setComponentState(&in2);
        }
    }

    return ok;
}

// ── Serialize: pack Vst3PluginState into a flat byte vector ──────────────────

std::vector<uint8_t> Vst3StateManager::serialize(const Vst3PluginState& state) {
    std::vector<uint8_t> out;
    out.reserve(8 + state.processor.size() + state.controller.size());

    // Helper: append a uint32_t in little-endian format.
    auto append_u32 = [&](uint32_t v) {
        uint8_t b[4] = {
            static_cast<uint8_t>(v),
            static_cast<uint8_t>(v >> 8),
            static_cast<uint8_t>(v >> 16),
            static_cast<uint8_t>(v >> 24)
        };
        out.insert(out.end(), b, b + 4);
    };

    append_u32(static_cast<uint32_t>(state.processor.size()));
    out.insert(out.end(), state.processor.begin(),   state.processor.end());

    append_u32(static_cast<uint32_t>(state.controller.size()));
    out.insert(out.end(), state.controller.begin(),  state.controller.end());

    return out;
}

// ── Deserialize: unpack a flat byte vector into a Vst3PluginState ────────────

Vst3PluginState Vst3StateManager::deserialize(const std::vector<uint8_t>& data) {
    Vst3PluginState state;
    if (data.size() < 8) return state; // too short to hold even the two length fields

    size_t pos = 0;

    // Helper: read uint32_t little-endian.
    auto read_u32 = [&]() -> uint32_t {
        uint32_t v = static_cast<uint32_t>(data[pos])
                   | (static_cast<uint32_t>(data[pos+1]) << 8)
                   | (static_cast<uint32_t>(data[pos+2]) << 16)
                   | (static_cast<uint32_t>(data[pos+3]) << 24);
        pos += 4;
        return v;
    };

    // Processor state.
    uint32_t proc_len = read_u32();
    if (pos + proc_len > data.size()) return state;
    state.processor.assign(data.begin() + static_cast<ptrdiff_t>(pos),
                            data.begin() + static_cast<ptrdiff_t>(pos + proc_len));
    pos += proc_len;

    // Controller state.
    if (pos + 4 > data.size()) return state;
    uint32_t ctrl_len = read_u32();
    if (pos + ctrl_len > data.size()) return state;
    state.controller.assign(data.begin() + static_cast<ptrdiff_t>(pos),
                             data.begin() + static_cast<ptrdiff_t>(pos + ctrl_len));

    return state;
}

#endif // HAVE_VST3_SDK
