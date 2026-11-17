"""WebSocket client for ``/api/v1/sessions/{id}/events``.

Responsibilities:
- Authenticate with ``Sec-WebSocket-Protocol: bearer.<jwt>``.
- Dispatch every server event to a callback.
- Send ``client.heartbeat`` well inside the backend's 65 s idle timeout.
- Reconnect with backoff and ``client.resubscribe`` using the last sequence.
- Stop permanently on 4401/4403/4404 instead of retrying blindly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from desktop_client.config import Settings

logger = logging.getLogger(__name__)

# Server event types the overlay understands.
SESSION_STATUS_CHANGED = "session.status_changed"
TRANSCRIPT_DELTA = "speech.transcript_delta"
TRANSCRIPT_FINAL = "speech.transcript_final"
TRANSCRIPT_ERROR = "speech.transcript_error"
ARTIFACT_EXTRACTED = "artifact.extracted"
DEBRIEF_STATUS_CHANGED = "debrief.status_changed"
SYSTEM_HEARTBEAT = "system.heartbeat"
SYSTEM_ERROR = "system.error"
SYSTEM_REPLAY_GAP = "system.replay_gap"

# Close codes per the backend contract.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_MISSING_SESSION = 4404
_TERMINAL_CLOSE_CODES = {CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN, CLOSE_MISSING_SESSION}


class EventStreamAuthError(Exception):
    """Raised when the events socket is rejected for a non-retryable reason."""


@dataclass
class ServerEvent:
    event_type: str
    payload: Dict[str, Any]
    sequence_number: Optional[int]
    event_id: Optional[str]
    raw: Dict[str, Any]


EventHandler = Callable[[ServerEvent], Awaitable[None]]


def _parse_event(raw: Any) -> Optional[ServerEvent]:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring non-JSON event frame")
        return None
    if not isinstance(data, dict):
        return None

    event_type = data.get("event_type") or data.get("type")
    if not event_type:
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {k: v for k, v in data.items() if k not in {"event_type", "type"}}

    sequence = data.get("sequence_number")
    return ServerEvent(
        event_type=str(event_type),
        payload=payload,
        sequence_number=sequence if isinstance(sequence, int) else None,
        event_id=data.get("event_id"),
        raw=data,
    )


class EventClient:
    """Long-lived, self-healing subscription to a session's event stream."""

    def __init__(
        self,
        settings: Settings,
        session_id: str,
        on_event: EventHandler,
        *,
        on_replay_gap: Optional[Callable[[ServerEvent], Awaitable[None]]] = None,
        on_state_change: Optional[Callable[[str], None]] = None,
        on_auth_expired: Optional[Callable[[], Awaitable[bool]]] = None,
        heartbeat_seconds: Optional[float] = None,
        max_backoff_seconds: float = 30.0,
        initial_backoff_seconds: float = 1.0,
    ):
        self.settings = settings
        self.session_id = session_id
        self._on_event = on_event
        self._on_replay_gap = on_replay_gap
        self._on_state_change = on_state_change
        self._on_auth_expired = on_auth_expired
        self._heartbeat_seconds = heartbeat_seconds or max(
            1.0, min(settings.heartbeat_seconds, 30.0)
        )
        self._max_backoff = max_backoff_seconds
        self._initial_backoff = initial_backoff_seconds
        self._last_sequence = 0
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._ws: Optional[Any] = None

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    def _url(self) -> str:
        return self.settings.ws_url(self.session_id, "events")

    async def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover - best effort shutdown
                pass

    async def run(self) -> None:
        """Connect/consume/reconnect until :meth:`stop` is called."""
        backoff = self._initial_backoff
        auth_retry_used = False
        while not self._stop.is_set():
            try:
                await self._session_once()
                backoff = self._initial_backoff
                auth_retry_used = False
            except EventStreamAuthError as exc:
                if not auth_retry_used and await self._try_refresh():
                    auth_retry_used = True
                    logger.info("Retrying events socket with refreshed token")
                    continue
                logger.error("Events socket rejected: %s", exc)
                self._notify_state("unauthorized")
                return
            except (InvalidStatus, InvalidHandshake) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403):
                    if not auth_retry_used and await self._try_refresh():
                        auth_retry_used = True
                        logger.info("Retrying events handshake with refreshed token")
                        continue
                    logger.error("Events socket rejected (%s); not retrying", status)
                    self._notify_state("unauthorized")
                    return
                logger.warning("Events handshake failed: %s", exc)
            except (ConnectionClosed, OSError) as exc:
                logger.warning("Events socket closed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Unexpected events socket error: %s", exc)

            if self._stop.is_set():
                return
            self._notify_state("reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _try_refresh(self) -> bool:
        if self._on_auth_expired is None:
            return False
        try:
            return await self._on_auth_expired()
        except Exception:  # pragma: no cover - refresh must not kill the loop
            logger.exception("Token refresh callback failed")
            return False

    async def _session_once(self) -> None:
        url = self._url()
        subprotocols = [f"bearer.{self.settings.access_token}"]
        logger.info("Connecting events socket for session %s", self.session_id)
        async with websockets.connect(
            url, subprotocols=subprotocols, open_timeout=10, close_timeout=5
        ) as ws:
            self._ws = ws
            self._notify_state("connected")

            if self._last_sequence > 0:
                await self._send(
                    {
                        "type": "client.resubscribe",
                        "last_known_sequence": self._last_sequence,
                    }
                )

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                async for raw in ws:
                    event = _parse_event(raw)
                    if event is None:
                        continue
                    if event.sequence_number is not None:
                        self._last_sequence = max(
                            self._last_sequence, event.sequence_number
                        )
                    await self._dispatch(event)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._ws = None

                code = getattr(ws, "close_code", None)
                if code in _TERMINAL_CLOSE_CODES:
                    raise EventStreamAuthError(f"events socket closed with {code}")

    async def _dispatch(self, event: ServerEvent) -> None:
        if event.event_type == SYSTEM_REPLAY_GAP and self._on_replay_gap is not None:
            await self._on_replay_gap(event)
            return
        await self._on_event(event)

    async def _heartbeat_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                async with self._send_lock:
                    await ws.send(json.dumps({"type": "client.heartbeat"}))
            except (ConnectionClosed, OSError):
                return

    async def _send(self, message: Dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            await ws.send(json.dumps(message))

    def _notify_state(self, state: str) -> None:
        if self._on_state_change is not None:
            try:
                self._on_state_change(state)
            except Exception:  # pragma: no cover - UI callbacks must not break I/O
                logger.exception("State callback failed")
