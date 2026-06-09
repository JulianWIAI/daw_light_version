/**
 * Vst3AutomationQueue.cpp -- IParameterChanges / IParamValueQueue implementation.
 */

#include "Vst3AutomationQueue.h"

#ifdef HAVE_VST3_SDK

// ── add_point: insert a breakpoint for a parameter ────────────────────────────

void Vst3AutomationQueue::add_point(Steinberg::Vst::ParamID    param_id,
                                     Steinberg::int32           sample_offset,
                                     Steinberg::Vst::ParamValue normalized_value)
{
    auto it = by_id_.find(param_id);
    if (it == by_id_.end()) {
        // First breakpoint for this parameter: allocate a new queue.
        auto queue = std::make_unique<Vst3ParamQueue>(param_id);
        Vst3ParamQueue* raw = queue.get();
        by_id_[param_id] = raw;
        queues_.push_back(std::move(queue));
        raw->add_point(sample_offset, normalized_value);
    } else {
        it->second->add_point(sample_offset, normalized_value);
    }
}

// ── clear: discard all breakpoints ───────────────────────────────────────────

void Vst3AutomationQueue::clear() noexcept {
    queues_.clear();
    by_id_.clear();
}

// ── IParameterChanges interface ───────────────────────────────────────────────

Steinberg::int32 Vst3AutomationQueue::getParameterCount() {
    return static_cast<Steinberg::int32>(queues_.size());
}

Steinberg::Vst::IParamValueQueue*
Vst3AutomationQueue::getParameterData(Steinberg::int32 index) {
    if (index < 0 || index >= static_cast<Steinberg::int32>(queues_.size()))
        return nullptr;
    return queues_[static_cast<size_t>(index)].get();
}

Steinberg::Vst::IParamValueQueue*
Vst3AutomationQueue::addParameterData(const Steinberg::Vst::ParamID& id,
                                       Steinberg::int32&              index)
{
    auto it = by_id_.find(id);
    if (it != by_id_.end()) {
        // Already exists: return the existing queue and fill in the index.
        for (size_t i = 0; i < queues_.size(); ++i) {
            if (queues_[i].get() == it->second) {
                index = static_cast<Steinberg::int32>(i);
                return it->second;
            }
        }
    }
    // New parameter: create and register.
    index = static_cast<Steinberg::int32>(queues_.size());
    auto queue = std::make_unique<Vst3ParamQueue>(id);
    Vst3ParamQueue* raw = queue.get();
    by_id_[id] = raw;
    queues_.push_back(std::move(queue));
    return raw;
}

#endif // HAVE_VST3_SDK
