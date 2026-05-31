"""
audio_fx_panel.py -- Backward-compatible alias for FxRackWidget.
================================================================
gui_windows.py imports AudioFxPanel from this module.  Re-exporting
FxRackWidget as AudioFxPanel means gui_windows.py needs zero changes
while the underlying implementation is now the dynamic insert-slot rack.
"""

from .fx_rack_widget import FxRackWidget

# Drop-in replacement: same public interface (load_chain, chain_changed signal).
AudioFxPanel = FxRackWidget

__all__ = ["AudioFxPanel"]
