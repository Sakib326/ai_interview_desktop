# Next Steps — Desktop Client

**Current state:** the PySide6 skeleton is implemented in this repository.
It has a translucent always-on-top overlay, 50 ms PCM audio capture, Windows
capture exclusion (best effort), graceful Stop, and Close-to-exit.

**Verified here:** `19 passed` with `QT_QPA_PLATFORM=offscreen pytest -q`.
The offscreen entrypoint prints `captured 1600 bytes` with `--simulate-audio`.

**Not implemented yet:** any backend WebSocket/API connection. The skeleton does
not create sessions, stream audio, render backend transcripts, call the answer
SSE endpoint, or show debrief/memory data.

---

## 0. Repository hygiene (do first)

- Commit the current desktop changes.
- Decide whether this project stays a separate repo or moves into the main
  `ai-interview` repo. The approved plan lives at
  `ai-interview/docs/desktop-client-plan.md`.
- Track `IMPLEMENTATION.md` and `initialize-plan.md` (currently untracked), or
  move them under a tracked `docs/` directory.
- Existing `__pycache__` files are tracked from the initial commit. `.gitignore`
  now covers them, but they must be untracked once:

  ```bash
  git rm -r --cached desktop_client/__pycache__ tests/__pycache__
  ```

- Keep `.env` local. `.env.example` is versioned; it documents the future
  backend settings and is not read by the skeleton yet.

---

## 1. Windows stealth verification (pre-ship gate)

Stealth is authorized by `initialize-plan.md` §10 and is Windows-only. It is
best-effort, not a guarantee.

- Run the §10.6 matrix on the target Windows machine:
  - Teams screen share (window + full screen)
  - Zoom
  - Google Meet in Chrome
  - OBS: WGC and BitBlt display capture
  - Snipping Tool (Win11 24H2+)
  - Xbox Game Bar
  - Discord
- Verify the badge matches actual behavior in every cell.
- Verify exclusion survives:
  - a click-through toggle round-trip,
  - hide/show,
  - a session restart.
- Add an OS-level hotkey/tray for F8/F9. The current `QShortcut` may never fire
  because the overlay uses `WindowDoesNotAcceptFocus`.
- Record known gaps in the matrix; do not silently drop failures.

**Acceptance:** `EXCLUDED` on WGC-based tools; `FAILED` on the primary target is
a ship blocker.

---

## 2. Backend prerequisites (blocking the live-answer overlay)

These are server changes, not client changes. They are required before the
overlay can call `POST /api/v1/sessions/{id}/practice/answer` with reliable turn
IDs.

### 2.1 Mic vs loopback source tagging

- `WS /api/v1/sessions/{id}/audio` currently has no source parameter.
- `TranscriptService` stores finals with `speaker=segment.speaker or "candidate"`.
- Add `source=mic|loopback` (or an equivalent frame prefix/query parameter),
  propagate it to `STTSegment`/`Turn.speaker`, and keep
  `(session_id, segment_id)` idempotency correct per source.
- Without this, the interviewer's loopback speech is persisted as a candidate
  turn and cannot be a reliable `question_turn_id`.

### 2.2 Audio format exposure

- `/audio` does not negotiate or normalize format.
- Publish the frozen audio config on `session.status_changed` (sample rate,
  encoding, frame ms), or add `GET /sessions/{id}/audio-config`.
- The client currently assumes PCM S16LE, mono, 16 kHz, 50 ms (1,600 bytes).

### 2.3 Speaker/source in transcript events

- `speech.transcript_delta` and `speech.transcript_final` currently have no
  `speaker`.
- Add `speaker`/`source` to the event payloads, or require the client to fetch
  `GET /sessions/{id}/transcript` after each final. Adding it to events is
  preferable for a live overlay.

### 2.4 Turn Gate/live answer wiring

- Confirm the live path is `POST /practice/answer` and that Turn Gate runs
  before generation.
- If Turn Gate is not on that path, add the live endpoint/wiring or make the
  overlay call the Turn Gate explicitly.

### 2.5 Backend tests

- Source tagging → persisted `Turn.speaker`.
- Audio config appears on the session event or config endpoint.
- Transcript event payloads include `speaker`/`source`.
- Existing clients remain backward compatible.

---

## 3. Client WebSocket integration

After the backend prerequisites, wire the client in this order.

### 3.1 `/events` first

- Open `WS /api/v1/sessions/{id}/events` with
  `Sec-WebSocket-Protocol: bearer.<jwt>`.
- Consume the initial `session.status_changed` before any transcript event.
- Handle:
  - `session.status_changed`
  - `speech.transcript_delta`
  - `speech.transcript_final` (store `turn_id`, `sequence_number`)
  - `speech.transcript_error`
  - `artifact.extracted`
  - `debrief.status_changed`
  - `system.heartbeat`
  - `system.error`
  - `system.replay_gap`
- Send `client.heartbeat` every 10–30 seconds. The server closes the socket after
  65 seconds without a client message.
- `client.ack` has no sequencing effect; do not build replay guarantees on it.
  Use `client.resubscribe` with `last_known_sequence`.
- On reconnect, handle `system.replay_gap` by resyncing through
  `GET /sessions/{id}/transcript`.

### 3.2 `/audio` second

- Open `WS /api/v1/sessions/{id}/audio` after `/events` is ready.
- Send raw PCM S16LE mono 50 ms frames (1,600 bytes at 16 kHz) as binary frames.
- `/audio` does not coalesce or pad; keep each chunk within 20–1,000 ms.
- Send `{"type":"client.audio_end"}` on stop.
- Wait for `audio.closed` or `audio.failed` on the audio socket.
- Finals arrive on `/events`, not on `/audio`. Wait for both the final
  `speech.transcript_final` (for `turn_id`) and `audio.closed` (for flush
  completion) before committing the utterance.

### 3.3 Rendering

- Partial → update the partial label.
- Final → append to the transcript and dedup by `turn_id`/`segment_id`.
- Surface `speech.transcript_error` and `system.error` with retryable state.
- Continue the red capture indicator; the overlay stays display-only (no TTS).

### 3.4 Live answers

- Read `turn_id` from `speech.transcript_final`.
- Call `POST /api/v1/sessions/{id}/practice/answer` (HTTP + SSE):
  - `Authorization: Bearer <jwt>`
  - `Idempotency-Key`
  - `{"question_turn_id":"...","candidate_turn_id":"..."}`
- Render `assistant.answer_token`; finalize on `assistant.answer_done` or
  `assistant.answer_failed`.
- Do not start this until §2.1–§2.4 are resolved.

### 3.5 End, debrief, memory

- `POST /api/v1/sessions/{id}/end` (optional `Idempotency-Key`).
- Poll `GET /api/v1/sessions/{id}/debrief/status`:
  `writing`, `ready`, `no_content`, `generate_failed`, `unavailable`.
- Fetch `GET /api/v1/sessions/{id}/debrief`: `202` writing, `200` ready,
  `404` no_content/generate_failed/unavailable.
- `POST /api/v1/sessions/{id}/debrief/retry` on failure.
- Fetch `GET /api/v1/memory`; support delete/erase and
  `POST /api/v1/memory/retry?session_id=...`.
- Memory extraction is enqueued automatically when the debrief becomes `ready`.

### 3.6 Live prerequisites

- `GET /api/v1/policies/acceptable-use` → read `version`.
- `POST /api/v1/sessions/preflight` with `kind:"live"` and the current
  `acknowledged_policy_version`.
- `POST /api/v1/sessions` with `kind:"live"` and the acknowledged version, or
  create pending and `POST /api/v1/sessions/{id}/acknowledge`
  (`{"policy_version":"<version>"}`).
- Keep the overlay display-only and the capture indicator visible.

---

## 4. Client integration tests

Add tests that do not need AssemblyAI keys:

- `/events` initial status + delta/final rendering.
- 50 ms frame forwarding and `client.audio_end`.
- `audio.closed` / `audio.failed` handling.
- Heartbeat and reconnect/resubscribe.
- Answer SSE token/done/failed parsing (mock HTTP/SSE).
- End → debrief status/fetch/retry → memory list.

---

## 5. Packaging and release

- Decide PyInstaller/uv packaging.
- Choose a neutral binary name (per `initialize-plan.md` §10.4).
- Add installer/signing for Windows.
- Add OS-level hotkeys/tray.
- Document PortAudio requirements (`libportaudio2` on Debian/Ubuntu).
- Final target-machine verification: real mic, real backend, stealth matrix.

---

## Known limitations today

- No backend integration yet.
- Stealth is Windows-only and best-effort; macOS/Linux report `UNSUPPORTED`.
- F8/F9 may not work until a global hotkey/tray is added.
- This environment has no PortAudio; tests use the fake stream or
  `--simulate-audio`. Verify real capture on the target machine.
- `/stt` is not the product path; do not ship it as the live transport.
- No TTS; the overlay remains display-only.
- Raw audio is never persisted; only transcript turns are persisted by the
  backend `/audio` path.
