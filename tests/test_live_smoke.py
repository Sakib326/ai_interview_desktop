"""Opt-in end-to-end test against a running backend.

Skipped unless a real backend and token are supplied:

    DESKTOP_LIVE_API=http://127.0.0.1:8000 \
    DESKTOP_LIVE_TOKEN=<jwt> \
    QT_QPA_PLATFORM=offscreen pytest -q -s tests/test_live_smoke.py

This drives the real controller against the real HTTP/WebSocket/SSE surface.
It does not assert on transcription or answer text, because those depend on
provider credentials the test harness cannot assume.
"""

import asyncio
import os

import pytest

from desktop_client.config import Settings
from desktop_client.overlay import OverlayWindow
from desktop_client.session import LiveSessionController

LIVE_API = os.environ.get("DESKTOP_LIVE_API", "")
LIVE_TOKEN = os.environ.get("DESKTOP_LIVE_TOKEN", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (LIVE_API and LIVE_TOKEN),
        reason="set DESKTOP_LIVE_API and DESKTOP_LIVE_TOKEN to run live tests",
    ),
]


@pytest.mark.asyncio
async def test_live_session_round_trip(qapp):
    settings = Settings(
        api_base_url=LIVE_API,
        access_token=LIVE_TOKEN,
        simulate_audio=True,
        auto_answer=False,
    )
    overlay = OverlayWindow()
    overlay.show()
    controller = LiveSessionController(overlay, settings)

    try:
        await controller.start()
        assert controller.session_id, "no session was created or adopted"

        await asyncio.sleep(3.0)
        status_during_capture = overlay.status_label.text()

        await controller.stop()
        audio = controller.audio_outcome

        print("\n--- live smoke ---")
        print("session :", controller.session_id)
        print("status  :", status_during_capture)
        print("audio   :", audio)
        print("turns   :", overlay.transcript_edit.toPlainText())
        print("answer  :", overlay.answer_edit.toPlainText())
        print("partial :", overlay.partial_label.text())

        assert status_during_capture not in {"offline", "unauthorized"}
        assert audio is not None
        assert audio.status in {"closed", "failed", "disconnected", "timeout"}
    finally:
        await controller.shutdown()
