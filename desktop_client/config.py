"""Runtime configuration for the desktop client.

Values are read from the process environment first, then from a local ``.env``
file. A ``.env`` file never overrides an already-set environment variable, so
shell-level overrides (and tests) win.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

API_PREFIX = "/api/v1"
AUDIO_SOURCES = frozenset({"mic", "loopback"})

_TRUTHY = {"1", "true", "yes", "on"}


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in _TRUTHY


def derive_ws_base_url(api_base_url: str) -> str:
    """Map an http(s) API base URL onto its ws(s) counterpart."""
    parts = urlsplit(api_base_url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, "", "", ""))


def token_expires_at(token: str) -> float:
    """Read ``exp`` from a JWT without verifying it. Returns 0 when unknown."""
    if not token or token.count(".") != 2:
        return 0.0
    payload = token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
    except (ValueError, json.JSONDecodeError):
        return 0.0
    exp = data.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else 0.0


def token_expiring(token: str, within_seconds: float = 120.0) -> bool:
    """True when the token is known to expire within ``within_seconds``."""
    exp = token_expires_at(token)
    if not exp:
        return False
    return exp - time.time() <= within_seconds


@dataclass
class Settings:
    api_base_url: str = "http://127.0.0.1:8000"
    ws_base_url: str = ""
    access_token: str = ""
    refresh_token: str = ""
    device_id: str = ""
    session_id: str = ""
    goal_id: str = ""
    simulate_audio: bool = False
    heartbeat_seconds: float = 20.0
    auto_answer: bool = False
    audio_source: str = "mic"
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.ws_base_url:
            self.ws_base_url = derive_ws_base_url(self.api_base_url)
        self.api_base_url = self.api_base_url.rstrip("/")
        self.ws_base_url = self.ws_base_url.rstrip("/")

    @property
    def api_v1(self) -> str:
        return f"{self.api_base_url}{API_PREFIX}"

    @property
    def ws_v1(self) -> str:
        return f"{self.ws_base_url}{API_PREFIX}"

    def ws_url(self, session_id: str, channel: str, query: str = "") -> str:
        url = f"{self.ws_v1}/sessions/{session_id}/{channel}"
        return f"{url}?{query}" if query else url

    @property
    def configured(self) -> bool:
        """True when the client has enough to talk to a backend."""
        return bool(self.access_token)

    @classmethod
    def load(cls, env_file: Optional[Path] = None) -> "Settings":
        file_values = _parse_env_file(env_file or Path(".env"))

        def get(name: str, default: str = "") -> str:
            return os.environ.get(name) or file_values.get(name, default)

        access_token = get("DESKTOP_ACCESS_TOKEN")
        if not access_token:
            # The backend auth flow issues access tokens; accept the generic
            # name too so a developer can export either.
            access_token = os.environ.get("ACCESS_TOKEN", "")

        return cls(
            api_base_url=get("DESKTOP_API_BASE_URL", "http://127.0.0.1:8000"),
            ws_base_url=get("DESKTOP_WS_BASE_URL"),
            access_token=access_token,
            refresh_token=get("DESKTOP_REFRESH_TOKEN"),
            device_id=get("DESKTOP_DEVICE_ID"),
            session_id=get("DESKTOP_SESSION_ID"),
            goal_id=get("DESKTOP_GOAL_ID"),
            simulate_audio=_as_bool(get("DESKTOP_SIMULATE_AUDIO")),
            heartbeat_seconds=float(get("DESKTOP_HEARTBEAT_SECONDS", "20")),
            auto_answer=_as_bool(get("DESKTOP_AUTO_ANSWER")),
            audio_source=get("DESKTOP_AUDIO_SOURCE", "mic") or "mic",
            request_timeout_seconds=float(get("DESKTOP_REQUEST_TIMEOUT", "30")),
        )
