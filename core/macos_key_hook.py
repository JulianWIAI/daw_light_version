"""
macos_key_hook.py — macOS NSEvent local keyboard monitor (PyObjC)
=================================================================
Installs an NSEvent local monitor so that keyboard events fire our
callbacks *before* they are dispatched to the currently focused window
(including native plugin editor windows created by pedalboard).

This lets the user hold a key to play a note while the VST editor has
Cocoa keyboard focus.

Requires:  pip install pyobjc-framework-Cocoa

Previous implementation used hand-crafted ctypes ObjC blocks, which caused
immediate SIGSEGV on ARM64 macOS 15 (Sequoia) because PAC (Pointer
Authentication Codes) rejects un-signed raw function pointers stored in
block invoke fields.  PyObjC's bridge signs them correctly.

Usage:
    handle = macos_key_hook.install(key_down_cb, key_up_cb)
    # ... open native editor (blocks) ...
    macos_key_hook.remove(handle)

Callbacks receive a single argument: the Qt.Key integer for the
pressed / released key (same value as QKeyEvent.key()).
"""

from __future__ import annotations

import logging
import platform
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Optional PyObjC import ────────────────────────────────────────────────

try:
    import AppKit as _AppKit
    _PYOBJC_OK = True
except ImportError:
    _PYOBJC_OK = False
    logger.debug(
        "pyobjc-framework-Cocoa not installed — "
        "QWERTY keys will be silent while the native VST editor is open. "
        "Install with:  pip install pyobjc-framework-Cocoa"
    )

# ─── NSEvent mask / modifier constants ────────────────────────────────────

_NSEventMaskKeyDown = 1 << 10   # 1024
_NSEventMaskKeyUp   = 1 << 11   # 2048

_NSEventModifierFlagControl = 1 << 18
_NSEventModifierFlagOption  = 1 << 19
_NSEventModifierFlagCommand = 1 << 20
_MODIFIER_MASK = (
    _NSEventModifierFlagControl |
    _NSEventModifierFlagOption  |
    _NSEventModifierFlagCommand
)

# Opaque handle type returned by install()
_Handle = Tuple  # (mon_kd, mon_ku)


# ─── Character → Qt key translation ───────────────────────────────────────

def _chars_to_qt_key(chars: str) -> int:
    """Translate NSEvent.characters string to a Qt.Key integer."""
    if not chars:
        return 0
    c = chars[0]
    if c.isalpha():
        return ord(c.upper())      # Qt.Key_A = 65 = ord('A')
    if 32 < ord(c) < 128:
        return ord(c)              # Qt.Key_Comma = 44 = ord(',')
    return 0


# ─── Public API ───────────────────────────────────────────────────────────

def install(
    key_down_cb: Callable[[int], None],
    key_up_cb:   Callable[[int], None],
) -> Optional[_Handle]:
    """
    Install a macOS NSEvent local keyboard monitor via PyObjC.

    Returns an opaque handle to pass to remove(), or None if PyObjC is
    unavailable or setup fails.
    """
    if platform.system() != "Darwin" or not _PYOBJC_OK:
        return None
    try:
        return _install_impl(key_down_cb, key_up_cb)
    except Exception as exc:
        logger.debug("macOS key monitor install failed: %s", exc)
        return None


def remove(handle: Optional[_Handle]) -> None:
    """Remove a monitor previously installed with install()."""
    if handle is None or not _PYOBJC_OK:
        return
    try:
        _remove_impl(handle)
    except Exception as exc:
        logger.debug("macOS key monitor remove failed: %s", exc)


# ─── Implementation ───────────────────────────────────────────────────────

def _make_handler(cb: Callable[[int], None]):
    """Return an NSEvent handler block that calls cb(qt_key) on each event."""
    def handler(event):
        try:
            if event.modifierFlags() & _MODIFIER_MASK:
                return event
            chars = event.characters()
            if chars:
                qt_key = _chars_to_qt_key(str(chars))
                if qt_key:
                    cb(qt_key)
        except Exception:
            pass
        return event
    return handler


def _install_impl(
    key_down_cb: Callable[[int], None],
    key_up_cb:   Callable[[int], None],
) -> _Handle:
    mon_kd = _AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        _NSEventMaskKeyDown, _make_handler(key_down_cb)
    )
    mon_ku = _AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        _NSEventMaskKeyUp, _make_handler(key_up_cb)
    )
    return (mon_kd, mon_ku)


def _remove_impl(handle: _Handle) -> None:
    mon_kd, mon_ku = handle
    if mon_kd is not None:
        _AppKit.NSEvent.removeMonitor_(mon_kd)
    if mon_ku is not None:
        _AppKit.NSEvent.removeMonitor_(mon_ku)