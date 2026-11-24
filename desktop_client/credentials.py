"""OS-protected credential storage for the desktop client.

Spec §11.1.7 requires the rotating refresh token and ``device_id`` to live in
Windows Credential Manager (or an equivalent OS facility); the access token
stays in memory. There is no plaintext-file fallback — if the OS store is not
available the client still logs in for the current run but cannot persist.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class CredentialStoreUnavailable(RuntimeError):
    """The OS credential store is not available on this machine."""


@dataclass(frozen=True)
class StoredCredentials:
    refresh_token: str
    device_id: str
    email: str = ""
    display_name: str = ""


class TokenStore(Protocol):
    def load(self) -> Optional[StoredCredentials]:
        ...

    def save(self, credentials: StoredCredentials) -> None:
        ...

    def clear(self) -> None:
        ...


class MemoryTokenStore:
    """In-memory store for tests and for machines without an OS keyring."""

    def __init__(self, credentials: Optional[StoredCredentials] = None):
        self._credentials = credentials

    def load(self) -> Optional[StoredCredentials]:
        return self._credentials

    def save(self, credentials: StoredCredentials) -> None:
        self._credentials = credentials

    def clear(self) -> None:
        self._credentials = None


class KeyringTokenStore:
    """Windows Credential Manager (or the platform equivalent) via ``keyring``."""

    SERVICE = "ai-interview-desktop"
    ACCOUNT = "refresh-token"

    def __init__(self, service: str = SERVICE):
        self.service = service

    def _keyring(self):
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise CredentialStoreUnavailable(
                "keyring is required for OS-protected credential storage"
            ) from exc
        return keyring

    def load(self) -> Optional[StoredCredentials]:
        try:
            raw = self._keyring().get_password(self.service, self.ACCOUNT)
        except CredentialStoreUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - backend-specific failures
            raise CredentialStoreUnavailable(str(exc)) from exc
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("Ignoring malformed stored credentials")
            return None
        refresh = str(data.get("refresh_token") or "")
        device = str(data.get("device_id") or "")
        if not refresh or not device:
            return None
        return StoredCredentials(
            refresh_token=refresh,
            device_id=device,
            email=str(data.get("email") or ""),
            display_name=str(data.get("display_name") or ""),
        )

    def save(self, credentials: StoredCredentials) -> None:
        payload = json.dumps(
            {
                "refresh_token": credentials.refresh_token,
                "device_id": credentials.device_id,
                "email": credentials.email,
                "display_name": credentials.display_name,
            },
            separators=(",", ":"),
        )
        try:
            self._keyring().set_password(self.service, self.ACCOUNT, payload)
        except CredentialStoreUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - backend-specific failures
            raise CredentialStoreUnavailable(str(exc)) from exc

    def clear(self) -> None:
        try:
            self._keyring().delete_password(self.service, self.ACCOUNT)
        except Exception:
            # Deleting a missing entry is fine; a broken backend must not crash
            # shutdown.
            pass
