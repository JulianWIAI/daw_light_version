/**
 * AutomationProcessor.h -- Piecewise-linear automation curve interpolator.
 * =========================================================================
 * Header-only C++ class.  Stores a sorted list of (time_secs, value)
 * control points and provides linear interpolation between them.
 *
 * Used by FullProjectRenderer to apply per-frame volume or pan automation
 * during the offline mastering render without any Python round-trips.
 *
 * Thread safety: read-only after all add_point() calls complete.
 * Not safe to call add_point() while fill_buffer() is running.
 */

#pragma once

#include <algorithm>
#include <utility>
#include <vector>

class AutomationProcessor {
public:
    AutomationProcessor() = default;

    // ── Population ────────────────────────────────────────────────────────────

    /** Add one control point.  Out-of-order inserts are accepted; the vector
     *  is re-sorted after every insertion so value_at() is always correct. */
    void add_point(double time_secs, double value) {
        points_.push_back({time_secs, value});
        std::sort(points_.begin(), points_.end());
    }

    /** Remove all control points. */
    void clear_points() {
        points_.clear();
    }

    /** Return true if at least one control point has been added. */
    bool has_points() const {
        return !points_.empty();
    }

    // ── Query ─────────────────────────────────────────────────────────────────

    /**
     * Return the interpolated parameter value at time_secs.
     *
     * Behaviour at boundaries:
     *   - Before the first point : returns the first point's value (hold).
     *   - After  the last  point : returns the last  point's value (hold).
     *   - Between two points     : linear interpolation.
     *   - Empty list             : returns 0.0.
     */
    double value_at(double time_secs) const {
        if (points_.empty())                   return 0.0;
        if (points_.size() == 1)               return points_[0].second;
        if (time_secs <= points_.front().first) return points_.front().second;
        if (time_secs >= points_.back().first)  return points_.back().second;

        // Binary search: find the first point at or after time_secs.
        // std::lower_bound on a vector<pair> compares the .first member.
        auto it = std::lower_bound(
            points_.begin(), points_.end(),
            std::make_pair(time_secs, -1e30));

        // Bracketing segment: [prev, it].
        auto prev = std::prev(it);
        const double t0 = prev->first,  v0 = prev->second;
        const double t1 = it->first,    v1 = it->second;
        const double alpha = (time_secs - t0) / (t1 - t0);
        return v0 + alpha * (v1 - v0);
    }

    /**
     * Fill out[0..n_frames) with one automation value per sample.
     *
     * out[i] = value_at(start_secs + i / sample_rate)
     *
     * The caller must pre-allocate out to at least n_frames floats.
     * This is the hot path during offline rendering — the binary search
     * of value_at() is O(log N) per frame; for dense automation O(N·log P).
     * Typical point counts (P) are < 1000, so the cost is negligible.
     */
    void fill_buffer(float*  out,
                     int     n_frames,
                     double  start_secs,
                     double  sample_rate) const {
        const double inv_sr = 1.0 / sample_rate;
        for (int i = 0; i < n_frames; ++i) {
            out[i] = static_cast<float>(value_at(start_secs + i * inv_sr));
        }
    }

private:
    // Sorted ascending by time_secs (first element of the pair).
    std::vector<std::pair<double, double>> points_;
};
