import asyncio
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from desktop_client.auth import (
    AuthError,
    LoopbackReceiver,
    build_portal_url,
    exchange_handoff,
    generate_pkce,
    login,
    pkce_challenge,
    validate_state,
)
from desktop_client.config import Settings


def _jwt(payload: dict) -> str:
    def enc(value):
        return (
            base64.urlsafe_b64encode(json.dumps(value).encode())
            .rstrip(b"=")
            .decode()
        )

    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


def _settings(**overrides):
    values = {
        "api_base_url": "http://backend.test",
        "access_token": "",
        "login_timeout_seconds": 5.0,
        "device_name": "test-machine",
    }
    values.update(overrides)
    return Settings(**values)


def test_pkce_matches_rfc7636_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_generate_pkce_is_valid():
    verifier, challenge = generate_pkce()
    assert 43 <= len(verifier) <= 128
    assert pkce_challenge(verifier) == challenge


def test_build_portal_url_defaults_to_backend():
    settings = _settings()
    url = build_portal_url(
        settings,
        challenge="challenge",
        state="state",
        redirect_uri="http://127.0.0.1:1234/callback",
    )
    assert url.startswith("http://backend.test/web/desktop-login?")
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge"] == ["challenge"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state"]
    assert query["redirect_uri"] == ["http://127.0.0.1:1234/callback"]
    assert query["scope"] == ["openid profile email"]


def test_build_portal_url_honors_explicit_portal():
    settings = _settings(auth_portal_url="https://portal.example/desk")
    url = build_portal_url(
        settings,
        challenge="c",
        state="s",
        redirect_uri="http://127.0.0.1:1/callback",
    )
    assert url.startswith("https://portal.example/desk?")


def test_validate_state():
    validate_state("same", "same")
    with pytest.raises(AuthError):
        validate_state("expected", "different")


@pytest.mark.asyncio
async def test_loopback_receiver_round_trip():
    receiver = LoopbackReceiver(port=0)
    await receiver.start()
    try:

        async def hit():
            await asyncio.sleep(0)
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", receiver.port
            )
            writer.write(
                b"GET /callback?code=abc&state=xyz HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n"
            )
            await writer.drain()
            await reader.read()
            writer.close()

        task = asyncio.create_task(hit())
        code, state = await receiver.wait(timeout=5)
        assert (code, state) == ("abc", "xyz")
        await task
    finally:
        await receiver.close()


@pytest.mark.asyncio
async def test_loopback_receiver_rejects_wrong_path():
    receiver = LoopbackReceiver(port=0)
    await receiver.start()
    try:

        async def hit():
            await asyncio.sleep(0)
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", receiver.port
            )
            writer.write(b"GET /other?code=abc&state=xyz HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            writer.close()
            return response

        task = asyncio.create_task(hit())
        with pytest.raises(AuthError):
            await receiver.wait(timeout=0.3)
        response = await task
        assert b"404" in response
    finally:
        await receiver.close()


@pytest.mark.asyncio
async def test_loopback_receiver_times_out():
    receiver = LoopbackReceiver(port=0)
    await receiver.start()
    try:
        with pytest.raises(AuthError):
            await receiver.wait(timeout=0.1)
    finally:
        await receiver.close()


@pytest.mark.asyncio
async def test_exchange_handoff_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "device_id": "device",
                "email": "operator@example.com",
                "display_name": "Operator",
            },
        )

    payload = {
        "code": "one-time",
        "state": "state",
        "code_verifier": "a" * 43,
        "redirect_uri": "http://127.0.0.1:1234/callback",
        "device_name": "test-machine",
        "platform": "windows",
        "app_version": "0.1.0",
    }
    result = await exchange_handoff(
        _settings(), payload, transport=httpx.MockTransport(handler)
    )
    assert captured["path"] == "/api/v1/auth/handoff"
    assert captured["body"] == payload
    assert result.access_token == "access"
    assert result.refresh_token == "refresh"
    assert result.device_id == "device"
    assert result.email == "operator@example.com"


@pytest.mark.asyncio
async def test_exchange_handoff_error_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "invalid_handoff_code"})

    with pytest.raises(AuthError) as excinfo:
        await exchange_handoff(
            _settings(),
            {
                "code": "bad",
                "state": "s",
                "code_verifier": "a" * 43,
                "redirect_uri": "http://127.0.0.1:1234/callback",
                "device_name": "test",
                "platform": "windows",
                "app_version": "0.1.0",
            },
            transport=httpx.MockTransport(handler),
        )
    assert "invalid_handoff_code" in str(excinfo.value)


@pytest.mark.asyncio
async def test_login_full_flow_with_fake_browser():
    settings = _settings()
    captured = {}
    portal = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "device_id": "device",
            },
        )

    def opener(url: str) -> bool:
        query = parse_qs(urlsplit(url).query)
        portal["challenge"] = query["code_challenge"][0]
        portal["state"] = query["state"][0]
        redirect_uri = query["redirect_uri"][0]
        state = portal["state"]

        async def hit():
            await asyncio.sleep(0)
            parts = urlsplit(redirect_uri)
            reader, writer = await asyncio.open_connection(
                parts.hostname, parts.port
            )
            writer.write(
                f"GET {parts.path}?code=one-time&state={state} HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n\r\n".encode()
            )
            await writer.drain()
            await reader.read()
            writer.close()

        asyncio.get_running_loop().create_task(hit())
        return True

    result = await login(
        settings,
        transport=httpx.MockTransport(handler),
        browser_open=opener,
    )

    assert result.access_token == "access"
    assert captured["path"] == "/api/v1/auth/handoff"
    body = captured["body"]
    assert body["code"] == "one-time"
    assert body["state"] == portal["state"]
    assert body["device_name"] == "test-machine"
    assert body["platform"] in {"windows", "macos", "linux"}
    assert body["redirect_uri"].startswith("http://127.0.0.1:")
    assert body["redirect_uri"].endswith("/callback")
    assert pkce_challenge(body["code_verifier"]) == portal["challenge"]


@pytest.mark.asyncio
async def test_login_aborts_on_state_mismatch():
    settings = _settings()

    def opener(url: str) -> bool:
        redirect_uri = parse_qs(urlsplit(url).query)["redirect_uri"][0]

        async def hit():
            await asyncio.sleep(0)
            parts = urlsplit(redirect_uri)
            reader, writer = await asyncio.open_connection(
                parts.hostname, parts.port
            )
            writer.write(
                f"GET {parts.path}?code=one-time&state=wrong HTTP/1.1\r\n\r\n".encode()
            )
            await writer.drain()
            await reader.read()
            writer.close()

        asyncio.get_running_loop().create_task(hit())
        return True

    with pytest.raises(AuthError):
        await login(settings, browser_open=opener)


@pytest.mark.asyncio
async def test_login_requires_explicit_loopback_uri():
    settings = _settings(oidc_redirect_uri="https://evil.example/callback")
    with pytest.raises(AuthError):
        await login(settings, browser_open=lambda url: True)


@pytest.mark.asyncio
async def test_login_requires_callback_path():
    settings = _settings(
        oidc_redirect_uri="http://127.0.0.1:8765/api/v1/auth/callback"
    )
    with pytest.raises(AuthError):
        await login(settings, browser_open=lambda url: True)


@pytest.mark.asyncio
async def test_ensure_authenticated_skips_browser_for_simulated_audio():
    from desktop_client.credentials import MemoryTokenStore
    from desktop_client.main import ensure_authenticated

    settings = _settings(simulate_audio=True)
    assert await ensure_authenticated(settings, MemoryTokenStore()) == ""
    assert settings.access_token == ""


@pytest.mark.asyncio
async def test_ensure_authenticated_replaces_expired_env_token(monkeypatch):
    from desktop_client.auth import LoginResult
    from desktop_client.credentials import MemoryTokenStore
    import desktop_client.main as main_mod

    settings = _settings(access_token=_jwt({"exp": time.time() - 60}))

    async def fake_login(settings):
        return LoginResult(
            access_token="new-access",
            refresh_token="new-refresh",
            device_id="new-device",
        )

    monkeypatch.setattr(main_mod, "login", fake_login)
    assert (
        await main_mod.ensure_authenticated(settings, MemoryTokenStore()) == ""
    )
    assert settings.access_token == "new-access"
    assert settings.refresh_token == "new-refresh"
    assert settings.device_id == "new-device"


@pytest.mark.asyncio
async def test_ensure_authenticated_keeps_valid_env_token():
    from desktop_client.credentials import MemoryTokenStore
    from desktop_client.main import ensure_authenticated

    token = _jwt({"exp": time.time() + 600})
    settings = _settings(access_token=token)
    assert await ensure_authenticated(settings, MemoryTokenStore()) == ""
    assert settings.access_token == token
