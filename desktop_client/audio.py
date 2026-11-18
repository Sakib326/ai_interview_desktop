"""Audio capture producing 50 ms PCM S16LE 16 kHz mono frames (1,600 bytes).

Supports microphone input (default) and, on Windows, WASAPI system/loopback
capture so interviewer audio played through the speakers can be transcribed.

Stopping is cooperative through an optional ``stop_event``. A graceful stop
flushes a short tail padded with silence; cancellation closes the stream
without yielding from ``finally``.
"""

import asyncio
import logging
import sys
from array import array
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


def _downmix_s16le(data: bytes, channels: int) -> array:
    """Downmix interleaved PCM S16LE samples to mono."""
    samples = array("h")
    samples.frombytes(data)
    if channels <= 1:
        return samples
    frame_count = len(samples) // channels
    mono = array("h", [0]) * frame_count
    for i in range(frame_count):
        base = i * channels
        total = 0
        for c in range(channels):
            total += samples[base + c]
        mono[i] = int(total / channels)
    return mono


def _resample_mono(samples: array, in_rate: int, out_rate: int, state: dict) -> array:
    """Streaming linear resample of mono S16LE samples to ``out_rate``."""
    if in_rate == out_rate:
        return samples
    buf = state.get("buf")
    if buf is None:
        buf = array("h")
    buf.extend(samples)
    if len(buf) < 2:
        state["buf"] = buf
        return array("h")
    step = in_rate / float(out_rate)
    pos = float(state.get("pos", 0.0))
    out = array("h")
    limit = len(buf) - 1
    while pos < limit:
        i = int(pos)
        if i + 1 >= len(buf):
            break
        frac = pos - i
        s0 = buf[i]
        s1 = buf[i + 1]
        out.append(int(s0 + (s1 - s0) * frac))
        pos += step
    consumed = int(pos)
    if consumed > 0:
        state["buf"] = buf[consumed:]
        state["pos"] = pos - consumed
    else:
        state["buf"] = buf
        state["pos"] = pos
    return out


def _convert_to_target(data: bytes, convert_ctx: dict) -> bytes:
    """Convert captured audio to 16 kHz mono PCM S16LE bytes."""
    mono = _downmix_s16le(data, convert_ctx["in_channels"])
    in_rate = convert_ctx["in_rate"]
    if in_rate != SAMPLE_RATE:
        mono = _resample_mono(mono, in_rate, SAMPLE_RATE, convert_ctx["state"])
    return mono.tobytes()


def _wasapi_loopback_extra(sd):
    """Best-effort WASAPI loopback extra settings for the installed sounddevice.

    Returns ``(extra_settings_or_None, loopback_flag_supported)``. Some
    sounddevice builds accept ``WasapiSettings(loopback=True)``; others do not
    expose the flag, in which case we fall back to a loopback-capable input
    device (e.g. Stereo Mix) if available.
    """
    settings_cls = getattr(sd, "WasapiSettings", None)
    if settings_cls is None:
        return None, False
    try:
        extra = settings_cls(loopback=True)
        # If construction accepts the kwarg, treat loopback as supported.
        return extra, bool(getattr(extra, "loopback", True))
    except TypeError:
        try:
            extra = settings_cls()
        except Exception:
            return None, False
        try:
            extra.loopback = True
        except Exception:
            return extra, False
        return extra, bool(getattr(extra, "loopback", False))
    except Exception:
        return None, False


def _find_loopback_input_device(sd):
    """Return a loopback-capable input device index, if available.

    Prefers explicit WASAPI loopback endpoints, then common loopback/mix inputs
    such as "Stereo Mix", "What U Hear", or virtual cable outputs.
    """
    try:
        hostapis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:
        return None

    wasapi_idx = None
    for i, api in enumerate(hostapis):
        if "wasapi" in (api.get("name") or "").lower():
            wasapi_idx = i
            break

    candidates = []
    for idx, dev in enumerate(devices):
        if (dev.get("max_input_channels") or 0) <= 0:
            continue
        name = (dev.get("name") or "").lower()
        score = 0
        if "loopback" in name:
            score += 10
        if "stereo mix" in name:
            score += 8
        if "what u hear" in name or "wave out mix" in name or "wave out" in name:
            score += 8
        if "cable output" in name or "vb-audio" in name or "virtual-audio-capturer" in name:
            score += 6
        if score == 0:
            continue
        if dev.get("hostapi") == wasapi_idx:
            score += 2
        candidates.append((score, idx))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _open_loopback_stream(sd, sample_rate, channels, dtype, block_size, device, callback):
    """Open a system/loopback capture stream (Windows).

    Returns ``(stream, convert_ctx)`` where ``convert_ctx`` is None when the
    stream already delivers ``sample_rate``/``channels`` frames, or a dict with
    conversion parameters when capture is at the device's native format.
    """
    if sys.platform != "win32":
        raise RuntimeError("System/loopback audio capture is only supported on Windows")

    extra, loopback_flag_ok = _wasapi_loopback_extra(sd)

    # Preferred path: WASAPI loopback flag on the output/render device.
    if loopback_flag_ok and extra is not None:
        out_idx = sd.default.device[1] if device is None else device
        if out_idx is not None and not (isinstance(out_idx, int) and out_idx < 0):
            # Prefer direct 16 kHz mono capture if PortAudio/WASAPI accepts it.
            try:
                stream = sd.RawInputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype=dtype,
                    blocksize=block_size,
                    device=out_idx,
                    callback=callback,
                    extra_settings=extra,
                )
                stream.start()
                logger.info(
                    "Loopback stream started (direct %d Hz, %d ch, blocksize=%d)",
                    sample_rate,
                    channels,
                    block_size,
                )
                return stream, None
            except Exception as exc:
                logger.info("Direct loopback open failed (%s); trying native format", exc)

            # Fall back to native output-device format and convert to 16 kHz mono.
            try:
                info = sd.query_devices(out_idx)
                in_rate = int(info.get("default_samplerate") or sample_rate)
                in_channels = int(info.get("max_output_channels") or channels or 2)
                if in_channels <= 0:
                    in_channels = 2
                in_blocksize = max(1, int(in_rate * (FRAME_DURATION_MS / 1000.0)))
                stream = sd.RawInputStream(
                    samplerate=in_rate,
                    channels=in_channels,
                    dtype=dtype,
                    blocksize=in_blocksize,
                    device=out_idx,
                    callback=callback,
                    extra_settings=extra,
                )
                stream.start()
                logger.info(
                    "Loopback stream started (native %d Hz, %d ch -> %d Hz mono)",
                    in_rate,
                    in_channels,
                    sample_rate,
                )
                return stream, {
                    "in_rate": in_rate,
                    "in_channels": in_channels,
                    "state": {"buf": array("h"), "pos": 0.0},
                }
            except Exception as exc:
                logger.info(
                    "Native loopback open failed (%s); trying loopback device", exc
                )

    # Fallback: use an explicit loopback-capable input device (e.g. Stereo Mix).
    loop_idx = _find_loopback_input_device(sd)
    if loop_idx is None and device is not None:
        loop_idx = device
    if loop_idx is None:
        raise RuntimeError(
            "System/loopback audio capture not available: WASAPI loopback is not "
            "supported by this sounddevice/PortAudio build and no loopback-capable "
            "input device (e.g. Stereo Mix) was found. Enable Stereo Mix in Windows "
            "sound settings or use a virtual audio cable, or update sounddevice."
        )

    try:
        stream = sd.RawInputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype=dtype,
            blocksize=block_size,
            device=loop_idx,
            callback=callback,
        )
        stream.start()
        logger.info(
            "Loopback stream started (device %s, %d Hz, %d ch)",
            loop_idx,
            sample_rate,
            channels,
        )
        return stream, None
    except Exception as exc:
        logger.info("Direct loopback device open failed (%s); trying native format", exc)

    info = sd.query_devices(loop_idx)
    in_rate = int(info.get("default_samplerate") or sample_rate)
    in_channels = int(info.get("max_input_channels") or channels or 2)
    if in_channels <= 0:
        in_channels = 2
    in_blocksize = max(1, int(in_rate * (FRAME_DURATION_MS / 1000.0)))
    stream = sd.RawInputStream(
        samplerate=in_rate,
        channels=in_channels,
        dtype=dtype,
        blocksize=in_blocksize,
        device=loop_idx,
        callback=callback,
    )
    stream.start()
    logger.info(
        "Loopback stream started (device %s, native %d Hz, %d ch -> %d Hz mono)",
        loop_idx,
        in_rate,
        in_channels,
        sample_rate,
    )
    return stream, {
        "in_rate": in_rate,
        "in_channels": in_channels,
        "state": {"buf": array("h"), "pos": 0.0},
    }


def _open_mic_stream(sd, sample_rate, channels, dtype, blocksize, device, callback):
    stream = sd.RawInputStream(
        samplerate=sample_rate,
        channels=channels,
        dtype=dtype,
        blocksize=blocksize,
        device=device,
        callback=callback,
    )
    stream.start()
    logger.info(
        "Microphone stream started (rate=%d, channels=%d, blocksize=%d)",
        sample_rate,
        channels,
        blocksize,
    )
    return stream


async def mic_frames(
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    block_size: int = BLOCK_SIZE,
    max_queue_frames: int = DEFAULT_QUEUE_MAXSIZE,
    device: Optional[int] = None,
    source: str = "mic",
    allow_simulation_fallback: bool = False,
    stop_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[bytes]:
    """Capture raw audio frames asynchronously.

    Args:
        source: ``"mic"`` for microphone input (default) or ``"loopback"`` for
            system/output audio capture on Windows (interviewer audio).
        allow_simulation_fallback: Emit silent frames when no capture device is
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
    convert_ctx = None

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
        if convert_ctx is not None:
            try:
                data = _convert_to_target(data, convert_ctx)
            except Exception:
                logger.exception("Audio conversion failed")
                return
            if not data:
                return

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
            if source == "loopback":
                stream, convert_ctx = _open_loopback_stream(
                    sd, sample_rate, channels, DTYPE, block_size, device, audio_callback
                )
            else:
                stream = _open_mic_stream(
                    sd, sample_rate, channels, DTYPE, block_size, device, audio_callback
                )
        except Exception as exc:
            if allow_simulation_fallback:
                logger.warning(
                    "Failed to open audio hardware (%s); using opt-in synthetic 50 ms frames",
                    exc,
                )
                use_simulation = True
                stream = None
                convert_ctx = None
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


async def capture_frames(*args, source: str = "mic", **kwargs) -> AsyncIterator[bytes]:
    """Alias for :func:`mic_frames` with an explicit ``source`` argument."""
    async for frame in mic_frames(*args, source=source, **kwargs):
        yield frame
