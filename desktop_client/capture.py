"""Screen capture for session artifacts.

Uses ``QScreen.grabWindow(0)`` from PySide6, so no extra dependency is needed.
Captures the primary screen as PNG bytes for ``POST /sessions/{id}/solve``;
the backend owns OCR and artifact classification.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QGuiApplication

logger = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    """Raised when the screen cannot be captured."""


def grab_screen_png(screen_index: Optional[int] = None) -> bytes:
    """Return the current screen contents as PNG bytes."""
    screens = QGuiApplication.screens()
    if not screens:
        raise CaptureError("No screen available to capture")

    if screen_index is None:
        screen = QGuiApplication.primaryScreen() or screens[0]
    else:
        if not 0 <= screen_index < len(screens):
            raise CaptureError(f"Screen index {screen_index} is out of range")
        screen = screens[screen_index]

    pixmap = screen.grabWindow(0)
    if pixmap.isNull():
        raise CaptureError("Screen capture returned an empty image")

    payload = QByteArray()
    buffer = QBuffer(payload)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise CaptureError("Could not open image buffer")
    try:
        if not pixmap.save(buffer, "PNG"):
            raise CaptureError("Could not encode screen capture as PNG")
    finally:
        buffer.close()
    return bytes(payload)
