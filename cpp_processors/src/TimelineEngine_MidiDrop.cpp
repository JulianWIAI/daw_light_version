/*
 * TimelineEngine_MidiDrop.cpp
 * ===========================
 * Implementations of the three importMultiTrackMidi / is_import_busy /
 * check_import_ready forwarding methods added to TimelineEngine.
 *
 * Kept in a separate translation unit so the main TimelineEngine.cpp file
 * does not need to be edited — just add this file to CMakeLists.txt alongside
 * MidiDropImporter.cpp.
 */

#include "TimelineEngine.h"
#include "MidiDropImporter.h"

// ---------------------------------------------------------------------------
// Lazy initialiser — creates the importer on the first import request.
// The engine (`this`) is guaranteed to outlive the importer because the
// importer is a unique_ptr member of TimelineEngine.
// ---------------------------------------------------------------------------

static MidiDropImporter& _get_importer(
    std::unique_ptr<MidiDropImporter>& ptr, TimelineEngine& eng)
{
    if (!ptr)
        ptr = std::make_unique<MidiDropImporter>(eng);
    return *ptr;
}


// ---------------------------------------------------------------------------
// Public forwarding methods
// ---------------------------------------------------------------------------

void TimelineEngine::importMultiTrackMidi(
    const std::vector<MidiTrackPayload>& payloads,
    std::function<void(bool)>            on_done)
{
    _get_importer(_midi_importer, *this).import(payloads, std::move(on_done));
}

bool TimelineEngine::is_import_busy() const noexcept
{
    if (!_midi_importer)
        return false;
    return _midi_importer->is_busy();
}

bool TimelineEngine::check_import_ready()
{
    if (!_midi_importer)
        return false;
    return _midi_importer->check_import_ready();
}
