"""Interactive browser login via the backend portal handoff.

The desktop never talks to the identity provider directly and never reads the
portal's session cookie. It runs PKCE, opens the portal in the system browser,
receives a one-time code on a loopback listener, and exchanges that code at
``POST /api/v1/auth/handoff`` for its own platform tokens.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import logging
import platform
import secrets
import sys
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from desktop_client import __version__
from desktop_client.config import Settings

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/callback"
DEFAULT_TIMEOUT_SECONDS = 120.0
_MAX_REQUEST_LINE = 8192
_MAX_HEADER_LINES = 50


class AuthError(Exception):
    """Login could not be completed."""


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    refresh_token: str
    device_id: str
    email: str = ""
    display_name: str = ""


def pkce_challenge(verifier: str) -> str:
    """BASE64URL(SHA256(verifier)) without padding, per RFC 7636."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_pkce() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    return verifier, pkce_challenge(verifier)


def validate_state(expected: str, actual: str) -> None:
    if not hmac.compare_digest(expected, actual or ""):
        raise AuthError("State mismatch; login aborted")


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def build_portal_url(
    settings: Settings, *, challenge: str, state: str, redirect_uri: str
) -> str:
    params = {
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "redirect_uri": redirect_uri,
        "scope": settings.oauth_scope,
    }
    separator = "&" if "?" in settings.portal_url else "?"
    return f"{settings.portal_url}{separator}{urlencode(params)}"


class LoopbackReceiver:
    """One-shot HTTP listener for the browser redirect."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        path: str = CALLBACK_PATH,
    ):
        self.host = host
        self.port = port
        self.path = path or CALLBACK_PATH
        self._server: Optional[asyncio.base_events.Server] = None
        self._future: Optional[asyncio.Future] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        try:
            self._server = await asyncio.start_server(
                self._handle, self.host, self.port
            )
        except OSError as exc:
            raise AuthError(
                f"Could not listen on {self.host}:{self.port or 'auto'} for the "
                f"login callback: {exc}"
            ) from exc
        self.port = self._server.sockets[0].getsockname()[1]

    async def wait(self, timeout: float) -> Tuple[str, str]:
        if self._future is None:
            raise AuthError("Loopback receiver was not started")
        try:
            return await asyncio.wait_for(self._future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AuthError("Timed out waiting for the browser login") from exc

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except Exception:  # pragma: no cover - best effort
            pass

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not request_line or len(request_line) > _MAX_REQUEST_LINE:
                self._respond(writer, 400, "Bad request")
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                self._respond(writer, 400, "Bad request")
                return
            method, target = parts[0], parts[1]
            for _ in range(_MAX_HEADER_LINES):
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break

            parsed = urlsplit(target)
            if method != "GET" or parsed.path != self.path:
                self._respond(writer, 404, "Not found")
                return

            query = parse_qs(parsed.query)
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            error = (query.get("error") or [""])[0]

            if error:
                message = (query.get("error_description") or [error])[0]
                self._fail(AuthError(f"Authorization failed: {message}"))
                self._respond(writer, 400, "Login failed. You can close this tab.")
                return
            if not code or not state:
                self._fail(AuthError("Login callback was missing code or state"))
                self._respond(writer, 400, "Login failed. You can close this tab.")
                return
            if self._future is None or self._future.done():
                self._respond(writer, 409, "Login already completed.")
                return

            self._future.set_result((code, state))
            self._respond(writer, 200, "Login complete. You can close this tab.")
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Loopback callback failed", exc_info=True)
            self._fail(AuthError(f"Login callback failed: {exc}"))
            self._respond(writer, 400, "Login failed. You can close this tab.")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _fail(self, error: Exception) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_exception(error)

    def _respond(
        self, writer: asyncio.StreamWriter, status: int, message: str
    ) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            409: "Conflict",
        }.get(status, "OK")
        body = (
            "<html><body style='font-family:sans-serif;padding:2rem'>"
            f"<h3>{html.escape(message)}</h3></body></html>"
        ).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            writer.write(head + body)
        except Exception:  # pragma: no cover - client may have gone away
            pass


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Login failed ({response.status_code})"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        return str(
            detail.get("message")
            or detail.get("code")
            or f"Login failed ({response.status_code})"
        )
    if detail:
        return str(detail)
    return f"Login failed ({response.status_code})"


async def exchange_handoff(
    settings: Settings,
    payload: dict,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> LoginResult:
    """Redeem the one-time code at the backend handoff endpoint."""

    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, transport=transport
        ) as client:
            response = await client.post(
                f"{settings.api_v1}/auth/handoff", json=payload
            )
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach the backend login endpoint: {exc}") from exc

    if response.status_code >= 400:
        raise AuthError(_error_message(response))

    try:
        data = response.json()
    except ValueError as exc:
        raise AuthError("Login endpoint returned a non-JSON response") from exc

    access = data.get("access_token")
    refresh = data.get("refresh_token")
    device = data.get("device_id")
    if not access or not refresh or not device:
        raise AuthError("Login response was missing tokens")
    return LoginResult(
        access_token=str(access),
        refresh_token=str(refresh),
        device_id=str(device),
        email=str(data.get("email") or ""),
        display_name=str(data.get("display_name") or ""),
    )


def _receiver_target(settings: Settings) -> Tuple[str, int, str, Optional[str]]:
    """Resolve the loopback bind address and the registered redirect URI."""
    configured = (settings.oidc_redirect_uri or "").strip()
    if not configured:
        return "127.0.0.1", 0, CALLBACK_PATH, None
    parts = urlsplit(configured)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        raise AuthError(
            "DESKTOP_OIDC_REDIRECT_URI must be a loopback http://127.0.0.1 URI"
        )
    port = parts.port
    if port is None:
        raise AuthError("DESKTOP_OIDC_REDIRECT_URI must include an explicit port")
    if (parts.path or "/") != CALLBACK_PATH:
        raise AuthError(
            f"DESKTOP_OIDC_REDIRECT_URI must use the {CALLBACK_PATH} path"
        )
    return parts.hostname, port, CALLBACK_PATH, f"http://127.0.0.1:{port}{CALLBACK_PATH}"


async def login(
    settings: Settings,
    *,
    timeout: Optional[float] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    browser_open: Optional[Callable[[str], bool]] = None,
) -> LoginResult:
    """Run the browser login flow and return the platform tokens."""

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(32)
    host, port, path, configured_uri = _receiver_target(settings)

    receiver = LoopbackReceiver(host=host, port=port, path=path)
    await receiver.start()
    try:
        redirect_uri = configured_uri or (
            f"http://127.0.0.1:{receiver.port}{path}"
        )
        portal_url = build_portal_url(
            settings, challenge=challenge, state=state, redirect_uri=redirect_uri
        )
        logger.info("Opening browser login: %s", settings.portal_url)

        opener = browser_open or webbrowser.open
        if not opener(portal_url):
            raise AuthError("Could not open the system browser for login")

        code, returned_state = await receiver.wait(
            timeout or settings.login_timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        )
    finally:
        await receiver.close()

    validate_state(state, returned_state)

    payload = {
        "code": code,
        "state": state,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "device_name": settings.device_name or platform.node() or "desktop",
        "platform": platform_name(),
        "app_version": __version__,
    }
    result = await exchange_handoff(settings, payload, transport=transport)
    logger.info("Browser login completed for %s", result.email or "operator")
    return result
