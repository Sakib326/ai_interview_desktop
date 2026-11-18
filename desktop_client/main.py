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

from PySide6.QtWidgets import QApplication, QMessageBox
from qasync import QEventLoop

from desktop_client.audio import mic_frames
from desktop_client.config import Settings, audio_source_channels
from desktop_client.hotkeys import VK_F8, VK_F9, GlobalHotkeys, TrayIcon
from desktop_client.overlay import OverlayWindow
from desktop_client.session import LiveSessionController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("desktop_client.main")

VK_F10 = 0x79


def _confirm_forget_memory(controller) -> None:
    """Ask before the one destructive memory action."""
    choice = QMessageBox.question(
        None,
        "Erase memory",
        "Delete all stored memory for this account?\n"
        "Sessions and debriefs are kept.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if choice == QMessageBox.StandardButton.Yes:
        asyncio.create_task(controller.forget_all_memory())


async def stream_audio_to_overlay(
    overlay: OverlayWindow,
    stop_event: asyncio.Event,
    simulate_audio: bool = False,
    source: str = "mic",
):
    """Capture-only loop used when no backend is configured."""
    sources = audio_source_channels(source)

    if len(sources) == 1:
        frame_count = 0
        total_bytes = 0
        try:
            async for frame in mic_frames(
                source=sources[0],
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
        return

    counts = {s: {"frames": 0, "bytes": 0} for s in sources}
    printed = set()

    async def run_one(src: str):
        async for frame in mic_frames(
            source=src,
            allow_simulation_fallback=simulate_audio,
            stop_event=stop_event,
        ):
            counts[src]["frames"] += 1
            counts[src]["bytes"] += len(frame)
            if src not in printed:
                print(f"captured {len(frame)} bytes [{src}]", flush=True)
                printed.add(src)
            parts = [
                f"{name} #{v['frames']} ({v['bytes'] // 1024} KB)"
                for name, v in counts.items()
            ]
            overlay.set_partial("captured " + " | ".join(parts))

    tasks = [asyncio.create_task(run_one(s), name=f"capture-{s}") for s in sources]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                raise r
        if any(isinstance(r, asyncio.CancelledError) for r in results):
            raise asyncio.CancelledError
        overlay.set_status("stopped")
        overlay.set_partial("Capture stopped.")
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Audio stream task cancelled")
        raise
    except Exception as exc:
        stop_event.set()
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.error("Error in audio stream: %s", exc)
        overlay.set_status("audio error")
        overlay.set_partial(f"Audio capture error: {exc}")


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
            stream_audio_to_overlay(
                overlay, local_stop, settings.simulate_audio, settings.audio_source
            )
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
        extra_actions = []
        if controller is not None:
            extra_actions = [
                ("Forget all memory\u2026", lambda: _confirm_forget_memory(controller)),
                (
                    "Retry memory extraction",
                    lambda: asyncio.create_task(controller.retry_memory()),
                ),
            ]
        tray = TrayIcon(overlay, on_quit=overlay.close, extra_actions=extra_actions)
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
    import signal

    from PySide6.QtCore import QTimer

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

    # Qt can block Python's signal handling on Windows; a timer keeps the
    # interpreter responsive so Ctrl+C is delivered promptly.
    sig_timer = QTimer()
    sig_timer.start(250)
    sig_timer.timeout.connect(lambda: None)

    task = loop.create_task(
        async_main(
            app,
            settings,
            use_tray=not args.no_tray,
            use_hotkeys=not args.no_hotkeys,
        )
    )

    def _sigint(*_):
        logger.info("SIGINT received; closing")
        try:
            app.closeAllWindows()
        except Exception:
            pass

    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _sigint)

    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
        try:
            app.closeAllWindows()
        except Exception:
            pass
        try:
            loop.run_until_complete(asyncio.wait_for(asyncio.shield(task), timeout=15))
        except Exception:
            if not task.done():
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        sig_timer.stop()
        # Best-effort cleanup of any tasks left behind after interruption.
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except Exception:
            pass
        loop.close()


if __name__ == "__main__":
    main()
