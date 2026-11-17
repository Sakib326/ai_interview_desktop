"""Main entry point for the PySide6 desktop client skeleton.

Integrates PySide6 with asyncio via qasync, initializes OverlayWindow, and
demonstrates 50 ms audio frame capture via mic_frames().
"""

import argparse
import asyncio
import logging
import sys

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from desktop_client.audio import mic_frames
from desktop_client.overlay import OverlayWindow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("desktop_client.main")


async def stream_audio_to_overlay(
    overlay: OverlayWindow,
    stop_event: asyncio.Event,
    simulate_audio: bool = False,
):
    """Read mic frames until stopped or the window closes."""
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

            status_msg = (
                f"captured {frame_len} bytes "
                f"(frame #{frame_count}, total: {total_bytes // 1024} KB)"
            )
            overlay.set_partial(status_msg)
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


async def async_main(app: QApplication, simulate_audio: bool = False):
    """Run the overlay and audio task until the overlay closes."""
    overlay = OverlayWindow()
    overlay.show()

    loop = asyncio.get_running_loop()
    close_future = loop.create_future()
    stop_event = asyncio.Event()

    def request_close():
        if not close_future.done():
            close_future.set_result(None)

    overlay.closed.connect(request_close)
    overlay.stop_requested.connect(stop_event.set)

    audio_task = asyncio.create_task(
        stream_audio_to_overlay(overlay, stop_event, simulate_audio=simulate_audio)
    )

    try:
        await close_future
    finally:
        stop_event.set()
        audio_task.cancel()
        try:
            await audio_task
        except asyncio.CancelledError:
            pass


def main(argv=None):
    """Application entry point with qasync event loop."""
    parser = argparse.ArgumentParser(description="PySide6 live interview overlay")
    parser.add_argument(
        "--simulate-audio",
        action="store_true",
        help="Emit silent frames when no microphone is available (headless verification only)",
    )
    args = parser.parse_args(argv)

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    # qasync's explicit loop integration is supported on all Python versions we
    # target. Avoid catching broad exceptions around asyncio.run(loop_factory=...).
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            async_main(app, simulate_audio=args.simulate_audio)
        )
    finally:
        loop.close()


if __name__ == "__main__":
    main()
