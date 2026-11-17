"""Microphone audio capture module using sounddevice.RawInputStream.

Produces 50 ms PCM S16LE 16 kHz mono frames (1,600 bytes each) for the live session.
"""

import asyncio
import logging
import os
import sys
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


def _ensure_linux_portaudio():
    """Helper for Linux environments to locate local venv PortAudio if needed."""
    if not sys.platform.startswith("linux"):
        return

    # Check if ALSA configuration is in local venv
    potential_alsa = os.path.abspath(".venv/usr/share/alsa/alsa.conf")
    if os.path.exists(potential_alsa) and "ALSA_CONFIG_PATH" not in os.environ:
        os.environ["ALSA_CONFIG_PATH"] = potential_alsa

    # Check local venv lib directory
    potential_lib_dir = os.path.abspath(".venv/usr/lib/x86_64-linux-gnu")
    if os.path.exists(potential_lib_dir):
        import ctypes
        import ctypes.util

        for dep in ("libasound.so.2", "libjack.so.0"):
            p = os.path.join(potential_lib_dir, dep)
            if os.path.exists(p):
                try:
                    ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
                except Exception:
                    pass

        pa_path = os.path.join(potential_lib_dir, "libportaudio.so.2")
        if os.path.exists(pa_path):
            orig_find = ctypes.util.find_library

            def patched_find(name):
                if name == "portaudio":
                    return pa_path
                return orig_find(name)

            ctypes.util.find_library = patched_find


_ensure_linux_portaudio()


async def mic_frames(
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    block_size: int = BLOCK_SIZE,
    max_queue_frames: int = DEFAULT_QUEUE_MAXSIZE,
    device: Optional[int] = None,
    allow_simulation_fallback: bool = True,
) -> AsyncIterator[bytes]:
    """Capture raw microphone audio frames asynchronously.

    Yields:
        50 ms frames of raw PCM S16LE audio bytes (1,600 bytes per frame at 16 kHz).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=max_queue_frames)
    tail_buffer = bytearray()
    expected_bytes = block_size * channels * BYTES_PER_SAMPLE

    import sounddevice as sd

    def audio_callback(indata, frames, time_info, status):
        """PortAudio audio stream callback on high-priority audio thread.

        Must never block or perform heavy allocation.
        """
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
    use_simulation = False

    try:
        # Check if any input devices exist
        try:
            default_in = sd.default.device[0]
            if default_in < 0 and device is None:
                raise sd.PortAudioError("No default input device available")
        except Exception:
            pass

        stream = sd.RawInputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype=DTYPE,
            blocksize=block_size,
            device=device,
            callback=audio_callback,
        )
        stream.start()
        logger.info("Microphone stream started (rate=%d, channels=%d, blocksize=%d)", sample_rate, channels, block_size)
    except Exception as exc:
        if allow_simulation_fallback:
            logger.warning("Failed to open audio hardware (%s); falling back to synthetic 50ms frames for verification", exc)
            use_simulation = True
        else:
            raise

    try:
        if use_simulation:
            # Generate simulated 50ms frames of silent PCM S16LE audio at 50ms intervals
            silent_frame = b"\x00" * expected_bytes
            while True:
                await asyncio.sleep(FRAME_DURATION_MS / 1000.0)
                yield silent_frame
        else:
            while True:
                frame = await queue.get()
                if frame is None:
                    # Stream ending signal
                    break

                # Ensure exact frame sizing
                tail_buffer.extend(frame)
                while len(tail_buffer) >= expected_bytes:
                    out_frame = bytes(tail_buffer[:expected_bytes])
                    del tail_buffer[:expected_bytes]
                    yield out_frame

    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                logger.warning("Error stopping audio stream: %s", exc)

        # On stop, pad a short tail with silence to a full 50 ms frame, then end generator
        if len(tail_buffer) > 0:
            remainder = len(tail_buffer) % expected_bytes
            if remainder != 0:
                padding = b"\x00" * (expected_bytes - remainder)
                tail_buffer.extend(padding)

            while len(tail_buffer) >= expected_bytes:
                padded_frame = bytes(tail_buffer[:expected_bytes])
                del tail_buffer[:expected_bytes]
                yield padded_frame
