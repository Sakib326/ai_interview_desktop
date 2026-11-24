# Change record — desktop interactive login

**Date:** 2026-11-24
**Design:** `docs/desktop-auth-handoff.md`
**Backend record:** `E:\ai_interview\docs\desktop-auth-handoff-changes.md`
**Status:** implemented; desktop suite green (`115 passed, 1 skipped`).

## What changed

| File | Change |
|---|---|
| `desktop_client/auth.py` (new) | PKCE generation, portal URL builder, one-shot loopback HTTP receiver, `state` validation, and `POST /api/v1/auth/handoff` exchange. No IdP calls and no portal-cookie access. |
| `desktop_client/credentials.py` (new) | `TokenStore` protocol, in-memory test double, and `KeyringTokenStore` (Windows Credential Manager via `keyring`). Stores `refresh_token` + `device_id`; the access token stays in memory. |
| `desktop_client/config.py` | New settings: `auth_portal_url`, `oidc_redirect_uri`, `oauth_scope`, `login_enabled`, `login_timeout_seconds`, `device_name`, and a `portal_url` property (defaults to `<api_base_url>/web/desktop-login`). |
| `desktop_client/api.py` | Optional `on_tokens_rotated` callback so a rotated refresh token is persisted immediately. |
| `desktop_client/main.py` | `ensure_authenticated()` ordering (env token → stored refresh → browser login), `_build_store()`, `--no-login`, persistence wiring, and "signing in" overlay status. |
| `pyproject.toml` | Added `keyring>=24`. |
| `.env.example` | Documented the login settings. |
| `tests/test_auth.py` (new) | RFC 7636 PKCE vector, portal URL, loopback round trip/404/timeout, handoff success/error, full login flow with a fake browser, state mismatch, redirect-URI guard. |
| `tests/test_credentials.py` (new) | Memory and keyring stores, malformed entry, unavailable backend. |
| `tests/test_config.py` | Portal URL, login env settings, `--no-login`. |
| `tests/test_desktop_client.py` | Made the loopback/WASAPI test deterministic by blocking the SoundCard path (the machine has a default playback endpoint, so the old test skipped its fake device). |

## Behavior notes

- A valid `DESKTOP_ACCESS_TOKEN` (shell or `.env`) is used as-is and no browser
  opens. An **expired** environment token is discarded instead of pinning the
  client to live mode: the client then tries refresh credentials and finally the
  browser login. This fixed the 401 `Token has expired` loop on startup.
- Without a token: load the OS credential store, try `/auth/refresh`, and fall
  back to the browser flow. On login failure the client still starts in
  capture-only mode and shows the error in the overlay, rather than hanging.
- `--no-login` (or `DESKTOP_LOGIN_ENABLED=0`) forces capture-only when no token
  is configured.
- `DESKTOP_OIDC_REDIRECT_URI` must be
  `http://127.0.0.1:<port>/callback` when set; the client binds that exact port
  and rejects any other path. When unset it binds an ephemeral port and uses
  `http://127.0.0.1:<port>/callback`. The handoff path is independent of the
  backend's native `OIDC_REDIRECT_URI`, which may point at a backend route.
- The loopback receiver accepts one `GET` on the configured path, bounds the
  request line and header count, closes after the callback, and rejects a
  missing/duplicate callback.
- Rotated refresh tokens are persisted from `ApiClient.refresh_token()`, so a
  restart reuses the session instead of forcing a new login.
- No plaintext token file is written. If `keyring` has no usable backend the
  client logs a warning, still logs in, and does not persist.

## Verification

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest -q
# 115 passed, 1 skipped
```

`keyring` was installed into the local venv to exercise the declared dependency.
The skipped test is the opt-in live smoke test.

## Migration / rollout

1. The backend must expose `GET /web/desktop-login` and
   `POST /api/v1/auth/handoff` (see the backend change record) and the IdP must
   allow the loopback redirect.
2. Install the new dependency: `pip install -e ".[dev]"` (picks up
   `keyring>=24`). The PyInstaller build needs `keyring` and its backends
   collected; verify the built `overlay.exe` can read/write the credential after
   a login.
3. No change is required for headless/CI runs: they can keep supplying
   `DESKTOP_ACCESS_TOKEN`, or pass `--no-login`.

## Known limits

- Windows Credential Manager is the primary target; macOS/Linux rely on whatever
  `keyring` backend is available.
- The portal handoff store is process-local on the backend; a backend restart
  between the redirect and redemption forces a new login.
- The web portal session itself still cannot refresh (backend-owned; see the
  backend record).
