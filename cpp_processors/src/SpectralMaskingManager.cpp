/*
 * SpectralMaskingManager.cpp  --  Two-Track Spectral Masking Resolution Manager
 * ===============================================================================
 * See SpectralMaskingManager.h for the algorithm description.
 */

#include "SpectralMaskingManager.h"
#include <cmath>
#include <algorithm>

// ---------------------------------------------------------------------------
// Singleton accessor  (Meyers singleton, guaranteed thread-safe in C++11)
// ---------------------------------------------------------------------------

SpectralMaskingManager& SpectralMaskingManager::instance() noexcept
{
    static SpectralMaskingManager s_instance;
    return s_instance;
}

// ---------------------------------------------------------------------------
// update()
// ---------------------------------------------------------------------------

void SpectralMaskingManager::update(int   group_id,
                                     int   slot,
                                     float centroid_hz,
                                     float tolerance_hz,
                                     float max_pan,
                                     float dt,
                                     float smooth_ms) noexcept
{
    std::lock_guard<std::mutex> lock(mutex_);

    GroupState& gs = groups_[group_id];   // creates a default GroupState if new

    // Update this slot's centroid.
    gs.centroid[slot & 1] = centroid_hz;

    // Compute the LP filter coefficient from dt and smooth_ms.
    // coeff → 1 means very slow (long smooth_ms), coeff → 0 means instant.
    float coeff = 0.0f;
    if (smooth_ms > 0.0f && dt > 0.0f) {
        coeff = expf(-dt / (smooth_ms * 0.001f));
    }

    // Recompute target pans and advance this slot's smoothed pan.
    _step_pan(gs, slot & 1, tolerance_hz, max_pan, coeff);
}

// ---------------------------------------------------------------------------
// get_pan()
// ---------------------------------------------------------------------------

float SpectralMaskingManager::get_pan(int group_id, int slot) noexcept
{
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = groups_.find(group_id);
    if (it == groups_.end()) return 0.0f;
    return it->second.smooth_pan[slot & 1];
}

// ---------------------------------------------------------------------------
// remove_group()
// ---------------------------------------------------------------------------

void SpectralMaskingManager::remove_group(int group_id) noexcept
{
    std::lock_guard<std::mutex> lock(mutex_);
    groups_.erase(group_id);
}

// ---------------------------------------------------------------------------
// _step_pan()  --  compute targets and advance one slot's smoothed pan
// ---------------------------------------------------------------------------

void SpectralMaskingManager::_step_pan(GroupState& gs, int slot,
                                        float tolerance_hz, float max_pan,
                                        float coeff) noexcept
{
    // Measure the spectral distance between the two tracks.
    float delta    = gs.centroid[0] - gs.centroid[1];
    float absdelta = fabsf(delta);

    // Target pan for each slot: default to centre (no masking).
    float target_pan_0 = 0.0f;
    float target_pan_1 = 0.0f;

    if (absdelta < tolerance_hz) {
        // Masking detected.  Compute masking severity in [0, 1].
        float strength = (tolerance_hz - absdelta) / tolerance_hz;

        // Decide direction: track with lower centroid goes left.
        // If centroid[0] <= centroid[1], track 0 has more low-frequency energy
        // → push track 0 left, track 1 right (consistent with convention).
        float dir = (delta <= 0.0f) ? 1.0f : -1.0f;

        target_pan_0 = -dir * max_pan * strength;
        target_pan_1 = +dir * max_pan * strength;
    }

    // Smooth only the requested slot toward its target.
    float target = (slot == 0) ? target_pan_0 : target_pan_1;
    gs.smooth_pan[slot] = coeff * gs.smooth_pan[slot]
                        + (1.0f - coeff) * target;
}
