# PySide6 Desktop Client — Full Implementation Guide

Covers the skeleton (overlay, capture, stealth) and the backend integration
built on top of it: `/events`, `/audio`, live answers, debrief, memory, global
hotkeys, and the tray.

---

## 1. Executive Summary & Design Principles

### Scope and boundaries
- **UI:** desktop overlay for a Live session. Display-only; no TTS, no audio
  injection.
- **Transport:** the canonical product path — `WS /sessions/{id}/events` +
  `WS /sessions/{id}/audio` — not `/stt`.
- **Platform focus:** Windows for capture exclusion. macOS/Linux render and
  capture normally but report `UNSUPPORTED` stealth.
- **Backend:** read-only dependency. No server changes were made; two API gaps
  are handled client-side and documented (§6).

### Three orthogonal axes
| Axis | Mechanism | Effect |
|---|---|---|
| Capture visibility (stealth) | `SetWindowDisplayAffinity` | Window omitted from shares/recordings |
| Input transparency (click-through) | `WindowTransparentForInput` | Clicks pass to apps underneath |
| Keyboard reachability | `RegisterHotKey` + tray | Control the overlay without focus |

They are never merged: stealth is not implemented with
`WA_TransparentForMouseEvents`, and click-through never disables stealth.

---

## 2. Package Architecture

```text
desktop_client/
├── __init__.py     # version
├── config.py       # Settings: env then .env, ws origin derivation
├── api.py          # httpx REST + SSE client, ApiError, SSE frame parser
├── events.py       # /events socket: dispatch, heartbeat, reconnect, resubscribe
├── audio.py        # sounddevice RawInputStream -> 1,600-byte frames
├── audio_ws.py     # /audio socket: binary frames, audio_end, closed/failed
├── transcript.py   # dedup by turn/segment, ordering, authoritative merge
├── session.py      # LiveSessionController: ordered lifecycle
├── overlay.py      # QWidget overlay, transcript + answer panes
├── stealth.py      # Windows display affinity wrapper
├── hotkeys.py      # global hotkeys + tray menu
└── main.py         # qasync entry point, CLI, offline fallback
```

Dependency direction is one-way: `main -> session -> {api, events, audio_ws,
transcript}`, with `overlay` a passive view. `audio` and `stealth` stay leaf
modules.

---

## 3. Component Breakdown

### 3.1 Configuration (`config.py`)
- Precedence: process environment, then `.env`. A `.env` file never overrides a
  set env var.
- `api_base_url` -> `api_v1`; `ws_base_url` is derived when absent
  (`http`→`ws`, `https`→`wss`).
- Booleans accept `1/true/yes/on`.
- `configured` (token present) selects live vs capture-only mode.

### 3.2 REST + SSE client (`api.py`)
- `ApiClient` wraps only the endpoints the overlay needs. Base URL is
  `{api_base_url}/api/v1`.
- `ApiError(status_code, code, message, retryable, detail)` is the single error
  shape. `detail` objects and strings from FastAPI are both flattened; 401 is
  never retryable, 429/5xx are.
- `parse_sse_stream` is a spec-tolerant parser: `event:`/`data:` fields, multi
  line data, comment lines (`:`), and malformed JSON frames are dropped with a
  warning instead of raising.
- `answer()` streams with `httpx.AsyncClient.stream` and a stable
  `Idempotency-Key: answer-<question_turn_id>`, so a retry replays the recorded
  answer rather than regenerating it.
- `end_session` uses `end-<session_id>` for the same reason.

### 3.3 Events socket (`events.py`)
- URL: `{ws}/api/v1/sessions/{id}/events`, authenticated with
  `Sec-WebSocket-Protocol: bearer.<jwt>` (the backend selects the subprotocol).
- Rejects `?token=` implicitly by never using it; the server closes `4401`.
- The initial `session.status_changed` is consumed before any transcript event.
- Heartbeat every 20 s by default (configurable, clamped to 1–30 s). The server
  closes `1000` after 65 s of client silence.
- Reconnect loop with exponential backoff (`1s` → `30s`, resettable for tests).
- On reconnect, `client.resubscribe` with `last_known_sequence` — never
  `client.ack`, which the backend treats as a metric only.
- `system.replay_gap` is routed to a dedicated handler instead of the normal
  event path; the controller refetches the transcript.
- Close codes `4401/4403/4404` raise `EventStreamAuthError`, and `run()`
  returns instead of retrying. Handshake 401/403 behaves the same way.
- Sends are serialized behind a lock because heartbeats and resubscribe can
  race.

### 3.4 Audio capture (`audio.py`)
- `sounddevice.RawInputStream(samplerate=16000, channels=1, dtype="int16",
  blocksize=800)`; 800 samples = 50 ms = 1,600 bytes.
- PortAudio's realtime callback never touches asyncio directly: it schedules
  `queue.get_nowait()`/`put_nowait()` via `loop.call_soon_threadsafe`.
- Bounded queue (40 frames ≈ 2 s); on overflow the oldest frame is dropped with
  a warning.
- **Graceful stop drains the queue.** On `stop_event`, frames captured but not
  yet consumed are moved into the tail buffer before the stream closes, so the
  words spoken just before Stop are not silently lost. The residual partial
  frame is zero-padded to 1,600 bytes.
- Nothing is yielded from `finally`, so `aclose()`/cancellation cannot raise
  "async generator ignored GeneratorExit".

### 3.5 Audio socket (`audio_ws.py`)
- Sends raw bytes as binary frames; one 50 ms frame per message (within
  AssemblyAI's 20–1,000 ms range, and the backend does no padding).
- Appends `?source=mic|loopback` from config. The backend validates it and maps
  the value to the persisted `Turn.speaker` (`candidate` / `interviewer`); an
  unknown value is rejected with `audio.failed` + close `1008`, so the client
  validates the setting before connecting.
- Sends `{"type":"client.audio_end"}` after the last frame, then awaits
  `audio.closed` / `audio.failed` with a 12 s timeout.
- Returns `AudioOutcome(status, error_class, retryable, message)` with
  `closed | failed | disconnected | timeout` so the controller can distinguish
  a provider failure from a dropped socket.

### 3.6 Transcript state (`transcript.py`)
- Finals are immutable turns, deduplicated by `turn_id` **and** `segment_id`
  (a reconnect can replay both).
- Deltas are keyed by `segment_id` and replace the text for that segment;
  a final clears the matching partial.
- Turns are kept sorted by `sequence_number` even if a replay arrives late.
- `speaker` comes from the event payload (`microphone` capture is `candidate`,
  `loopback` is `interviewer`).
- `merge_authoritative(transcript)` merges `GET /transcript` rather than
  replacing: the REST list can lag the event stream, and replacing with an
  empty read would erase just-rendered finals. It is now only a fallback for
  backends that omit `speaker`, plus the replay-gap resync path.

### 3.7 Overlay (`overlay.py`)
- Flags: `FramelessWindowHint | WindowStaysOnTopHint | Tool |
  WindowDoesNotAcceptFocus`; attributes `WA_TranslucentBackground`,
  `WA_ShowWithoutActivating`; neutral title `"Overlay"`.
- Rounded-rect `paintEvent`; alpha rises to 250 when stealth degrades to
  `BLACK_BOX` so the captured rectangle looks intentional.
- Panels: status row (pulse dot, status, stealth badge, model), transcript
  `QTextEdit`, a separate live-answer pane, a partial `QLabel`, and controls.
- Answer API: `set_answer_loading`, `append_answer_token`, `set_answer(text,
  title)`, `set_answer_error(message, retryable)`, `clear_answer`.
- Transcript API: `set_turns([(label, text)])`; speaker labels come from the
  store (`Interviewer` / `You` / `Assistant`).
- The partial lives in its own label so deltas never reflow the transcript.

### 3.8 Stealth (`stealth.py`)
- States: `EXCLUDED` (0x11), `BLACK_BOX` (0x01 fallback), `UNSUPPORTED`
  (non-Windows), `FAILED`.
- Build-gated on 19041+; `0x11` then `0x01`; both failing is `FAILED`, not an
  exception.
- The HWND is read at call time and never cached — Qt destroys and recreates it
  on `setWindowFlag`.
- Re-applied in `showEvent` and via `QTimer.singleShot(0, ...)` after every flag
  change (including click-through toggles).
- `clear()` sets `WDA_NONE` and is idempotent.

### 3.9 Hotkeys and tray (`hotkeys.py`)
- `RegisterHotKey` on the overlay HWND with `MOD_NOREPEAT`; WM_HOTKEY is
  dispatched by a `QAbstractNativeEventFilter` reading the native `MSG`.
- Registered: F8 click-through, F9 stealth, F10 answer latest.
- Unregistered on shutdown; registration failure logs and degrades to the
  existing (unreliable) `QShortcut`s.
- Tray icon is painted at runtime (no asset files) and exposes show/hide,
  click-through, stealth, Stop, and Quit. This is the only escape from
  click-through mode, which blocks all mouse input to the overlay.

### 3.10 Session controller (`session.py`)
Ordered lifecycle:

1. `GET /policies/acceptable-use` -> `version`.
2. If `DESKTOP_SESSION_ID` is set: fetch it; acknowledge it if `pending`.
   Otherwise `POST /sessions/preflight`; blockers abort with their messages.
3. `POST /sessions` with `kind="live"` and the acknowledged version. A `409`
   means a session already exists, so the active one is adopted and acked.
4. Start `/events`, wait for `connected` (≤15 s), then start `/audio`.
5. Render deltas/finals; `speaker` comes from the event, so no per-final
   transcript refetch (kept only as a fallback when `speaker` is absent).
6. Answers: auto-answer only for `interviewer` finals via the gated
   `POST /live/answer` (skip clears the pane); F10 uses the ungated
   `POST /practice/answer`. One stream per question turn; a new question
   cancels the previous stream.
7. Stop: `client.audio_end` -> wait for `audio.closed` (25 s cap) -> end the
   session -> poll debrief -> fetch -> memory summary.
8. Shutdown ends the session (unless it is a reused `DESKTOP_SESSION_ID`),
   cancels all tasks, and closes the HTTP client.
9. Token refresh runs on a 30 s maintenance task, plus on HTTP 401 and once on
   a 4401 WebSocket rejection. The refresh token rotates and is kept in memory.
10. A retryable `audio.failed` or a dropped audio socket restarts the stream
    once; a user Stop never retries.
11. Tray actions cover erase-all memory (confirmed) and extraction retry.

Long operations (transcript refresh, answers, debrief) run as tasks so the
events reader is never blocked.

---

## 4. Verification

### 4.1 Automated tests (`tests/`, 56 tests)

| Area | Coverage |
|---|---|
| Overlay / audio / stealth (19) | Flags, badges, click-through, frame size, tail padding, `aclose`, affinity mocks |
| Config | env vs `.env` precedence, ws derivation, CLI overrides |
| Transcript | delta replace, dedup by turn+segment, ordering, authoritative merge, relabel |
| API/SSE | token/done parsing, idempotency header, structured 503/404 errors, memory, malformed frames |
| Events socket | initial status + delta + final, subprotocol header, heartbeat, reconnect+resubscribe, 4401 stop, replay gap |
| Audio socket | frame forwarding, `client.audio_end`, `audio.closed`, `audio.failed`, disconnect, finalize timeout |
| Controller | transcript rendering, dedup, capture status, replay-gap resync, answer success/failure+retry, end→debrief→memory, generate_failed retry, session adoption/creation/preflight, preflight model/warnings, audio restart, end-on-quit, memory actions |
| Live smoke (opt-in) | real backend round trip: preflight → create → events → audio → stop → end → debrief → memory; plus `stub_stt` runs for speaker labels and the gated answer |

```bash
QT_QPA_PLATFORM=offscreen pytest -q
# 75 passed, 1 skipped
```

Socket tests use real local `websockets` servers; HTTP tests use
`httpx.MockTransport`. No backend, keys, or audio hardware required.

The skipped test is `tests/test_live_smoke.py`; set `DESKTOP_LIVE_API` and
`DESKTOP_LIVE_TOKEN` to run it against a real backend. Verified against the
`ai_interview` server on a disposable migrated DB in three modes:

1. No provider keys: lifecycle + `stt_provider_unavailable` + `no_content`.
2. `mic` + `stub_stt`: final carries `speaker=candidate`, rendered as `You: …`
   with no transcript refetch.
3. `loopback` + gated auto-answer: `Interviewer: …`, `/live/answer` runs the
   Turn Gate, and the backend persists an `assistant` turn plus
   `session.answer_generated`.

### 4.2 Entry point
```bash
python -m desktop_client.main --simulate-audio
# captured 1600 bytes
```

---

## 5. Client-side handling of backend gaps

| Gap | Client behavior |
|---|---|
| No audio-format negotiation | Assumes 16 kHz S16LE mono 50 ms; documented as an assumption |
| No `speaker` (older backends) | Falls back to assuming candidate and refetching `GET /transcript` after a final; current backends send `speaker` and skip the refetch |
| Turn Gate absent (older backends) | Auto-answer uses `live/answer`; if a deployment lacks it, the request fails and is surfaced as an error |
| No loopback capture | `DESKTOP_AUDIO_SOURCE` tags the stream, but only microphone audio is captured |

---

## 6. What is intentionally not here

- No packaging/installer (PyInstaller/uv decision still open).
- No TTS.
- No loopback/system-audio capture — mic only.
- No stealth verification record; the §10.6 matrix needs the target hardware.
