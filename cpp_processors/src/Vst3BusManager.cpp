/**
 * Vst3BusManager.cpp -- Multi-bus routing manager for VST3 plugins.
 * ====================================================================
 * When HAVE_VST3_SDK is defined: queries IAudioProcessor/IComponent for
 * bus topology and provides AudioBusBuffers per block.
 * Otherwise: no-op stub so the rest of the build is unaffected.
 */

#include "Vst3BusManager.h"

#include <cstring>
#include <memory>
#include <string>
#include <vector>

#ifdef HAVE_VST3_SDK

// VST3 SDK interface headers.
#include <pluginterfaces/vst/ivstaudioprocessor.h>
#include <pluginterfaces/vst/ivstcomponent.h>
#include <pluginterfaces/base/ibstream.h>

using namespace Steinberg;
using namespace Steinberg::Vst;

// ── Helpers ───────────────────────────────────────────────────────────────────

namespace {

// Convert UTF-16 (char16_t*) bus name to a UTF-8 std::string.
std::string utf16_to_utf8(const char16_t* src, int max_len = 128) {
    std::string out;
    for (int i = 0; i < max_len && src[i] != 0; ++i) {
        uint32_t cp = static_cast<uint32_t>(src[i]);
        if (cp < 0x80) {
            out += static_cast<char>(cp);
        } else if (cp < 0x800) {
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else {
            out += static_cast<char>(0xE0 | (cp >> 12));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        }
    }
    return out;
}

// Convert a VST3 BusType enum to a human-readable string.
const char* bus_type_str(BusType t) {
    return t == kMain ? "main" : "aux";
}

} // anonymous namespace


// ── pImpl ─────────────────────────────────────────────────────────────────────

struct Vst3BusManager::Impl {
    // Raw pointer to the plugin's IComponent interface.
    // We do NOT own this pointer; lifetime is managed by the VST3 host.
    IComponent* component = nullptr;

    struct BusEntry {
        CppBusInfo  info;
        bool        is_input;
        int         index;
        bool        active;
    };

    std::vector<BusEntry> inputs_;
    std::vector<BusEntry> outputs_;

    void clear() {
        component = nullptr;
        inputs_.clear();
        outputs_.clear();
    }

    bool query(IComponent* comp) {
        component = comp;
        inputs_.clear();
        outputs_.clear();

        if (!comp) return false;

        // Query all audio input buses.
        int32_t n_in = comp->getBusCount(kAudio, kInput);
        for (int32_t i = 0; i < n_in; ++i) {
            BusInfo vst_info;
            if (comp->getBusInfo(kAudio, kInput, i, vst_info) == kResultOk) {
                BusEntry e;
                e.info.name          = utf16_to_utf8(vst_info.name);
                e.info.channel_count = static_cast<int>(vst_info.channelCount);
                e.info.bus_type      = bus_type_str(vst_info.busType);
                e.info.default_active= (vst_info.busType == kMain);
                e.is_input = true;
                e.index    = static_cast<int>(i);
                e.active   = e.info.default_active;
                inputs_.push_back(e);
            }
        }

        // Query all audio output buses.
        int32_t n_out = comp->getBusCount(kAudio, kOutput);
        for (int32_t i = 0; i < n_out; ++i) {
            BusInfo vst_info;
            if (comp->getBusInfo(kAudio, kOutput, i, vst_info) == kResultOk) {
                BusEntry e;
                e.info.name          = utf16_to_utf8(vst_info.name);
                e.info.channel_count = static_cast<int>(vst_info.channelCount);
                e.info.bus_type      = bus_type_str(vst_info.busType);
                e.info.default_active= (vst_info.busType == kMain);
                e.is_input = false;
                e.index    = static_cast<int>(i);
                e.active   = e.info.default_active;
                outputs_.push_back(e);
            }
        }

        return !outputs_.empty();
    }

    bool activate_bus(bool is_input, int index, bool active) {
        if (!component) return false;
        auto dir = is_input ? kInput : kOutput;
        tresult res = component->activateBus(kAudio, dir,
                                              static_cast<int32>(index),
                                              active ? TBool(1) : TBool(0));
        if (res == kResultOk) {
            auto& vec = is_input ? inputs_ : outputs_;
            if (index >= 0 && index < static_cast<int>(vec.size()))
                vec[index].active = active;
        }
        return res == kResultOk;
    }
};


// ── Public methods ────────────────────────────────────────────────────────────

Vst3BusManager::Vst3BusManager()
    : impl_(std::make_unique<Impl>())
{}

Vst3BusManager::~Vst3BusManager() = default;

bool Vst3BusManager::load_topology(uintptr_t component_ptr) {
    auto* comp = reinterpret_cast<IComponent*>(component_ptr);
    return impl_->query(comp);
}

int Vst3BusManager::input_bus_count()  const noexcept {
    return static_cast<int>(impl_->inputs_.size());
}

int Vst3BusManager::output_bus_count() const noexcept {
    return static_cast<int>(impl_->outputs_.size());
}

std::vector<CppBusInfo> Vst3BusManager::get_input_buses() const {
    std::vector<CppBusInfo> out;
    out.reserve(impl_->inputs_.size());
    for (const auto& e : impl_->inputs_) out.push_back(e.info);
    return out;
}

std::vector<CppBusInfo> Vst3BusManager::get_output_buses() const {
    std::vector<CppBusInfo> out;
    out.reserve(impl_->outputs_.size());
    for (const auto& e : impl_->outputs_) out.push_back(e.info);
    return out;
}

int Vst3BusManager::total_output_channels() const noexcept {
    int total = 0;
    for (const auto& e : impl_->outputs_)
        if (e.active) total += e.info.channel_count;
    return total;
}

bool Vst3BusManager::set_bus_active(bool is_input, int index, bool active) {
    return impl_->activate_bus(is_input, index, active);
}

void Vst3BusManager::activate() {
    for (const auto& e : impl_->inputs_)
        impl_->activate_bus(true,  e.index, e.info.default_active);
    for (const auto& e : impl_->outputs_)
        impl_->activate_bus(false, e.index, e.info.default_active);
}

void Vst3BusManager::deactivate() {
    for (const auto& e : impl_->inputs_)
        impl_->activate_bus(true,  e.index, false);
    for (const auto& e : impl_->outputs_)
        impl_->activate_bus(false, e.index, false);
}


#else // HAVE_VST3_SDK not defined — compile no-op stubs


struct Vst3BusManager::Impl {};

Vst3BusManager::Vst3BusManager()
    : impl_(std::make_unique<Impl>())
{}

Vst3BusManager::~Vst3BusManager() = default;

bool Vst3BusManager::load_topology(uintptr_t) { return false; }
int  Vst3BusManager::input_bus_count()  const noexcept { return 0; }
int  Vst3BusManager::output_bus_count() const noexcept { return 0; }
std::vector<CppBusInfo> Vst3BusManager::get_input_buses()  const { return {}; }
std::vector<CppBusInfo> Vst3BusManager::get_output_buses() const { return {}; }
int  Vst3BusManager::total_output_channels() const noexcept { return 2; }
bool Vst3BusManager::set_bus_active(bool, int, bool) { return false; }
void Vst3BusManager::activate()   {}
void Vst3BusManager::deactivate() {}


#endif // HAVE_VST3_SDK
