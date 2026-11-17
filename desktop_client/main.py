"""Main entry point for PySide6 desktop client skeleton.

Integrates PySide6 with asyncio via qasync, initializes OverlayWindow,
and demonstrates 50 ms audio frame capture via mic_frames().
"""

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


async def stream_audio_to_overlay(overlay: OverlayWindow):
    """Coroutine to read mic frames and update the overlay partial status."""
    frame_count = 0
    total_bytes = 0

    try:
        async for frame in mic_frames():
            frame_len = len(frame)
            frame_count += 1
            total_bytes += frame_len

            # Verification requirement: Print 'captured 1600 bytes' at least once
            if frame_count == 1:
                print(f"captured {frame_len} bytes", flush=True)

            status_msg = f"captured {frame_len} bytes (frame #{frame_count}, total: {total_bytes // 1024} KB)"
            overlay.set_partial(status_msg)

    except asyncio.CancelledError:
        logger.info("Audio stream task cancelled")
    except Exception as exc:
        logger.error("Error in audio stream: %s", exc)
        overlay.set_partial(f"Audio capture error: {exc}")


async def async_main(app: QApplication):
    """Main async entry coroutine."""
    overlay = OverlayWindow()
    overlay.show()

    # Track window close to terminate async loop
    close_future = asyncio.get_running_loop().create_future()
    overlay.destroyed.connect(lambda: not close_future.done() and close_future.set_result(None))

    audio_task = asyncio.create_task(stream_audio_to_overlay(overlay))

    try:
        # Wait until user closes the overlay
        await close_future
    finally:
        audio_task.cancel()
        try:
            await audio_task
        except asyncio.CancelledError:
            pass


def main():
    """Application entry point with qasync event loop."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    # Use qasync loop_factory on Python 3.12+ with fallback
    try:
        asyncio.run(async_main(app), loop_factory=lambda: QEventLoop(app))
    except (TypeError, Exception) as exc:
        logger.debug("Falling back from loop_factory to explicit QEventLoop: %s", exc)
        loop = QEventLoop(app)
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(async_main(app))
        finally:
            loop.close()


if __name__ == "__main__":
    main()
