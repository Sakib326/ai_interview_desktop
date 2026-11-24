import sys
import types

import pytest

from desktop_client.credentials import (
    CredentialStoreUnavailable,
    KeyringTokenStore,
    MemoryTokenStore,
    StoredCredentials,
)


def test_memory_store_round_trip():
    store = MemoryTokenStore()
    assert store.load() is None

    credentials = StoredCredentials(
        refresh_token="refresh", device_id="device", email="a@b.c", display_name="A"
    )
    store.save(credentials)
    assert store.load() == credentials

    store.clear()
    assert store.load() is None


class _FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def test_keyring_store_round_trip(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)

    store = KeyringTokenStore()
    credentials = StoredCredentials(
        refresh_token="refresh", device_id="device", email="a@b.c", display_name="A"
    )
    store.save(credentials)
    assert store.load() == credentials

    store.clear()
    assert store.load() is None


def test_keyring_store_ignores_malformed_entry(monkeypatch):
    fake = _FakeKeyring()
    fake.values[(KeyringTokenStore.SERVICE, KeyringTokenStore.ACCOUNT)] = "not-json"
    monkeypatch.setitem(sys.modules, "keyring", fake)

    assert KeyringTokenStore().load() is None


def test_keyring_store_reports_unavailable(monkeypatch):
    def broken():
        raise RuntimeError("no keyring backend")

    fake = types.SimpleNamespace(
        get_password=lambda service, account: broken(),
        set_password=lambda service, account, value: None,
        delete_password=lambda service, account: None,
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)

    with pytest.raises(CredentialStoreUnavailable):
        KeyringTokenStore().load()
