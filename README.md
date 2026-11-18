# Desktop Client — Live Overlay

PySide6 always-on-top overlay with microphone capture, Windows capture
exclusion (stealth), and a full backend integration: session preflight, the
`/events` and `/audio` WebSockets, streaming SSE answers, debrief, and memory.

See [`INTEGRATION_STATUS.md`](./INTEGRATION_STATUS.md) for the client ↔ backend
contract audit, live-run evidence, and the remaining server-side gaps.

## Scope

- **Overlay:** frameless, translucent, always-on-top, non-activating. Pulsing
  recording dot, transcript with speaker labels, live answer pane, stealth
  badge, Stop/Close.
- **Audio:** 50 ms PCM S16LE 16 kHz mono frames (1,600 bytes) from
  `sounddevice.RawInputStream`, streamed as binary WebSocket frames. The
  `?source=mic|loopback` tag tells the backend to persist turns as `candidate`
  or `interviewer`.
- **Events:** `WS /api/v1/sessions/{id}/events` with `bearer.<jwt>`
  subprotocol, 20 s heartbeats, reconnect + `client.resubscribe`, and
  `system.replay_gap` resync through `GET /sessions/{id}/transcript`.
  Transcript events carry `speaker`, so labels render without a REST refetch.
- **Answers:** auto-answer uses the gated `POST /sessions/{id}/live/answer`
  (Turn Gate may emit `answer_skipped`); F10 uses the always-answer
  `POST /sessions/{id}/practice/answer`. Both stream SSE, render token by
  token, finalize on `answer_done`, and surface retryable failures.
- **Session end:** `client.audio_end` -> `audio.closed`, `POST /sessions/{id}/end`,
  debrief status polling/fetch/retry, memory summary.
- **Stealth:** best-effort screen-share exclusion via
  `SetWindowDisplayAffinity`. Windows only; not a guarantee. See
  `initialize-plan.md` §10.
- **No TTS.** The overlay is display-only.

---

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Windows
# .venv/bin/pip install -e ".[dev]"        # macOS / Linux
```

Real capture needs PortAudio (`sudo apt install libportaudio2` on
Debian/Ubuntu). Tests do not.

### Build a Windows binary

```powershell
.venv\Scripts\pip install -e ".[build]"
.\packaging\build.ps1              # windowed: dist\overlay\overlay.exe
.\packaging\build.ps1 -Console     # console build for debugging
```

Output is one-folder (`overlay.exe` + `_internal\`), not a single file: faster
startup and fewer AV false positives. The name `overlay` is deliberate
(`initialize-plan.md` §10.4). Ship the whole `dist\overlay` directory and put
`.env` next to `overlay.exe` — the app resolves `.env` from its working
directory. No installer or code signing yet.

---

## Run

Copy `.env.example` to `.env` and set `DESKTOP_ACCESS_TOKEN` for live mode.

```bash
python -m desktop_client.main
```

Without a token the client runs in **capture-only mode**: it proves the
overlay and the 50 ms framing without any network calls.

Headless/device-less verification uses synthetic frames:

```bash
python -m desktop_client.main --simulate-audio
# prints: captured 1600 bytes
```

### CLI flags

| Flag | Effect |
|---|---|
| `--simulate-audio` | Emit silent frames instead of opening a device |
| `--session <id>` | Reuse an existing session (acks it if pending) |
| `--api-url <url>` | Override the backend origin (ws origin is derived) |
| `--auto-answer` | Answer interviewer finals through the gated live endpoint |
| `--no-answer` | Never auto-answer (F10 still works) |
| `--no-tray` | Disable the tray icon |
| `--no-hotkeys` | Disable Windows global hotkeys |

### Hotkeys

Global (Windows `RegisterHotKey`, work while another app has focus):

- **F8** — toggle click-through
- **F9** — toggle stealth exclusion
- **F10** — answer the latest transcript turn

The tray menu mirrors these plus show/hide, Stop, and Quit. The tray exists
because click-through mode cannot be clicked out of: the overlay itself stops
receiving mouse input. It also carries **Forget all memory…** (confirmed) and
**Retry memory extraction** when a backend is configured.

### Tokens

Set `DESKTOP_REFRESH_TOKEN` and `DESKTOP_DEVICE_ID` alongside the access token
to survive the 60-minute access-token lifetime: the client refreshes ahead of
expiry, on HTTP 401, and once on a `4401` WebSocket rejection. Without them a
long session ends at the first expiry.

---

## Session flow

```text
GET  /policies/acceptable-use            -> version
POST /sessions/preflight                 -> blockers/warnings
POST /sessions  (kind=live, ack=version) -> active (or adopt the 409 active one)
WS   /sessions/{id}/events               -> initial session.status_changed
WS   /sessions/{id}/audio?source=mic     -> 1,600-byte binary frames
     client.audio_end                    -> audio.closed | audio.failed
POST /sessions/{id}/live/answer          -> SSE tokens | answer_skipped (gated)
POST /sessions/{id}/end                  -> debrief enqueued
GET  /sessions/{id}/debrief/status       -> poll until not `writing`
GET  /sessions/{id}/debrief              -> 200 ready / 202 writing / 404 none
GET  /memory                             -> summary count
```

Reconnects resubscribe from the last sequence number; a `system.replay_gap`
triggers a transcript refetch. `client.ack` is intentionally unused — the
backend gives it no sequencing effect.

---

## Package architecture

```text
desktop_client/
  __init__.py     # version
  config.py       # env/.env settings, ws origin derivation
  api.py          # REST + SSE client (httpx), structured ApiError
  events.py       # /events WebSocket: heartbeat, reconnect, resubscribe
  audio.py        # sounddevice capture -> 1,600-byte frames
  audio_ws.py     # /audio WebSocket sender + finalization wait
  transcript.py   # dedup, ordering, speaker merge
  session.py      # lifecycle orchestration
  overlay.py      # QWidget overlay + answer pane
  stealth.py      # Windows display affinity
  hotkeys.py      # global hotkeys + tray
  main.py         # qasync entry point
tests/
  conftest.py             # offscreen QApplication + local WS server
  test_desktop_client.py  # overlay, audio framing, stealth (19)
  test_config.py          # settings/derivation
  test_transcript.py      # dedup, ordering, speaker merge
  test_api.py             # REST/SSE parsing and error mapping
  test_events.py          # live socket: heartbeat, reconnect, replay gap
  test_audio_ws.py        # frames, audio_end, closed/failed/timeout
  test_session.py         # events -> UI, answers, end/debrief/memory
```

---

## Tests

```bash
QT_QPA_PLATFORM=offscreen pytest -q
# 75 passed, 1 skipped
```

The socket tests start real local WebSocket servers; the HTTP tests use
`httpx.MockTransport`. No backend, no API keys, no audio device required.

### Live smoke test

`tests/test_live_smoke.py` drives the real controller against a running
backend (preflight → session → events → audio → stop → end → debrief → memory).
It is skipped unless both variables are set:

```bash
DESKTOP_LIVE_API=http://127.0.0.1:8000 \
DESKTOP_LIVE_TOKEN=<jwt> \
QT_QPA_PLATFORM=offscreen pytest -q -s tests/test_live_smoke.py
```

Get a token locally with the backend's own tool:
`python tools/generate_dev_token.py` from the `ai_interview` repo. Without
provider keys the audio socket reports `stt_provider_unavailable`; the test
asserts the lifecycle and error surfacing rather than transcript text.

---

## Known limitations

- **System/loopback audio capture** (Windows, WASAPI) is implemented. Set
  `DESKTOP_AUDIO_SOURCE=both` to capture microphone and system/output audio
  simultaneously (recommended), or `loopback` for system audio only, or `mic`
  for microphone only. Turns are tagged `candidate` (mic) / `interviewer`
  (loopback). Use headphones to avoid feedback/leakage.
- Auto-answer only fires for `interviewer` finals (gated endpoint). With
  `DESKTOP_AUDIO_SOURCE=mic` there are no interviewer turns, so use F10 or run
  in `loopback`/`both` mode with interviewer audio playing through system output.
- `/audio` does not negotiate audio format; 16 kHz S16LE mono is an assumption.
- No interactive OIDC login yet: the access token (and optional refresh
  credentials) must be supplied via the environment.
- Stealth is Windows-only and best-effort; macOS/Linux report `UNSUPPORTED`.
  The §10.6 capture matrix must be run on the target machine before ship.
- No packaging/installer yet; see `NEXT_STEPS.md`.
