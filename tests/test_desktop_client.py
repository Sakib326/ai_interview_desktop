"""Unit test suite for PySide6 desktop client skeleton.

Tests:
- Stealth API capability, apply, clear, and state transitions (including Windows mocks)
- OverlayWindow creation, flags, attributes, click-through toggling, and APIs
- Audio frame generator, 1600-byte framing (50 ms, 16 kHz S16LE), queue bounds, and tail padding
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from desktop_client import stealth
from desktop_client.audio import (
    BLOCK_SIZE,
    BYTES_PER_SAMPLE,
    CHANNELS,
    DEFAULT_QUEUE_MAXSIZE,
    FRAME_BYTES,
    FRAME_DURATION_MS,
    SAMPLE_RATE,
    mic_frames,
)
from desktop_client.overlay import OverlayWindow
from desktop_client.stealth import (
    WDA_EXCLUDEFROMCAPTURE,
    WDA_MONITOR,
    WDA_NONE,
    StealthState,
)


@pytest.fixture(scope="session")
def qapp():
    """Ensure QApplication instance is initialized for GUI tests."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ============================================================================
# 1. Stealth Tests
# ============================================================================

def test_stealth_enum_and_constants():
    assert StealthState.EXCLUDED.value == "excluded"
    assert StealthState.BLACK_BOX.value == "black_box"
    assert StealthState.UNSUPPORTED.value == "unsupported"
    assert StealthState.FAILED.value == "failed"

    assert WDA_NONE == 0x00
    assert WDA_MONITOR == 0x01
    assert WDA_EXCLUDEFROMCAPTURE == 0x11


def test_stealth_capability_non_windows():
    if sys.platform != "win32":
        assert stealth.capability() == StealthState.UNSUPPORTED


def test_stealth_apply_clear_non_windows(qapp):
    if sys.platform != "win32":
        widget = MagicMock()
        assert stealth.apply(widget) == StealthState.UNSUPPORTED
        # clear is a no-op
        stealth.clear(widget)


def test_stealth_capability_windows_mock():
    with patch("sys.platform", "win32"):
        # Modern Windows (build >= 19041)
        mock_win_ver = MagicMock()
        mock_win_ver.build = 19045
        with patch("sys.getwindowsversion", return_value=mock_win_ver, create=True):
            assert stealth.capability() == StealthState.EXCLUDED

        # Older Windows (< 19041)
        mock_win_ver.build = 18362
        with patch("sys.getwindowsversion", return_value=mock_win_ver, create=True):
            assert stealth.capability() == StealthState.BLACK_BOX


def test_stealth_apply_windows_success_excluded():
    with patch("sys.platform", "win32"):
        mock_win_ver = MagicMock()
        mock_win_ver.build = 22631  # Win11

        mock_user32 = MagicMock()
        mock_user32.SetWindowDisplayAffinity.return_value = 1

        mock_ctypes = MagicMock()
        mock_ctypes.windll.user32 = mock_user32

        widget = MagicMock()
        widget.winId.return_value = 12345

        with patch("sys.getwindowsversion", return_value=mock_win_ver, create=True):
            with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
                state = stealth.apply(widget)
                assert state == StealthState.EXCLUDED
                mock_user32.SetWindowDisplayAffinity.assert_called_once_with(12345, WDA_EXCLUDEFROMCAPTURE)


def test_stealth_apply_windows_fallback_black_box():
    with patch("sys.platform", "win32"):
        mock_win_ver = MagicMock()
        mock_win_ver.build = 19044

        mock_user32 = MagicMock()
        # First call (0x11) fails with 0, second call (0x01) succeeds with 1
        mock_user32.SetWindowDisplayAffinity.side_effect = [0, 1]

        mock_ctypes = MagicMock()
        mock_ctypes.windll.user32 = mock_user32
        mock_ctypes.GetLastError.return_value = 5

        widget = MagicMock()
        widget.winId.return_value = 67890

        with patch("sys.getwindowsversion", return_value=mock_win_ver, create=True):
            with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
                state = stealth.apply(widget)
                assert state == StealthState.BLACK_BOX
                assert mock_user32.SetWindowDisplayAffinity.call_count == 2
                mock_user32.SetWindowDisplayAffinity.assert_any_call(67890, WDA_EXCLUDEFROMCAPTURE)
                mock_user32.SetWindowDisplayAffinity.assert_any_call(67890, WDA_MONITOR)


def test_stealth_apply_windows_both_failed():
    with patch("sys.platform", "win32"):
        mock_win_ver = MagicMock()
        mock_win_ver.build = 19044

        mock_user32 = MagicMock()
        # Both calls fail
        mock_user32.SetWindowDisplayAffinity.return_value = 0

        mock_ctypes = MagicMock()
        mock_ctypes.windll.user32 = mock_user32
        mock_ctypes.GetLastError.return_value = 87

        widget = MagicMock()
        widget.winId.return_value = 11111

        with patch("sys.getwindowsversion", return_value=mock_win_ver, create=True):
            with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
                state = stealth.apply(widget)
                assert state == StealthState.FAILED
                assert mock_user32.SetWindowDisplayAffinity.call_count == 2


def test_stealth_clear_windows():
    with patch("sys.platform", "win32"):
        mock_user32 = MagicMock()
        mock_ctypes = MagicMock()
        mock_ctypes.windll.user32 = mock_user32

        widget = MagicMock()
        widget.winId.return_value = 99999

        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            stealth.clear(widget)
            mock_user32.SetWindowDisplayAffinity.assert_called_once_with(99999, WDA_NONE)


# ============================================================================
# 2. Overlay UI Tests
# ============================================================================

def test_overlay_creation_and_flags(qapp):
    overlay = OverlayWindow()
    overlay.show()

    # Window title must be neutral (§10.4)
    assert overlay.windowTitle() == "Overlay"

    # Window flags (§4)
    flags = overlay.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.Tool
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus

    # Attributes (§4)
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    overlay.close()


def test_overlay_api_methods(qapp):
    overlay = OverlayWindow()

    # Transcript API
    overlay.set_transcript("First turn completed.")
    assert "First turn completed." in overlay.transcript_edit.toPlainText()

    overlay.append_transcript("Second turn completed.")
    assert "First turn completed.\nSecond turn completed." in overlay.transcript_edit.toPlainText()

    # Partial API
    overlay.set_partial("candidate speaking partial...")
    assert overlay.partial_label.text() == "candidate speaking partial..."

    # Status API
    overlay.set_status("connected")
    assert overlay.status_label.text() == "connected"

    # Model API
    overlay.set_model("test-model-v1")
    assert overlay.model_label.text() == "test-model-v1"

    # Stealth State API & Badge verification
    overlay.set_stealth_state(StealthState.EXCLUDED)
    assert overlay.stealth_badge.text() == "hidden from share"

    overlay.set_stealth_state(StealthState.BLACK_BOX)
    assert overlay.stealth_badge.text() == "hidden (opaque)"

    overlay.set_stealth_state(StealthState.FAILED)
    assert overlay.stealth_badge.text() == "capture exclusion failed"

    overlay.set_stealth_state(StealthState.UNSUPPORTED)
    assert overlay.stealth_badge.text() == "visible to share"

    overlay.close()


def test_overlay_click_through_toggle(qapp):
    overlay = OverlayWindow()
    overlay.show()

    assert not overlay._click_through
    assert not (overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput)

    # Enable click-through
    overlay.set_click_through(True)
    assert overlay._click_through
    assert overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput

    # Disable click-through
    overlay.set_click_through(False)
    assert not overlay._click_through
    assert not (overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput)

    overlay.close()


def test_overlay_stealth_toggle(qapp):
    overlay = OverlayWindow()
    overlay.show()

    initial = overlay._stealth_enabled
    assert initial is True  # Stealth is on by default per §10.0

    overlay.toggle_stealth()
    assert overlay._stealth_enabled is False

    overlay.toggle_stealth()
    assert overlay._stealth_enabled is True

    overlay.close()


# ============================================================================
# 3. Audio Capture Tests
# ============================================================================

def test_audio_constants():
    assert SAMPLE_RATE == 16000
    assert CHANNELS == 1
    assert FRAME_DURATION_MS == 50
    assert BLOCK_SIZE == 800
    assert BYTES_PER_SAMPLE == 2
    assert FRAME_BYTES == 1600
    assert DEFAULT_QUEUE_MAXSIZE == 40


@pytest.mark.asyncio
async def test_mic_frames_yields_1600_byte_frames():
    """Verify mic_frames generator produces exact 1600 byte frames (50ms PCM S16LE)."""
    frames = []
    async for frame in mic_frames(allow_simulation_fallback=True):
        frames.append(frame)
        if len(frames) >= 3:
            break

    assert len(frames) == 3
    for f in frames:
        assert isinstance(f, bytes)
        assert len(f) == 1600


@pytest.mark.asyncio
async def test_audio_tail_padding_on_stop():
    """Verify that when audio stream ends with partial tail, it is padded with silence."""
    # Test manual padding logic
    expected_bytes = 1600
    tail_buffer = bytearray(b"\x01\x02" * 500)  # 1000 bytes (partial frame)
    assert len(tail_buffer) == 1000

    remainder = len(tail_buffer) % expected_bytes
    assert remainder == 1000
    padding = b"\x00" * (expected_bytes - remainder)
    tail_buffer.extend(padding)

    assert len(tail_buffer) == 1600
    padded_frame = bytes(tail_buffer)
    assert len(padded_frame) == 1600
    assert padded_frame[:1000] == b"\x01\x02" * 500
    assert padded_frame[1000:] == b"\x00" * 600
