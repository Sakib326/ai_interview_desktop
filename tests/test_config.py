import base64
import json
import time

import pytest

from desktop_client.config import (
    Settings,
    derive_ws_base_url,
    token_expires_at,
    token_expiring,
)
from desktop_client.main import build_settings


def _jwt(payload: dict) -> str:
    def enc(value):
        return (
            base64.urlsafe_b64encode(json.dumps(value).encode())
            .rstrip(b"=")
            .decode()
        )

    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


def test_token_expiry_helpers():
    future = _jwt({"exp": time.time() + 3600})
    past = _jwt({"exp": time.time() - 10})

    assert token_expires_at(future) > time.time()
    assert token_expiring(future) is False
    assert token_expiring(past) is True
    assert token_expires_at("not-a-jwt") == 0.0
    assert token_expiring("not-a-jwt") is False


def test_derive_ws_base_url():
    assert derive_ws_base_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000"
    assert derive_ws_base_url("https://api.example.com") == "wss://api.example.com"


def test_settings_derives_ws_and_strips_slashes():
    settings = Settings(api_base_url="http://host:9000/", access_token="t")
    assert settings.api_v1 == "http://host:9000/api/v1"
    assert settings.ws_v1 == "ws://host:9000/api/v1"
    assert settings.ws_url("s1", "events") == (
        "ws://host:9000/api/v1/sessions/s1/events"
    )
    assert settings.ws_url("s1", "audio", "source=mic").endswith(
        "/sessions/s1/audio?source=mic"
    )


def test_settings_load_reads_env_file(tmp_path, monkeypatch):
    for key in (
        "DESKTOP_ACCESS_TOKEN",
        "DESKTOP_API_BASE_URL",
        "DESKTOP_GOAL_ID",
        "DESKTOP_AUTO_ANSWER",
        "DESKTOP_SIMULATE_AUDIO",
    ):
        monkeypatch.delenv(key, raising=False)

    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "DESKTOP_ACCESS_TOKEN='abc.def'\n"
        "DESKTOP_API_BASE_URL=https://backend.test\n"
        "DESKTOP_GOAL_ID=goal-1\n"
        "DESKTOP_AUTO_ANSWER=true\n"
        "DESKTOP_SIMULATE_AUDIO=0\n",
        encoding="utf-8",
    )

    settings = Settings.load(env)
    assert settings.access_token == "abc.def"
    assert settings.api_base_url == "https://backend.test"
    assert settings.ws_base_url == "wss://backend.test"
    assert settings.goal_id == "goal-1"
    assert settings.auto_answer is True
    assert settings.simulate_audio is False


def test_env_var_beats_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DESKTOP_ACCESS_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("DESKTOP_ACCESS_TOKEN", "from-env")

    assert Settings.load(env).access_token == "from-env"


def test_build_settings_overrides(monkeypatch):
    monkeypatch.setenv("DESKTOP_ACCESS_TOKEN", "token")

    class Args:
        api_url = "http://localhost:7000"
        session = "sess-9"
        simulate_audio = True
        auto_answer = False
        no_answer = True

    settings = build_settings(Args)
    assert settings.api_base_url == "http://localhost:7000"
    assert settings.ws_base_url == "ws://localhost:7000"
    assert settings.session_id == "sess-9"
    assert settings.simulate_audio is True
    assert settings.auto_answer is False
