/**
 * Vst3StateManager.h -- VST3 plugin state serialization / restoration.
 * =====================================================================
 * Saves and restores the complete state of a VST3 plugin — both the audio
 * processor component state and the edit controller state — as raw byte
 * vectors that can be stored in a project file (JSON / binary).
 *
 * VST3 state model:
 *   Each plugin has two stateful objects:
 *     IComponent    (audio processor) -- DSP parameters, buffer state
 *     IEditController (GUI controller) -- GUI-only parameters not in the processor
 *   A complete snapshot requires saving BOTH.  Some plugins synchronise them
 *   automatically; others maintain separate states.
 *
 * Serialization format (binary, little-endian):
 *   [4 bytes] processor_state length  (uint32_t, may be 0)
 *   [N bytes] processor_state data
 *   [4 bytes] controller_state length (uint32_t, may be 0)
 *   [M bytes] controller_state data
 *
 * Usage:
 *   // Save
 *   auto state  = Vst3StateManager::save(component, controller);
 *   auto bytes  = Vst3StateManager::serialize(state);    // store in project
 *
 *   // Restore
 *   auto state2 = Vst3StateManager::deserialize(bytes);  // load from project
 *   bool ok     = Vst3StateManager::restore(component, controller, state2);
 */

#pragma once

#ifdef HAVE_VST3_SDK

#include "pluginterfaces/vst/ivstcomponent.h"       // IComponent
#include "pluginterfaces/vst/ivsteditcontroller.h"  // IEditController

#include <cstdint>
#include <vector>

// ── Plugin state snapshot ─────────────────────────────────────────────────────

struct Vst3PluginState {
    std::vector<uint8_t> processor;    // bytes from IComponent::getState()
    std::vector<uint8_t> controller;   // bytes from IEditController::getState()

    bool is_empty() const noexcept {
        return processor.empty() && controller.empty();
    }
};

// ── Manager (all static methods — no instance needed) ─────────────────────────

class Vst3StateManager {
public:
    // Capture the full plugin state into a Vst3PluginState snapshot.
    // Either pointer may be nullptr; that field will be left empty.
    static Vst3PluginState save(Steinberg::Vst::IComponent*       component,
                                Steinberg::Vst::IEditController*  controller);

    // Restore a previously captured state into the plugin.
    // Returns true if at least one of the two restores succeeded.
    static bool restore(Steinberg::Vst::IComponent*       component,
                        Steinberg::Vst::IEditController*  controller,
                        const Vst3PluginState&            state);

    // Pack a Vst3PluginState into a flat byte vector for project serialization.
    static std::vector<uint8_t> serialize(const Vst3PluginState& state);

    // Unpack a flat byte vector (produced by serialize()) back to a Vst3PluginState.
    // Returns an empty state on format errors.
    static Vst3PluginState deserialize(const std::vector<uint8_t>& data);
};

#endif // HAVE_VST3_SDK
