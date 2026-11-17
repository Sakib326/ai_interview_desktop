"""WebSocket client for ``/api/v1/sessions/{id}/audio``.

Sends raw PCM S16LE frames as binary messages, then ``client.audio_end`` and
waits for the backend's ``audio.closed`` / ``audio.failed`` finalization signal.
Transcript events never arrive on this socket; they come from ``/events``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from desktop_client.config import Settings

logger = logging.getLogger(__name__)

AUDIO_CLOSED = "audio.closed"
AUDIO_FAILED = "audio.failed"


@dataclass
class AudioOutcome:
    status: str  # closed | failed | disconnected | timeout
    error_class: Optional[str] = None
    retryable: bool = False
    message: str = ""


class AudioSender:
    """Streams microphone frames to a session's audio socket."""

    def __init__(
        self,
        settings: Settings,
        session_id: str,
        *,
        source: Optional[str] = None,
        finalize_timeout: float = 12.0,
        on_state_change=None,
    ):
        self.settings = settings
        self.session_id = session_id
        self.source = source or settings.audio_source
        self.finalize_timeout = finalize_timeout
        self._on_state_change = on_state_change
        self._outcome: asyncio.Queue[AudioOutcome] = asyncio.Queue()
        self._ws: Optional[Any] = None
        self.frames_sent = 0
        self.bytes_sent = 0

    def _url(self) -> str:
        query = f"source={self.source}" if self.source else ""
        return self.settings.ws_url(self.session_id, "audio", query)

    async def run(self, frames: AsyncIterator[bytes]) -> AudioOutcome:
        """Send frames until the iterator ends, then finalize the stream."""
        url = self._url()
        subprotocols = [f"bearer.{self.settings.access_token}"]
        try:
            async with websockets.connect(
                url, subprotocols=subprotocols, open_timeout=10, close_timeout=5
            ) as ws:
                self._ws = ws
                self._notify("streaming")
                receiver = asyncio.create_task(self._receive_loop(ws))
                try:
                    async for frame in frames:
                        if not frame:
                            continue
                        await ws.send(frame)
                        self.frames_sent += 1
                        self.bytes_sent += len(frame)

                    self._notify("finalizing")
                    await ws.send(json.dumps({"type": "client.audio_end"}))
                    try:
                        return await asyncio.wait_for(
                            self._outcome.get(), timeout=self.finalize_timeout
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "No audio.closed/audio.failed within %.1fs",
                            self.finalize_timeout,
                        )
                        return AudioOutcome(
                            "timeout", message="audio finalization not acknowledged"
                        )
                finally:
                    receiver.cancel()
                    try:
                        await receiver
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # pragma: no cover - receiver is defensive
                        logger.debug("Audio receiver ended with error", exc_info=True)
                    self._ws = None
        except (InvalidStatus, InvalidHandshake) as exc:
            logger.error("Audio socket handshake failed: %s", exc)
            return AudioOutcome("disconnected", message=str(exc))
        except (ConnectionClosed, OSError) as exc:
            logger.warning("Audio socket closed early: %s", exc)
            return AudioOutcome("disconnected", message=str(exc))

    async def _receive_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                kind = message.get("type")
                if kind == AUDIO_CLOSED:
                    await self._outcome.put(AudioOutcome("closed"))
                elif kind == AUDIO_FAILED:
                    await self._outcome.put(
                        AudioOutcome(
                            "failed",
                            error_class=message.get("error_class"),
                            retryable=bool(message.get("retryable", False)),
                            message=str(message.get("message") or ""),
                        )
                    )
        except ConnectionClosed:
            return

    def _notify(self, state: str) -> None:
        if self._on_state_change is not None:
            try:
                self._on_state_change(state)
            except Exception:  # pragma: no cover - UI callbacks must not break I/O
                logger.exception("Audio state callback failed")
