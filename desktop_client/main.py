"""Application entry point.

Modes:
- Backend configured (``DESKTOP_ACCESS_TOKEN`` set): full live session —
  preflight, session, ``/events``, ``/audio``, streaming answers, debrief.
- No token: capture-only skeleton that still proves the overlay and mic framing
  without touching the network.

Both modes run on qasync so Qt and asyncio share one event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from desktop_client.audio import mic_frames
from desktop_client.config import Settings
from desktop_client.hotkeys import VK_F8, VK_F9, GlobalHotkeys, TrayIcon
from desktop_client.overlay import OverlayWindow
from desktop_client.session import LiveSessionController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("desktop_client.main")

VK_F10 = 0x79


async def stream_audio_to_overlay(
    overlay: OverlayWindow,
    stop_event: asyncio.Event,
    simulate_audio: bool = False,
):
    """Capture-only loop used when no backend is configured."""
    frame_count = 0
    total_bytes = 0

    try:
        async for frame in mic_frames(
            allow_simulation_fallback=simulate_audio,
            stop_event=stop_event,
        ):
            frame_len = len(frame)
            frame_count += 1
            total_bytes += frame_len

            if frame_count == 1:
                print(f"captured {frame_len} bytes", flush=True)

            overlay.set_partial(
                f"captured {frame_len} bytes "
                f"(frame #{frame_count}, total: {total_bytes // 1024} KB)"
            )
    except asyncio.CancelledError:
        logger.info("Audio stream task cancelled")
        raise
    except Exception as exc:
        logger.error("Error in audio stream: %s", exc)
        overlay.set_status("audio error")
        overlay.set_partial(f"Audio capture error: {exc}")
    else:
        overlay.set_status("stopped")
        overlay.set_partial("Capture stopped.")


async def async_main(app: QApplication, settings: Settings, *, use_tray: bool = True,
                     use_hotkeys: bool = True):
    overlay = OverlayWindow()
    overlay.show()

    loop = asyncio.get_running_loop()
    close_future: asyncio.Future = loop.create_future()

    def request_close():
        if not close_future.done():
            close_future.set_result(None)

    overlay.closed.connect(request_close)

    controller = None
    audio_task = None
    local_stop = asyncio.Event()

    if settings.configured:
        controller = LiveSessionController(overlay, settings)
        overlay.stop_requested.connect(
            lambda: asyncio.create_task(controller.stop())
        )
        try:
            await controller.start()
        except Exception as exc:
            logger.error("Could not start live session: %s", exc)
            overlay.set_status("offline")
            overlay.set_partial(str(exc))
    else:
        overlay.set_status("no backend")
        overlay.set_partial(
            "DESKTOP_ACCESS_TOKEN is not set \u2014 capture-only mode"
        )
        overlay.stop_requested.connect(local_stop.set)
        audio_task = asyncio.create_task(
            stream_audio_to_overlay(overlay, local_stop, settings.simulate_audio)
        )

    hotkeys = None
    if use_hotkeys:
        hotkeys = GlobalHotkeys(
            overlay,
            {
                VK_F8: overlay._on_toggle_click_through_btn,
                VK_F9: overlay.toggle_stealth,
                VK_F10: (
                    controller.answer_latest
                    if controller is not None
                    else (lambda: None)
                ),
            },
        )
        hotkeys.register()

    tray = None
    if use_tray:
        tray = TrayIcon(overlay, on_quit=overlay.close)
        tray.show()

    try:
        await close_future
    finally:
        if hotkeys is not None:
            hotkeys.unregister()
        if tray is not None:
            tray.close()
        if controller is not None:
            await controller.shutdown()
        if audio_task is not None:
            local_stop.set()
            audio_task.cancel()
            try:
                await audio_task
            except asyncio.CancelledError:
                pass


def build_settings(args) -> Settings:
    settings = Settings.load()
    if args.api_url:
        settings.api_base_url = args.api_url.rstrip("/")
        settings.ws_base_url = ""
    if args.session:
        settings.session_id = args.session
    if args.simulate_audio:
        settings.simulate_audio = True
    if args.auto_answer:
        settings.auto_answer = True
    if args.no_answer:
        settings.auto_answer = False
    # Recompute the ws origin when the API URL was overridden.
    if args.api_url or not settings.ws_base_url:
        settings.__post_init__()
    return settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="PySide6 live interview overlay")
    parser.add_argument(
        "--simulate-audio",
        action="store_true",
        help="Emit silent frames when no microphone is available (headless verification only)",
    )
    parser.add_argument("--session", help="Reuse an existing session id")
    parser.add_argument("--api-url", help="Backend API base URL override")
    parser.add_argument(
        "--auto-answer",
        action="store_true",
        help="Stream an answer after every transcript final",
    )
    parser.add_argument(
        "--no-answer",
        action="store_true",
        help="Never auto-answer (F10 still answers manually)",
    )
    parser.add_argument("--no-tray", action="store_true", help="Disable the tray icon")
    parser.add_argument(
        "--no-hotkeys", action="store_true", help="Disable Windows global hotkeys"
    )
    args = parser.parse_args(argv)

    settings = build_settings(args)

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            async_main(
                app,
                settings,
                use_tray=not args.no_tray,
                use_hotkeys=not args.no_hotkeys,
            )
        )
    finally:
        loop.close()


if __name__ == "__main__":
    main()
