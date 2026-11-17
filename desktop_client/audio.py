"""Microphone audio capture using sounddevice.RawInputStream.

Produces 50 ms PCM S16LE 16 kHz mono frames (1,600 bytes each) for the live
session. PortAudio opens the device at 16 kHz and performs any device-rate
conversion; this skeleton does not implement a separate scipy resampling path.

Stopping is cooperative through an optional ``stop_event``. A graceful stop
flushes a short tail padded with silence; cancellation closes the stream
without yielding from ``finally``.
"""

import asyncio
import logging
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Standard audio framing parameters
SAMPLE_RATE = 16000          # 16 kHz
CHANNELS = 1                 # Mono
DTYPE = "int16"              # S16LE (2 bytes per sample)
FRAME_DURATION_MS = 50       # 50 ms
BLOCK_SIZE = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0))  # 800 samples
BYTES_PER_SAMPLE = 2
FRAME_BYTES = BLOCK_SIZE * CHANNELS * BYTES_PER_SAMPLE         # 1,600 bytes
DEFAULT_QUEUE_MAXSIZE = 40   # 40 frames ≈ 2.0 seconds of audio buffer
_QUEUE_POLL_SECONDS = 0.1


async def mic_frames(
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    block_size: int = BLOCK_SIZE,
    max_queue_frames: int = DEFAULT_QUEUE_MAXSIZE,
    device: Optional[int] = None,
    allow_simulation_fallback: bool = False,
    stop_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[bytes]:
    """Capture raw microphone audio frames asynchronously.

    Args:
        allow_simulation_fallback: Emit silent frames when no microphone is
            available. Intended only for headless verification; production runs
            should leave this false so a missing device is surfaced.
        stop_event: Optional event for a graceful stop. When set, the generator
            flushes a padded tail and returns instead of waiting forever.

    Yields:
        50 ms frames of raw PCM S16LE audio bytes (1,600 bytes per frame at 16 kHz).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_queue_frames)
    tail_buffer = bytearray()
    expected_bytes = block_size * channels * BYTES_PER_SAMPLE

    try:
        import sounddevice as sd
    except Exception as exc:
        if not allow_simulation_fallback:
            raise
        logger.warning(
            "PortAudio unavailable (%s); using opt-in synthetic 50 ms frames",
            exc,
        )
        sd = None

    def audio_callback(indata, frames, time_info, status):
        """PortAudio callback thread: never block or touch asyncio directly."""
        if status:
            logger.warning("PortAudio status: %s", status)

        data = bytes(indata)

        def _enqueue():
            if queue.full():
                try:
                    queue.get_nowait()
                    logger.warning("Audio queue full; dropped oldest frame")
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

        loop.call_soon_threadsafe(_enqueue)

    stream = None
    use_simulation = sd is None

    if sd is not None:
        try:
            stream = sd.RawInputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype=DTYPE,
                blocksize=block_size,
                device=device,
                callback=audio_callback,
            )
            stream.start()
            logger.info(
                "Microphone stream started (rate=%d, channels=%d, blocksize=%d)",
                sample_rate,
                channels,
                block_size,
            )
        except Exception as exc:
            if allow_simulation_fallback:
                logger.warning(
                    "Failed to open audio hardware (%s); using opt-in synthetic 50 ms frames",
                    exc,
                )
                use_simulation = True
            else:
                raise

    try:
        if use_simulation:
            silent_frame = b"\x00" * expected_bytes
            while not (stop_event and stop_event.is_set()):
                await asyncio.sleep(FRAME_DURATION_MS / 1000.0)
                yield silent_frame
            return

        while not (stop_event and stop_event.is_set()):
            try:
                frame = await asyncio.wait_for(
                    queue.get(), timeout=_QUEUE_POLL_SECONDS
                )
            except asyncio.TimeoutError:
                continue

            tail_buffer.extend(frame)
            while len(tail_buffer) >= expected_bytes:
                out_frame = bytes(tail_buffer[:expected_bytes])
                del tail_buffer[:expected_bytes]
                yield out_frame
    finally:
        # Never yield here: aclose()/cancellation throws GeneratorExit at the
        # current suspension point, and yielding from finally raises
        # "async generator ignored GeneratorExit".
        #
        # On a *graceful* stop, frames captured but not yet consumed are still
        # sitting in the queue (up to max_queue_frames). Drain them so the last
        # words before Stop are not silently dropped; the tail flush below
        # emits them as padded 50 ms frames.
        if stop_event is not None and stop_event.is_set():
            while True:
                try:
                    tail_buffer.extend(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                logger.warning("Error stopping audio stream: %s", exc)

    # Graceful stop only. Cancellation propagates past this point.
    if tail_buffer:
        remainder = len(tail_buffer) % expected_bytes
        if remainder:
            tail_buffer.extend(b"\x00" * (expected_bytes - remainder))
        while len(tail_buffer) >= expected_bytes:
            padded_frame = bytes(tail_buffer[:expected_bytes])
            del tail_buffer[:expected_bytes]
            yield padded_frame
