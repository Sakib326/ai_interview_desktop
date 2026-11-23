"""Local speech-to-text via Moonshine Voice.

This module is only imported/used when ``DESKTOP_STT_PROVIDER=moonshine``. It
keeps the Moonshine dependency optional so the default backend-audio path does
not require it.

We feed PCM frames captured by :func:`desktop_client.audio.mic_frames` into a
Moonshine ``Transcriber`` (not ``MicTranscriber``) so we reuse the existing
capture pipeline and support both mic and loopback sources without opening a
second audio device.
"""

from __future__ import annotations

import asyncio
import logging
from array import array
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PartialCallback = Callable[[str, str, str], None]  # source, segment_id, text
FinalCallback = Callable[[str, str, str, Optional[float]], None]  # source, segment_id, text, confidence


@dataclass
class MoonshineCallbacks:
    on_partial: PartialCallback
    on_final: FinalCallback
    on_error: Callable[[str, Exception], None]


class MoonshineUnavailableError(RuntimeError):
    pass


def _import_moonshine():
    try:
        import moonshine_voice  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local install
        raise MoonshineUnavailableError(
            "DESKTOP_STT_PROVIDER=moonshine requires the 'moonshine-voice' package. "
            "Install it with: pip install moonshine-voice"
        ) from exc
    return moonshine_voice


def _pcm_s16le_bytes_to_float32(data: bytes):
    """Convert PCM S16LE bytes to float32 array in [-1, 1] for Moonshine."""
    try:
        import numpy as np  # type: ignore

        pcm = np.frombuffer(data, dtype=np.int16)
        return pcm.astype(np.float32) / 32768.0
    except Exception:
        # Fallback: array('f') is accepted by some APIs; list also works.
        samples = array("h")
        samples.frombytes(bytes(data))
        return [s / 32768.0 for s in samples]


def _listener_base():
    try:
        from moonshine_voice.transcriber import TranscriptEventListener  # type: ignore

        return TranscriptEventListener
    except Exception:  # pragma: no cover - defensive
        return object


class _Listener(_listener_base()):
    """Bridges Moonshine transcriber listener events into asyncio callbacks."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        source: str,
        callbacks: MoonshineCallbacks,
    ):
        base = _listener_base()
        if base is not object:
            try:
                super().__init__()
            except Exception:
                pass
        self._loop = loop
        self._source = source
        self._callbacks = callbacks
        self._segment_seq = 0
        self._active_line_ids: dict[int, str] = {}
        self.event_count = 0

    def _segment_id(self, line_id: Any) -> str:
        key = int(line_id) if line_id is not None else -1
        if key not in self._active_line_ids:
            self._segment_seq += 1
            self._active_line_ids[key] = f"moonshine-{self._source}-{self._segment_seq}"
        return self._active_line_ids[key]

    def _emit(self, fn, *args):
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            # Loop closed during shutdown; drop callback.
            pass

    def _count_event(self, event) -> None:
        self.event_count += 1
        if self.event_count <= 5 or self.event_count % 50 == 0:
            logger.info(
                "[moonshine:%s] event #%d %s",
                self._source, self.event_count, event.__class__.__name__,
            )

    def on_line_started(self, event):  # pragma: no cover - exercised via integration
        try:
            self._count_event(event)
            line = getattr(event, "line", None)
            text = str(getattr(line, "text", "") or "").strip()
            line_id = getattr(line, "id", None)
            logger.info("[moonshine:%s] line_started id=%s text=%r", self._source, line_id, text[:80])
            if not text:
                return
            seg = self._segment_id(line_id)
            self._emit(self._callbacks.on_partial, self._source, seg, text)
        except Exception as exc:
            logger.exception("[moonshine:%s] line_started handler failed", self._source)
            self._emit(self._callbacks.on_error, self._source, exc)

    def on_line_text_changed(self, event):  # pragma: no cover
        try:
            self._count_event(event)
            line = getattr(event, "line", None)
            text = str(getattr(line, "text", "") or "").strip()
            line_id = getattr(line, "id", None)
            logger.info("[moonshine:%s] line_text_changed id=%s text=%r", self._source, line_id, text[:80])
            if not text:
                return
            seg = self._segment_id(line_id)
            self._emit(self._callbacks.on_partial, self._source, seg, text)
        except Exception as exc:
            logger.exception("[moonshine:%s] line_text_changed handler failed", self._source)
            self._emit(self._callbacks.on_error, self._source, exc)

    def on_line_updated(self, event):  # pragma: no cover
        # Some builds emit updated without text_changed; treat as partial refresh.
        logger.debug("[moonshine:%s] line_updated", self._source)
        self.on_line_text_changed(event)

    def on_line_completed(self, event):  # pragma: no cover
        try:
            self._count_event(event)
            line = getattr(event, "line", None)
            text = str(getattr(line, "text", "") or "").strip()
            line_id = getattr(line, "id", None)
            confidence = getattr(line, "confidence", None)
            logger.info("[moonshine:%s] line_completed id=%s conf=%s text=%r", self._source, line_id, confidence, text[:120])
            seg = self._segment_id(line_id)
            self._active_line_ids.pop(int(line_id) if line_id is not None else -1, None)
            if not text:
                return
            conf = float(confidence) if isinstance(confidence, (int, float)) else None
            self._emit(self._callbacks.on_final, self._source, seg, text, conf)
        except Exception as exc:
            logger.exception("[moonshine:%s] line_completed handler failed", self._source)
            self._emit(self._callbacks.on_error, self._source, exc)

    def on_line_speakers_changed(self, event):  # pragma: no cover - not used
        return

    def on_error(self, event):  # pragma: no cover - defensive
        try:
            err = getattr(event, "error", None)
            if err:
                self._emit(self._callbacks.on_error, self._source, err if isinstance(err, Exception) else Exception(str(err)))
        except Exception:
            pass

    def __call__(self, event):  # pragma: no cover - fallback
        if hasattr(event, "line") and event.__class__.__name__ == "LineStarted":
            self.on_line_started(event)
        elif hasattr(event, "line") and event.__class__.__name__ == "LineTextChanged":
            self.on_line_text_changed(event)
        elif hasattr(event, "line") and event.__class__.__name__ == "LineUpdated":
            self.on_line_updated(event)
        elif hasattr(event, "line") and event.__class__.__name__ == "LineCompleted":
            self.on_line_completed(event)


class MoonshineTranscriberSession:
    """One Moonshine transcriber fed by externally-captured PCM frames."""

    def __init__(
        self,
        *,
        source: str,
        sample_rate: int,
        model_path: str = "",
        model_arch: str = "",
        language: str = "en",
        callbacks: MoonshineCallbacks,
    ):
        self.source = source
        self.sample_rate = sample_rate
        self.model_path = model_path
        self.model_arch = model_arch
        self.language = language
        self.callbacks = callbacks
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._transcriber: Any = None
        self._listener: Optional[_Listener] = None
        self._closed = False

    async def start(self) -> None:
        if self._transcriber is not None:
            return
        self._loop = asyncio.get_running_loop()
        mv = _import_moonshine()
        Transcriber = getattr(mv, "Transcriber", None)
        if Transcriber is None:  # pragma: no cover - defensive
            raise MoonshineUnavailableError("moonshine_voice.Transcriber not found")

        model_path = self.model_path
        model_arch = self.model_arch
        if not model_path:
            get_model_for_language = getattr(mv, "get_model_for_language", None)
            if callable(get_model_for_language):
                try:
                    resolved = await asyncio.to_thread(
                        get_model_for_language, self.language, model_arch or None
                    )
                    if isinstance(resolved, tuple) and len(resolved) == 2:
                        model_path, model_arch = resolved
                    elif isinstance(resolved, str):
                        model_path = resolved
                except Exception as exc:
                    raise MoonshineUnavailableError(
                        f"Could not resolve Moonshine model for language "
                        f"{self.language!r}: {exc}"
                    ) from exc

        if not model_path:
            raise MoonshineUnavailableError(
                "Moonshine model path could not be resolved. Set DESKTOP_MOONSHINE_MODEL "
                "to a local model directory, or ensure moonshine-voice can download "
                f"the default model for language {self.language!r}."
            )

        args: list[Any] = [str(model_path)]
        kwargs: dict[str, Any] = {}
        if model_arch:
            kwargs["model_arch"] = model_arch
        transcriber = Transcriber(*args, **kwargs)

        self._listener = _Listener(
            loop=self._loop, source=self.source, callbacks=self.callbacks
        )
        add_listener = getattr(transcriber, "add_listener", None)
        if callable(add_listener):
            add_listener(self._listener)

        load = getattr(transcriber, "load", None)
        if callable(load):
            await asyncio.to_thread(load)
        start = getattr(transcriber, "start", None)
        if callable(start):
            await asyncio.to_thread(start)
        self._transcriber = transcriber
        self._install_debug_hooks(transcriber)
        logger.info(
            "Moonshine transcriber started for source=%s model=%s arch=%s",
            self.source,
            model_path,
            model_arch,
        )

    def _install_debug_hooks(self, transcriber: Any) -> None:
        """Temporary debug: log what the stream sees/produces."""
        try:
            stream = transcriber.get_default_stream()
        except Exception:
            logger.exception("[moonshine:%s] debug: could not get default stream", self.source)
            return

        orig_notify = getattr(stream, "_notify_from_transcript", None)
        if callable(orig_notify):
            source = self.source

            def _wrapped_notify(transcript):
                try:
                    lines = getattr(transcript, "lines", []) or []
                    logger.info(
                        "[moonshine:%s] transcript lines=%d",
                        source, len(lines),
                    )
                    for i, line in enumerate(lines[:5]):
                        logger.info(
                            "[moonshine:%s] line[%d] text=%r is_new=%s is_updated=%s has_text_changed=%s is_complete=%s",
                            source, i,
                            str(getattr(line, "text", "") or "")[:80],
                            getattr(line, "is_new", None),
                            getattr(line, "is_updated", None),
                            getattr(line, "has_text_changed", None),
                            getattr(line, "is_complete", None),
                        )
                except Exception:
                    logger.exception("[moonshine:%s] debug notify inspect failed", source)
                return orig_notify(transcript)

            stream._notify_from_transcript = _wrapped_notify

        orig_update = getattr(stream, "update_transcription", None)
        if callable(orig_update):
            source = self.source

            def _wrapped_update(flags=0):
                t = orig_update(flags)
                try:
                    lines = getattr(t, "lines", []) or []
                    if lines or getattr(stream, "_last_pass", 0) > 0.01:
                        logger.info(
                            "[moonshine:%s] update_transcription flags=%s lines=%d last_pass=%s",
                            source, flags, len(lines), getattr(stream, "_last_pass", None),
                        )
                except Exception:
                    pass
                return t

            stream.update_transcription = _wrapped_update
            logger.info("[moonshine:%s] debug hooks installed", self.source)

    async def feed(self, pcm_s16le: bytes) -> None:
        if self._closed or self._transcriber is None or not pcm_s16le:
            return
        audio = _pcm_s16le_bytes_to_float32(pcm_s16le)
        if not hasattr(self, "_feed_debug_count"):
            self._feed_debug_count = 0
        self._feed_debug_count += 1
        if self._feed_debug_count <= 3 or self._feed_debug_count % 200 == 0:
            try:
                import numpy as np
                arr = np.asarray(audio)
                logger.info(
                    "[moonshine:%s] feed#%d len=%d dtype=%s min=%.4f max=%.4f mean=%.6f",
                    self.source, self._feed_debug_count, len(arr), arr.dtype,
                    float(arr.min()) if arr.size else 0, float(arr.max()) if arr.size else 0,
                    float(arr.mean()) if arr.size else 0,
                )
            except Exception:
                logger.info("[moonshine:%s] feed#%d len=%s type=%s", self.source, self._feed_debug_count, len(audio) if hasattr(audio,'__len__') else '?', type(audio))
        add_audio = getattr(self._transcriber, "add_audio", None)
        if not callable(add_audio):  # pragma: no cover - defensive
            raise MoonshineUnavailableError("moonshine_voice.Transcriber.add_audio not found")
        # add_audio is CPU-bound-ish but fast; keep it off the event loop.
        await asyncio.to_thread(add_audio, audio, self.sample_rate)

    def debug_state(self) -> dict[str, Any]:
        """Best-effort internal state for diagnosing no-transcript issues."""
        t = self._transcriber
        if t is None:
            return {"transcriber": None}
        stream = getattr(t, "_default_stream", None)
        state: dict[str, Any] = {
            "has_stream": stream is not None,
            "event_count": getattr(self._listener, "event_count", None),
        }
        if stream is not None:
            for attr in (
                "_stream_time", "_last_update_time", "_last_pass",
                "_update_interval", "_transcribe_flags",
            ):
                state[attr] = getattr(stream, attr, None)
            listeners = getattr(stream, "_listeners", None)
            state["listeners"] = len(listeners) if listeners is not None else None
        return state

    async def stop(self) -> None:
        if self._transcriber is None:
            return
        transcriber = self._transcriber
        self._transcriber = None
        self._closed = True
        stop = getattr(transcriber, "stop", None)
        close = getattr(transcriber, "close", None)
        try:
            if callable(stop):
                await asyncio.to_thread(stop)
        finally:
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception:
                    logger.debug("Moonshine close failed", exc_info=True)
        logger.info("Moonshine transcriber stopped for source=%s", self.source)