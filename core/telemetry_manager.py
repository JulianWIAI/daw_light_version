"""
core/telemetry_manager.py

TelemetryManager — owns the C++ TelemetryAnalyzer, five QDockWidget panels,
and the 30-FPS QTimer polling loop.

Usage (from the main window):
    self._telemetry = TelemetryManager(self)
    self._telemetry.setup_docks()      # adds docks to the main window

Wire audio after opening a player:
    player._telemetry_push = self._telemetry.push_audio

Toggle visibility from the toolbar button:
    btn.clicked.connect(self._telemetry.toggle)
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from PySide6.QtCore import Qt, QTimer, QObject
from PySide6.QtWidgets import QDockWidget, QMainWindow

from .telemetry_waveform_widget   import TelemetryWaveformWidget
from .telemetry_band_widget       import TelemetryBandWidget
from .telemetry_chroma_widget     import TelemetryChromaWidget
from .telemetry_waterfall_widget  import TelemetryWaterfallWidget
from .telemetry_hpss_widget       import TelemetryHpssWidget

logger = logging.getLogger(__name__)

# Try to import the C++ TelemetryAnalyzer.
try:
    import daw_processors as _dp
    _HAS_ANALYZER = hasattr(_dp, "TelemetryAnalyzer")
except ImportError:
    _HAS_ANALYZER = False


class TelemetryManager(QObject):
    """
    Manages the lifecycle of the TelemetryAnalyzer (C++) and all five
    telemetry QDockWidgets.

    - All docks start hidden; call toggle() to show/hide as a group.
    - Each dock is individually movable, floatable, and closable by the user
      (standard QDockWidget behaviour).
    - The 30-FPS QTimer only runs while at least one dock is visible.
    """

    _SAMPLE_RATE = 44100

    def __init__(self, main_window: QMainWindow) -> None:
        super().__init__(main_window)
        self._window   = main_window
        self._analyzer = None
        self._visible  = False

        # Panel widgets (created once, reused across show/hide cycles).
        self._waveform  = TelemetryWaveformWidget()
        self._bands     = TelemetryBandWidget()
        self._chroma    = TelemetryChromaWidget()
        self._waterfall = TelemetryWaterfallWidget()
        self._hpss      = TelemetryHpssWidget()

        # QDockWidget containers — created in setup_docks().
        self._docks: list = []

        # 30-FPS polling timer.
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._poll)

        # Try to create the C++ analyzer.
        if _HAS_ANALYZER:
            try:
                self._analyzer = _dp.TelemetryAnalyzer(self._SAMPLE_RATE)
                self._analyzer.start()
                logger.info("TelemetryManager: C++ TelemetryAnalyzer started.")
            except Exception as exc:
                logger.warning("TelemetryManager: could not start analyzer: %s", exc)
                self._analyzer = None
        else:
            logger.info(
                "TelemetryManager: daw_processors.TelemetryAnalyzer not available "
                "(recompile C++ extension to enable telemetry audio analysis)."
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def setup_docks(self) -> None:
        """Add all five telemetry QDockWidgets to the main window.
        Called once from the main window's setup phase.
        """
        specs = [
            ("📈 Waveform",   self._waveform,  Qt.BottomDockWidgetArea),
            ("🎚 Freq Bands", self._bands,      Qt.BottomDockWidgetArea),
            ("🎵 Chroma",     self._chroma,     Qt.BottomDockWidgetArea),
            ("🌊 Waterfall",  self._waterfall,  Qt.RightDockWidgetArea),
            ("⚡ H/P Split",  self._hpss,       Qt.RightDockWidgetArea),
        ]
        prev_bottom_dock = None
        for title, widget, area in specs:
            dock = QDockWidget(title, self._window)
            dock.setWidget(widget)
            dock.setAllowedAreas(Qt.AllDockWidgetAreas)
            dock.setFeatures(
                QDockWidget.DockWidgetMovable   |
                QDockWidget.DockWidgetFloatable |
                QDockWidget.DockWidgetClosable
            )
            self._window.addDockWidget(area, dock)
            dock.hide()

            # Tab-ify consecutive bottom docks to avoid the window growing huge.
            if area == Qt.BottomDockWidgetArea and prev_bottom_dock is not None:
                self._window.tabifyDockWidget(prev_bottom_dock, dock)
            if area == Qt.BottomDockWidgetArea:
                prev_bottom_dock = dock

            self._docks.append(dock)

        logger.debug("TelemetryManager: %d dock widgets created.", len(self._docks))

    def toggle(self) -> None:
        """Show all hidden docks, or hide all visible ones."""
        if self._visible:
            for dock in self._docks:
                dock.hide()
            self._timer.stop()
            self._visible = False
        else:
            for dock in self._docks:
                dock.show()
                dock.raise_()
            if self._analyzer is not None:
                self._timer.start()
            self._visible = True

    def push_audio(self, mono: np.ndarray) -> None:
        """Push a mono float32 chunk to the C++ analyzer (audio-thread safe)."""
        if self._analyzer is not None:
            try:
                self._analyzer.push(mono)
            except Exception:
                pass

    def shutdown(self) -> None:
        """Stop the background DSP thread.  Call from QMainWindow.closeEvent."""
        self._timer.stop()
        if self._analyzer is not None:
            try:
                self._analyzer.stop()
            except Exception:
                pass

    # ── Internal 30-FPS polling loop ──────────────────────────────────────────

    def _poll(self) -> None:
        """Query the C++ analyzer once and push the frame to every panel."""
        if self._analyzer is None:
            return
        try:
            frame = self._analyzer.get_frame()
        except Exception as exc:
            logger.debug("TelemetryManager._poll: %s", exc)
            return

        self._waveform .update_frame(frame)
        self._bands    .update_frame(frame)
        self._chroma   .update_frame(frame)
        self._waterfall.update_frame(frame)
        self._hpss     .update_frame(frame)
