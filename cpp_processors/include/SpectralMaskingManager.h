/*
 * SpectralMaskingManager.h  --  Two-Track Spectral Masking Resolution Manager
 * ============================================================================
 * Process-level singleton (Meyers singleton, thread-safe in C++11) that
 * receives spectral centroid values from two sibling SpectralPanningProcessor
 * instances (slot 0 = A, slot 1 = B) belonging to the same numbered group and
 * computes equal-and-opposite stereo panning vectors to separate them in the
 * frequency-based stereo field.
 *
 * Masking detection algorithm:
 *   delta = |centroid_A − centroid_B|
 *   if delta < tolerance_hz:
 *       strength = (tolerance_hz − delta) / tolerance_hz   ∈ [0, 1]
 *       target_pan_A = −max_pan × strength    (push A left)
 *       target_pan_B = +max_pan × strength    (push B right)
 *   else:
 *       target_pan_A = target_pan_B = 0.0     (no masking, return to centre)
 *
 * Pan smoothing:
 *   Each slot's pan is low-pass filtered toward its target with a one-pole
 *   IIR whose coefficient is derived from dt and the caller's smooth_ms:
 *       coeff = exp( -dt / (smooth_ms × 0.001) )
 *   This prevents jarring spatial jumps when masking starts or ends.
 *
 * Thread safety:
 *   All public methods are protected by a std::mutex.
 *   Called from audio-thread contexts; keep the lock duration minimal.
 */

#pragma once

#include <mutex>
#include <unordered_map>
#include <cmath>

class SpectralMaskingManager {
public:
    // Access the process-level singleton.
    static SpectralMaskingManager& instance() noexcept;

    // ------------------------------------------------------------------
    // update()
    //
    // Push a new centroid for slot (0=A or 1=B) in group group_id.
    // Recomputes both target pans from the current centroids of both slots,
    // then advances THIS slot's smoothed pan by one step toward its target.
    //
    // group_id      : integer group identifier (same value on both paired plugins).
    // slot          : 0 for track A, 1 for track B.
    // centroid_hz   : latest spectral centroid from SpectralAnalyzer::get_centroid().
    // tolerance_hz  : masking detection threshold in Hz.
    // max_pan       : maximum pan deflection [0, 1].  0.5 = 50 % pan.
    // dt            : duration of the current block in seconds.
    // smooth_ms     : LP filter time constant for pan smoothing in ms.
    // ------------------------------------------------------------------
    void update(int group_id, int slot, float centroid_hz,
                float tolerance_hz, float max_pan,
                float dt, float smooth_ms) noexcept;

    // Return the current smoothed pan for a slot [-1, +1].
    // Returns 0.0 if the group does not yet exist.
    float get_pan(int group_id, int slot) noexcept;

    // Remove a group's state (e.g. when a plugin is unloaded).
    void remove_group(int group_id) noexcept;

private:
    // Private constructor — use instance() to access the singleton.
    SpectralMaskingManager() = default;
    SpectralMaskingManager(const SpectralMaskingManager&)            = delete;
    SpectralMaskingManager& operator=(const SpectralMaskingManager&) = delete;

    struct GroupState {
        float centroid[2]   = { 500.0f, 2000.0f };  // centroid per slot
        float smooth_pan[2] = { 0.0f,   0.0f   };   // smoothed pan per slot
    };

    std::unordered_map<int, GroupState> groups_;
    std::mutex                          mutex_;

    // Compute target pans from both centroids and advance ONE slot's smooth_pan.
    static void _step_pan(GroupState& gs, int slot,
                          float tolerance_hz, float max_pan,
                          float coeff) noexcept;
};
