/**
 * Vst3AutomationQueue.h -- Sample-accurate VST3 parameter automation.
 * ====================================================================
 * Implements the VST3 IParameterChanges + IParamValueQueue interfaces so the
 * DAW's timeline automation data can be delivered to plugins with per-sample
 * accuracy inside every process() call.
 *
 * How it works:
 *   1. Before each audio block the Python timeline (or C++ automation engine)
 *      calls add_point(param_id, sample_offset, normalized_value) for every
 *      automation breakpoint that falls within the upcoming block.
 *   2. The Vst3AutomationQueue object is passed to ProcessData::inputParameterChanges.
 *   3. The VST3 plugin reads getParameterCount() → getParameterData(i) →
 *      getPointCount() → getPoint(j, ...) and interpolates between breakpoints.
 *   4. Call clear() at the start of the next block before re-populating.
 *
 * Thread safety:
 *   add_point() / clear() must be called from a SINGLE thread (the audio thread
 *   or the thread that prepares the process block).  The plugin's process() reads
 *   these structures from the same thread, so no locking is needed.
 *
 * VST3 COM lifetime:
 *   These objects are stack-allocated and passed by pointer to process().
 *   Well-behaved plugins do not hold references past the process() return.
 *   addRef / release are no-ops to suppress unwanted COM self-deletion.
 */

#pragma once

#ifdef HAVE_VST3_SDK

#include "pluginterfaces/vst/ivstparameterchanges.h"  // IParameterChanges, IParamValueQueue
#include "pluginterfaces/base/funknown.h"              // TUID, tresult, uint32

#include <cstdint>
#include <memory>
#include <unordered_map>
#include <vector>

// ── Single-parameter value queue ──────────────────────────────────────────────
// Holds sample-accurate breakpoints for one parameter in one audio block.

class Vst3ParamQueue final : public Steinberg::Vst::IParamValueQueue {
public:
    explicit Vst3ParamQueue(Steinberg::Vst::ParamID id) : param_id_(id) {}

    // Add one breakpoint.  sample_offset must be < block size.
    void add_point(Steinberg::int32 sample_offset, Steinberg::Vst::ParamValue value) {
        points_.push_back({ sample_offset, value });
    }

    // Remove all breakpoints.
    void clear() noexcept { points_.clear(); }

    // ── IParamValueQueue ─────────────────────────────────────────────────────

    Steinberg::Vst::ParamID PLUGIN_API getParameterId() override {
        return param_id_;
    }

    Steinberg::int32 PLUGIN_API getPointCount() override {
        return static_cast<Steinberg::int32>(points_.size());
    }

    Steinberg::tresult PLUGIN_API getPoint(Steinberg::int32  index,
                                            Steinberg::int32& sampleOffset,
                                            Steinberg::Vst::ParamValue& value) override
    {
        if (index < 0 || index >= static_cast<Steinberg::int32>(points_.size()))
            return Steinberg::kInvalidArgument;
        sampleOffset = points_[static_cast<size_t>(index)].sample_offset;
        value        = points_[static_cast<size_t>(index)].value;
        return Steinberg::kResultOk;
    }

    Steinberg::tresult PLUGIN_API addPoint(Steinberg::int32   sampleOffset,
                                            Steinberg::Vst::ParamValue value,
                                            Steinberg::int32&  index) override
    {
        index = static_cast<Steinberg::int32>(points_.size());
        points_.push_back({ sampleOffset, value });
        return Steinberg::kResultOk;
    }

    // ── FUnknown — no COM lifetime management (stack object) ─────────────────

    Steinberg::tresult PLUGIN_API queryInterface(const Steinberg::TUID iid, void** obj) override {
        if (Steinberg::FUnknownPrivate::iidEqual(iid, IParamValueQueue::iid) ||
            Steinberg::FUnknownPrivate::iidEqual(iid, Steinberg::FUnknown::iid)) {
            *obj = static_cast<IParamValueQueue*>(this);
            return Steinberg::kResultOk;
        }
        *obj = nullptr;
        return Steinberg::kNoInterface;
    }
    Steinberg::uint32 PLUGIN_API addRef()  override { return 1; }
    Steinberg::uint32 PLUGIN_API release() override { return 1; }

private:
    struct Point {
        Steinberg::int32           sample_offset;
        Steinberg::Vst::ParamValue value;
    };

    Steinberg::Vst::ParamID param_id_;
    std::vector<Point>      points_;
};

// ── Multi-parameter automation container ─────────────────────────────────────
// Holds queues for all parameters changed in one audio block.

class Vst3AutomationQueue final : public Steinberg::Vst::IParameterChanges {
public:
    // Add an automation breakpoint for param_id at the given sample_offset
    // within the current block.  normalized_value is in [0.0, 1.0].
    // Creates a new per-parameter queue on first use; subsequent calls append.
    void add_point(Steinberg::Vst::ParamID param_id,
                   Steinberg::int32        sample_offset,
                   Steinberg::Vst::ParamValue normalized_value);

    // Remove all breakpoints from all queues.  Call at the start of each block.
    void clear() noexcept;

    // ── IParameterChanges ─────────────────────────────────────────────────────

    // Number of parameters that have at least one breakpoint this block.
    Steinberg::int32 PLUGIN_API getParameterCount() override;

    // Return the queue for parameter at index.  Index is sequential order of
    // first-seen parameters (not parameter ID order).
    Steinberg::Vst::IParamValueQueue* PLUGIN_API getParameterData(
        Steinberg::int32 index) override;

    // Called by the plugin to add a new parameter queue; we find-or-create.
    Steinberg::Vst::IParamValueQueue* PLUGIN_API addParameterData(
        const Steinberg::Vst::ParamID& id,
        Steinberg::int32&              index) override;

    // ── FUnknown — no COM lifetime management (stack object) ─────────────────

    Steinberg::tresult PLUGIN_API queryInterface(const Steinberg::TUID iid, void** obj) override {
        if (Steinberg::FUnknownPrivate::iidEqual(iid, IParameterChanges::iid) ||
            Steinberg::FUnknownPrivate::iidEqual(iid, Steinberg::FUnknown::iid)) {
            *obj = static_cast<IParameterChanges*>(this);
            return Steinberg::kResultOk;
        }
        *obj = nullptr;
        return Steinberg::kNoInterface;
    }
    Steinberg::uint32 PLUGIN_API addRef()  override { return 1; }
    Steinberg::uint32 PLUGIN_API release() override { return 1; }

private:
    // Insertion-ordered list of queues (getParameterData uses sequential index).
    std::vector<std::unique_ptr<Vst3ParamQueue>> queues_;

    // Fast lookup by parameter ID.
    std::unordered_map<Steinberg::Vst::ParamID, Vst3ParamQueue*> by_id_;
};

#endif // HAVE_VST3_SDK
