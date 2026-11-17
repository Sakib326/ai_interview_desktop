"""Stealth capture exclusion module (Windows SetWindowDisplayAffinity).

Platform scope: Windows only. macOS and Linux return UNSUPPORTED.
"""

from enum import Enum
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


class StealthState(Enum):
    EXCLUDED = "excluded"        # OS confirmed exclusion
    BLACK_BOX = "black_box"      # Excluded as opaque rect (WDA_MONITOR)
    UNSUPPORTED = "unsupported"  # Not Windows
    FAILED = "failed"            # API present, call rejected


# Windows Display Affinity Constants
WDA_NONE = 0x00
WDA_MONITOR = 0x01
WDA_EXCLUDEFROMCAPTURE = 0x11


def capability() -> StealthState:
    """Probe OS stealth capability without side effects."""
    if sys.platform != "win32":
        return StealthState.UNSUPPORTED

    try:
        build = sys.getwindowsversion().build
        if build >= 19041:
            return StealthState.EXCLUDED
        return StealthState.BLACK_BOX
    except Exception as exc:
        logger.warning("Failed to determine Windows build version: %s", exc)
        return StealthState.FAILED


def apply(widget: Any) -> StealthState:
    """Apply capture exclusion to the top-level widget.

    Rules:
    - Must be called on the GUI thread after widget.show().
    - Never caches the window handle (handle re-created across window flag changes).
    - Windows-only; returns UNSUPPORTED immediately on other platforms.
    """
    if sys.platform != "win32":
        logger.info("Capture exclusion is unsupported on non-Windows platform: %s", sys.platform)
        return StealthState.UNSUPPORTED

    if widget is None:
        logger.error("apply() called with None widget")
        return StealthState.FAILED

    try:
        # Native window handle must be a local variable, never cached on instance
        hwnd = int(widget.winId())
    except Exception as exc:
        logger.error("Failed to acquire native window handle from widget: %s", exc)
        return StealthState.FAILED

    if not hwnd:
        logger.error("apply() called on widget without a valid native HWND")
        return StealthState.FAILED

    try:
        import ctypes
        user32 = ctypes.windll.user32
        build = sys.getwindowsversion().build

        # Primary path: Windows 10 2004+ (build 19041+) supports WDA_EXCLUDEFROMCAPTURE (0x11)
        if build >= 19041:
            res = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if res != 0:
                logger.debug("SetWindowDisplayAffinity(0x11) succeeded for HWND %d", hwnd)
                return StealthState.EXCLUDED

            err = ctypes.GetLastError()
            logger.warning(
                "SetWindowDisplayAffinity(0x11) failed (GetLastError=%d), retrying with WDA_MONITOR (0x01)",
                err,
            )

        # Fallback or older builds: WDA_MONITOR (0x01)
        res_monitor = user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR)
        if res_monitor != 0:
            logger.info("SetWindowDisplayAffinity(0x01) succeeded (BLACK_BOX)")
            return StealthState.BLACK_BOX

        err = ctypes.GetLastError()
        logger.error("SetWindowDisplayAffinity failed with error %d", err)
        return StealthState.FAILED

    except Exception as exc:
        logger.error("Unexpected error in apply(): %s", exc)
        return StealthState.FAILED


def clear(widget: Any) -> None:
    """Clear display affinity on the widget (set WDA_NONE). Idempotent."""
    if sys.platform != "win32" or widget is None:
        return

    try:
        import ctypes
        hwnd = int(widget.winId())
        if hwnd:
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_NONE)
            logger.debug("SetWindowDisplayAffinity(WDA_NONE) succeeded for HWND %d", hwnd)
    except Exception as exc:
        logger.warning("Error clearing window display affinity: %s", exc)
