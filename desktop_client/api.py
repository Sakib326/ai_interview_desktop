"""Async HTTP client for the backend REST + SSE API.

Only the endpoints the Live overlay needs are wrapped. Every method returns
plain dicts from the API so the UI layer can stay tolerant of additive backend
fields.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import httpx

from desktop_client.config import Settings, token_expiring

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """A backend error with enough structure for retry/UX decisions."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        detail: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code} ({self.status_code}): {self.message}"


def _error_from_response(response: httpx.Response, body: Any) -> ApiError:
    code = "http_error"
    message = response.text or response.reason_phrase
    retryable = response.status_code >= 500 or response.status_code == 429

    detail = body
    if isinstance(body, dict):
        inner = body.get("detail", body)
        if isinstance(inner, dict):
            detail = inner
            code = str(inner.get("code") or inner.get("error_class") or code)
            message = str(inner.get("message") or inner.get("detail") or message)
            retryable = bool(inner.get("retryable", retryable))
        elif isinstance(inner, str):
            message = inner
    if response.status_code == 401:
        code = "unauthorized"
        retryable = False
    return ApiError(
        response.status_code, code, message, retryable=retryable, detail=detail
    )


async def parse_sse_stream(
    lines: AsyncIterator[str],
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    """Parse SSE ``event:``/``data:`` frames into (event_type, payload) pairs.

    ``httpx.Response.aiter_lines`` already removes line terminators, so frames
    are separated by empty strings. Comment lines and unknown fields are
    ignored, per the SSE spec.
    """
    event_type: Optional[str] = None
    data_lines = []
    async for line in lines:
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Dropping malformed SSE frame: %s", raw[:200])
                else:
                    yield event_type or "message", payload
            event_type = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        raw = "\n".join(data_lines)
        try:
            yield event_type or "message", json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Dropping malformed trailing SSE frame: %s", raw[:200])


class ApiClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.settings = settings
        self._refresh_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=settings.api_v1,
            timeout=settings.request_timeout_seconds,
            transport=transport,
            headers=self._auth_headers(),
        )

    # ------------------------------------------------------------------ plumbing
    def _auth_headers(self) -> Dict[str, str]:
        token = self.settings.access_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    @property
    def can_refresh(self) -> bool:
        return bool(self.settings.refresh_token and self.settings.device_id)

    async def refresh_token(self) -> bool:
        """Rotate the access token via ``POST /auth/refresh``.

        Updates the shared :class:`Settings` (mutated in place), so the
        WebSocket clients pick up the new bearer token on their next connect.
        Returns False when refresh is not configured or the server rejects it.
        """
        if not self.can_refresh:
            return False
        async with self._refresh_lock:
            try:
                response = await self._client.post(
                    "/auth/refresh",
                    json={
                        "refresh_token": self.settings.refresh_token,
                        "device_id": self.settings.device_id,
                    },
                )
            except httpx.HTTPError as exc:
                logger.warning("Token refresh failed: %s", exc)
                return False
            if response.status_code != 200:
                logger.warning(
                    "Token refresh rejected (%s)", response.status_code
                )
                return False
            try:
                data = response.json()
            except ValueError:
                return False

        access = data.get("access_token")
        if not access:
            return False
        self.settings.access_token = access
        # The refresh token rotates on every use.
        self.settings.refresh_token = data.get("refresh_token") or self.settings.refresh_token
        self.settings.device_id = data.get("device_id") or self.settings.device_id
        self._client.headers["Authorization"] = f"Bearer {access}"
        logger.info("Access token refreshed")
        return True

    async def ensure_fresh_token(
        self, within_seconds: float = 120.0, force: bool = False
    ) -> bool:
        """Refresh ahead of expiry. No-op when refresh is not configured."""
        if not self.can_refresh:
            return False
        if not force and not token_expiring(
            self.settings.access_token, within_seconds
        ):
            return False
        return await self.refresh_token()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, _allow_refresh: bool = True, **kwargs: Any
    ) -> Any:
        if token_expiring(self.settings.access_token):
            await self.ensure_fresh_token()

        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(0, "network_error", str(exc), retryable=True) from exc

        if response.status_code == 401 and _allow_refresh:
            if await self.refresh_token():
                return await self._request(
                    method, path, _allow_refresh=False, **kwargs
                )

        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            raise _error_from_response(response, body)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ------------------------------------------------------------------ policies
    async def acceptable_use(self) -> Dict[str, Any]:
        return await self._request("GET", "/policies/acceptable-use")

    # ------------------------------------------------------------------ sessions
    async def preflight(
        self, kind: str = "live", acknowledged_policy_version: Optional[str] = None,
        goal_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"kind": kind}
        if acknowledged_policy_version:
            body["acknowledged_policy_version"] = acknowledged_policy_version
        if goal_id:
            body["goal_id"] = goal_id
        return await self._request("POST", "/sessions/preflight", json=body)

    async def create_session(
        self,
        kind: str = "live",
        acknowledged_policy_version: Optional[str] = None,
        goal_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"kind": kind}
        if acknowledged_policy_version:
            body["acknowledged_policy_version"] = acknowledged_policy_version
        if goal_id:
            body["goal_id"] = goal_id
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return await self._request("POST", "/sessions", json=body, headers=headers)

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/sessions/{session_id}")

    async def active_session(self) -> Optional[Dict[str, Any]]:
        return await self._request("GET", "/sessions/active")

    async def acknowledge(self, session_id: str, policy_version: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/sessions/{session_id}/acknowledge",
            json={"policy_version": policy_version},
        )

    async def end_session(
        self, session_id: str, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return await self._request(
            "POST", f"/sessions/{session_id}/end", headers=headers
        )

    async def transcript(self, session_id: str) -> list:
        return await self._request("GET", f"/sessions/{session_id}/transcript")

    # ------------------------------------------------------------------ answers
    async def answer(
        self,
        session_id: str,
        question_turn_id: str,
        candidate_turn_id: Optional[str] = None,
        *,
        gated: bool = False,
    ) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
        """Stream ``assistant.answer_*`` SSE events for one question turn.

        ``gated=True`` uses ``POST /sessions/{id}/live/answer``, where the
        backend Turn Gate may answer ``assistant.answer_skipped`` instead of
        generating. The default manual path is never gated.
        """
        body: Dict[str, Any] = {"question_turn_id": question_turn_id}
        if candidate_turn_id:
            body["candidate_turn_id"] = candidate_turn_id
        key_prefix = "live-answer" if gated else "answer"
        headers = {
            "Accept": "text/event-stream",
            # Stable per (mode, question) so a retry replays the recorded run.
            "Idempotency-Key": f"{key_prefix}-{question_turn_id}",
        }
        channel = "live/answer" if gated else "practice/answer"
        url = f"/sessions/{session_id}/{channel}"
        try:
            async with self._client.stream(
                "POST", url, json=body, headers=headers
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    raise _error_from_response(response, payload)
                async for event_type, payload in parse_sse_stream(
                    response.aiter_lines()
                ):
                    yield event_type, payload
        except httpx.HTTPError as exc:
            raise ApiError(0, "network_error", str(exc), retryable=True) from exc

    # ---------------------------------------------------------------- artifacts
    async def solve_artifact(
        self,
        session_id: str,
        *,
        image_bytes: Optional[bytes] = None,
        extracted_text: Optional[str] = None,
        auto_capture: bool = False,
    ) -> Dict[str, Any]:
        """Submit a screen capture (or already-extracted text) as an artifact.

        The backend classifies the text into an artifact type and publishes
        ``artifact.extracted``; the image itself is never retained.
        """
        body: Dict[str, Any] = {"auto_capture": auto_capture}
        if extracted_text:
            body["extracted_text"] = extracted_text
        if image_bytes:
            body["image_base64"] = base64.b64encode(image_bytes).decode("ascii")
        return await self._request("POST", f"/sessions/{session_id}/solve", json=body)

    async def answer_artifact(
        self, session_id: str, artifact_id: str
    ) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
        """Stream the LLM answer generated for a captured artifact."""
        url = f"/sessions/{session_id}/artifacts/{artifact_id}/answer"
        headers = {"Accept": "text/event-stream"}
        try:
            async with self._client.stream(
                "POST", url, headers=headers, timeout=120.0
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    raise _error_from_response(response, payload)
                async for event_type, payload in parse_sse_stream(
                    response.aiter_lines()
                ):
                    yield event_type, payload
        except httpx.HTTPError as exc:
            raise ApiError(0, "network_error", str(exc), retryable=True) from exc

    # ------------------------------------------------------------------ debrief
    async def debrief_status(self, session_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/sessions/{session_id}/debrief/status")

    async def debrief(self, session_id: str) -> Dict[str, Any]:
        """Fetch the debrief.

        A ``202`` (still writing) is a normal, non-error response from the
        backend and comes back as ``{"status": "writing"}``. A ``404`` raises
        :class:`ApiError` because it means no_content/generate_failed/unavailable.
        """
        return await self._request("GET", f"/sessions/{session_id}/debrief")

    async def retry_debrief(self, session_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST", f"/sessions/{session_id}/debrief/retry"
        )

    # ------------------------------------------------------------------ memory
    async def list_memory(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        return await self._request(
            "GET", "/memory", params={"limit": limit, "offset": offset}
        )

    async def delete_memory(self, memory_id: str) -> None:
        await self._request("DELETE", f"/memory/{memory_id}")

    async def clear_memory(self) -> None:
        await self._request("DELETE", "/memory")

    async def retry_memory(self, session_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST", "/memory/retry", params={"session_id": session_id}
        )
