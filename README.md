# Desktop Client — Live Overlay

PySide6 always-on-top overlay with microphone capture, Windows capture
exclusion (stealth), and a full backend integration: session preflight, the
`/events` and `/audio` WebSockets, streaming SSE answers, debrief, and memory.

## Scope

- **Overlay:** frameless, translucent, always-on-top, non-activating. Pulsing
  recording dot, transcript with speaker labels, live answer pane, stealth
  badge, Stop/Close.
- **Audio:** 50 ms PCM S16LE 16 kHz mono frames (1,600 bytes) from
  `sounddevice.RawInputStream`, streamed as binary WebSocket frames.
- **Events:** `WS /api/v1/sessions/{id}/events` with `bearer.<jwt>`
  subprotocol, 20 s heartbeats, reconnect + `client.resubscribe`, and
  `system.replay_gap` resync through `GET /sessions/{id}/transcript`.
- **Answers:** `POST /api/v1/sessions/{id}/practice/answer` (SSE), rendered
  token by token, finalized on `answer_done`, retry surfaced on
  `answer_failed` when retryable.
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
| `--auto-answer` | Answer after every transcript final |
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
receiving mouse input.

---

## Session flow

```text
GET  /policies/acceptable-use            -> version
POST /sessions/preflight                 -> blockers/warnings
POST /sessions  (kind=live, ack=version) -> active (or adopt the 409 active one)
WS   /sessions/{id}/events               -> initial session.status_changed
WS   /sessions/{id}/audio                -> 1,600-byte binary frames
     client.audio_end                    -> audio.closed | audio.failed
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
# 56 passed, 1 skipped
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

- Transcript event payloads have **no `speaker`**; finals are assumed to be the
  candidate and are relabeled from `GET /sessions/{id}/transcript`. Reliable
  interviewer/candidate separation needs backend source tagging.
- `DESKTOP_AUTO_ANSWER` is off by default for that reason; use F10 to answer
  deliberately.
- `/audio` does not negotiate audio format; 16 kHz S16LE mono is an assumption.
- Stealth is Windows-only and best-effort; macOS/Linux report `UNSUPPORTED`.
  The §10.6 capture matrix must be run on the target machine before ship.
- No packaging/installer yet; see `NEXT_STEPS.md`.
