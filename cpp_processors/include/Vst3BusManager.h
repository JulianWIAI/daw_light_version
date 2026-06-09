/**
 * Vst3BusManager.h -- Multi-bus audio routing manager for VST3 plugins.
 * =======================================================================
 * Queries a loaded VST3 plugin's IAudioProcessor for its bus topology
 * (number of buses, channel counts, bus types) and manages per-bus
 * AudioBusBuffers for passing to IAudioProcessor::process().
 *
 * Compiled with full VST3 SDK functionality when HAVE_VST3_SDK is defined.
 * When the SDK is absent the class compiles as a no-op stub so callers
 * do not need conditional #ifdefs outside this file.
 *
 * Thread safety:
 *   All methods should be called from the main thread EXCEPT
 *   prepare_process_buffers() which may be called from the audio thread
 *   after a successful activate().
 *
 * Typical lifecycle:
 *   1. Vst3BusManager mgr;
 *   2. mgr.load_topology(processor_ptr);     // query bus count + info
 *   3. mgr.activate();                       // tell plugin buses are active
 *   4. mgr.prepare_process_buffers(left, right, block_size); // each block
 *   5. ... pass to IAudioProcessor::process() ...
 *   6. mgr.deactivate();                     // cleanup
 */

#pragma once

#include <cstdint>
#include <string>
#include <vector>

// Forward declaration — resolves to the real VST3 type when SDK is available
// and to a dummy struct otherwise, so the header compiles either way.
#ifdef HAVE_VST3_SDK
#include <pluginterfaces/vst/ivstaudioprocessor.h>
#include <pluginterfaces/vst/ivstcomponent.h>
#else
namespace Steinberg { namespace Vst {
    // Minimal stubs so the header parses without the SDK.
    struct AudioBusBuffers {};
    enum BusType { kMain = 0, kAux = 1 };
    enum MediaType { kAudio = 0 };
    enum BusDirection { kInput = 0, kOutput = 1 };
    struct BusInfo {
        BusType   busType;
        int32_t   channelCount;
        char16_t  name[128];
    };
}} // namespace Steinberg::Vst
#endif // HAVE_VST3_SDK


// ── Python-friendly plain-struct mirror ──────────────────────────────────────
// pybind11 binds this struct; it mirrors the VST3 BusInfo fields that are
// safe to expose without SDK headers in Python consumer code.

struct CppBusInfo {
    std::string name;           // UTF-8 bus name.
    int         channel_count;  // Channels (1=mono, 2=stereo, 6=5.1…).
    std::string bus_type;       // "main" or "aux".
    bool        default_active; // Whether the bus should be active by default.
};


// ── Main class ────────────────────────────────────────────────────────────────

class Vst3BusManager {
public:
    Vst3BusManager();
    ~Vst3BusManager();

    // Non-copyable.
    Vst3BusManager(const Vst3BusManager&)            = delete;
    Vst3BusManager& operator=(const Vst3BusManager&) = delete;

    // ── Topology query ────────────────────────────────────────────────────────

    /**
     * Query bus topology from a raw IComponent pointer.
     * Pass the pointer as a uintptr_t obtained from ctypes / your VST3 host.
     *
     * @returns true on success (i.e. at least one audio bus was found).
     */
    bool load_topology(uintptr_t component_ptr);

    /** Number of audio input buses. */
    int input_bus_count()  const noexcept;

    /** Number of audio output buses. */
    int output_bus_count() const noexcept;

    /** Return metadata for all input buses (Python-friendly plain structs). */
    std::vector<CppBusInfo> get_input_buses()  const;

    /** Return metadata for all output buses. */
    std::vector<CppBusInfo> get_output_buses() const;

    /** Total channel count across all active output buses. */
    int total_output_channels() const noexcept;

    // ── Activation ────────────────────────────────────────────────────────────

    /**
     * Activate or deactivate one bus.
     *
     * is_input -- true for input bus, false for output bus.
     * index    -- 0-based bus index.
     * active   -- true to activate, false to deactivate.
     *
     * Calls IComponent::activateBus() when the SDK is available.
     */
    bool set_bus_active(bool is_input, int index, bool active);

    /** Activate all buses at their default state.  Call before the first block. */
    void activate();

    /** Deactivate all buses (e.g. before unloading the plugin). */
    void deactivate();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
