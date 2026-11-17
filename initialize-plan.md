# PySide6 Desktop Client Initialization Plan — Corrected

**Status:** ready for implementation
**Scope:** skeleton only (UI + mic capture + Windows capture exclusion); no business logic, no backend changes
**Platform:** Windows (stealth feature is Windows-only; macOS/Linux render but stay capture-visible)
**Target session path:** `WS /api/v1/sessions/{id}/audio` + `WS /api/v1/sessions/{id}/events`
**Not targeted:** `/stt` (dev spike only; ephemeral, no turns/debrief/memory)

---

## 1. Product decision (verified)

The overlay is the Live surface of a product whose session flow requires persisted
turns, an evidence-based debrief, and cross-session memory:

- `docs/spec-v2.1.0-frozen.md` §3.3: session end → async debrief → memory commit → history/analytics.
- §3.7: review retained timeline/transcript/debrief evidence.
- §7.8/§7.9: debrief and memory are defined capabilities.
- Live answer generation needs `turn_id` values; only the persisted `/audio` path creates them.

**Decision:** target `/audio` + `/events` via `TranscriptService`. Use `/stt` only behind
a dev flag to validate mic capture and framing.

---

## 2. Backend-verified contract

### 2.1 Sockets and order

| Order | Path | Purpose |
|---|---|---|
| 1 | `WS /api/v1/sessions/{id}/events` | Subscribe first; receives the initial session snapshot |
| 2 | `WS /api/v1/sessions/{id}/audio` | Send binary audio |

Both use `Sec-WebSocket-Protocol: bearer.<jwt>`. First-message `auth` is a fallback;
`?token=` is rejected. Close codes: `4401` auth, `4403` ownership, `4404` missing session.

### 2.2 `/events` server messages

Standard envelope:
`event_id`, `session_id`, `sequence_number`, `server_timestamp`, `schema_version`,
`event_type`, `payload`.

The server sends an initial `session.status_changed` immediately after accept, before
any transcript event. The client must consume it.

Event catalog the client should handle (or explicitly ignore):

| `event_type` | Payload notes |
|---|---|
| `session.status_changed` | `old_status`, `new_status`, `capture_status` |
| `speech.transcript_delta` | `text`, `is_final:false`, `segment_id`, `confidence`; **no speaker** |
| `speech.transcript_final` | `text`, `is_final:true`, `segment_id`, `confidence`, `turn_id`, `sequence_number`; **no speaker** |
| `speech.transcript_error` | `error_class`, `retryable`, `message` |
| `artifact.extracted` | `artifact_id`, `artifact_type`, `title` |
| `debrief.status_changed` | `status` (`ready` / `generate_failed`) |
| `system.heartbeat` | `server_time`; sent only in response to `client.heartbeat` |
| `system.error` | `code`, `error_class`, `retryable` |
| `system.replay_gap` | Reconnect only: `oldest_available_sequence`, `last_known_sequence` |

### 2.3 `/events` client commands

| Command | Backend behavior |
|---|---|
| `{"type":"client.heartbeat"}` | Updates `last_activity_at`, publishes `system.heartbeat` |
| `{"type":"client.ack","sequence_number":N}` | **No sequencing effect**; only increments `ws_ack_total` |
| `{"type":"client.resubscribe","last_known_sequence":N}` | Replays later events; may send `system.replay_gap` first |
| `{"type":"client.capture_control","source":"mic|loopback|screen","action":"pause|resume|stop"}` | Updates capture status, publishes `session.status_changed` |
| `{"type":"client.stop_all"}` | Sets all capture sources inactive, publishes status |

**Critical:** do not build delivery/replay guarantees on `client.ack`. Use
`client.resubscribe` for replay. The events socket closes `1000` after **65 seconds**
without a client message; send a heartbeat every 10–30 seconds.

### 2.4 `/audio` behavior

- Binary frames only. Raw audio bytes; no JSON/base64/container headers.
- The backend forwards bytes to the configured provider **without coalescing or padding**.
  The client must keep each chunk within AssemblyAI's 20–1,000 ms range; 50 ms is recommended.
- The backend does **not** negotiate audio format. The client must assume PCM S16LE,
  mono, 16 kHz, 50 ms frames (1,600 bytes) unless the session config says otherwise.
- Send `{"type":"client.audio_end"}`. The server waits up to 5 s for the provider flush, then sends:
  - `{"type":"audio.closed"}` on success
  - `{"type":"audio.failed","error_class":"...","retryable":true|false,"message":"..."}` on failure
- Transcript events do **not** arrive on `/audio`; they arrive on `/events`.
- There is no `speech.stream_closed` on the WS `/audio` path. `audio.closed` is the
  audio-flush signal; `speech.transcript_final` is the turn signal. They can race.

### 2.5 Live answer path

- Read `turn_id` and `sequence_number` from `speech.transcript_final`.
- Call `POST /api/v1/sessions/{id}/practice/answer` (HTTP + SSE):
  - Authorization: `Bearer <jwt>`
  - `Idempotency-Key` recommended
  - body: `{"question_turn_id":"...","candidate_turn_id":"..."}` (`candidate_turn_id` optional)
  - events: `assistant.answer_token`, `assistant.answer_done`, `assistant.answer_failed`
- If the overlay needs speaker labels, GET `/api/v1/sessions/{id}/transcript` (turns include
  `speaker`); transcript event payloads do not.

### 2.6 Session end, debrief, memory

| Action | Endpoint | Notes |
|---|---|---|
| End | `POST /api/v1/sessions/{id}/end` | Optional `Idempotency-Key`; enqueues debrief when ≥ `min_turns_for_evidence` |
| Debrief status | `GET /api/v1/sessions/{id}/debrief/status` | `writing`, `ready`, `no_content`, `generate_failed`, `unavailable`; never 404 for a valid session |
| Debrief fetch | `GET /api/v1/sessions/{id}/debrief` | `202` writing, `200` ready, `404` no_content/generate_failed/unavailable |
| Debrief retry | `POST /api/v1/sessions/{id}/debrief/retry` | Re-enqueues failed/empty |
| Memory list | `GET /api/v1/memory` | Cursor/offset per backend |
| Memory delete | `DELETE /api/v1/memory/{id}`, `DELETE /api/v1/memory` | Does not delete sessions/debriefs |
| Memory retry | `POST /api/v1/memory/retry?session_id=...` | Requires a ready debrief; rate-limited |

Memory extraction is enqueued automatically when the debrief becomes `ready`.

### 2.7 Live prerequisites

1. `GET /api/v1/policies/acceptable-use` → `{version, text, effective_at}`.
2. `POST /api/v1/sessions/preflight` with `kind:"live"` and the current
   `acknowledged_policy_version`; inspect `blockers`/`warnings`.
3. `POST /api/v1/sessions` (optional `Idempotency-Key`) with:
   - `kind: "live"`
   - `acknowledged_policy_version: <version>` → session starts `active`
   - or omit it → session starts `pending`
4. If pending: `POST /api/v1/sessions/{id}/acknowledge` with
   `{"policy_version":"<version>"}`. Mismatch returns `409`.
5. The overlay stays display-only: no TTS, visible consent/capture indicator.

### 2.8 Corrections applied from the earlier draft

- Removed reliance on `client.ack` sequence semantics.
- Added initial `session.status_changed`, `system.replay_gap`, `artifact.extracted`,
  and `debrief.status_changed`.
- Added heartbeat cadence and the 65 s socket timeout.
- Added exact transcript payload fields and the absence of `speaker`.
- Added finalization ordering (`speech.transcript_final` + `audio.closed`).
- Added HTTP bearer/idempotency details for answer/end/create.
- Added full debrief status/HTTP-code handling and memory retry.
- Added the acceptable-use version source.
- Added `audio.failed` shape.
- Clarified `/audio` has no `speech.stream_closed`.
- Corrected Qt click-through guidance (see §5).
- Added Windows capture exclusion as a first-class feature (see §10).

---

## 3. Backend prerequisites before the live-answer overlay

These are **not** part of the skeleton, but block the overlay's live-answer loop:

1. **Mic vs loopback source tagging (blocking).**
   `/audio` has no source parameter, and `TranscriptService` defaults finals to
   `speaker="candidate"`. The interviewer's loopback speech would be persisted as a
   candidate turn, so it cannot reliably be used as `question_turn_id`.
   Required: add `?source=mic|loopback` (or equivalent) to `/audio`, propagate it to
   `STTSegment`/`Turn.speaker`, and keep `(session_id, segment_id)` idempotency scoped
   per source if needed.
2. **Audio format exposure.**
   `/audio` does not negotiate or normalize format. Either publish the frozen audio
   config on `session.status_changed` (sample rate, encoding, frame ms) or add
   `GET /sessions/{id}/audio-config`; otherwise the client's PCM 16 kHz assumption is
   unverifiable.
3. **Speaker labels in live events.**
   Transcript event payloads omit `speaker`. Either add `speaker`/`source` to
   `speech.transcript_delta`/`final`, or have the overlay fetch
   `GET /sessions/{id}/transcript` after each final. For a live overlay, adding it to
   the event is preferable.
4. **Turn Gate/live answer wiring.**
   Confirm the live overlay's answer path is `POST /practice/answer` and that Turn Gate
   runs before generation. If not, a dedicated live-answer endpoint is needed.

---

## 4. Initialization prompt (corrected)

Create a Python package `desktop_client/` with:

```text
desktop_client/
  __init__.py
  main.py
  overlay.py
  audio.py
  stealth.py
```

Do not implement WebSocket/answer/debrief logic in this iteration. Only prove UI +
mic capture + capture exclusion independently.

### `overlay.py`

- Subclass `QWidget`.
- Window flags:
  `FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowDoesNotAcceptFocus`.
- Attributes: `WA_TranslucentBackground`, `WA_ShowWithoutActivating`.
- `paintEvent`: antialiased rounded rect, `QColor(0, 0, 0, 200)`, no pen.
- Layout:
  - Status row: red pulsing dot + "listening" + model name (right-aligned) + stealth badge (§10.5).
  - Transcript area: read-only `QTextEdit` for finals; separate `QLabel` for the
    current partial (or a dedicated partial line).
  - Controls row: Stop/Close buttons.
- API: `set_transcript(text: str)`, `set_partial(text: str)`, `set_status(text: str)`,
  `set_model(text: str)`, `set_stealth_state(state)`.
- Click-through:
  - Interactive mode: normal input.
  - Click-through mode: `setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)`
    (and/or `WA_TransparentForMouseEvents` on the window). Do **not** set
    `WA_TransparentForMouseEvents` on the window while expecting child buttons to work:
    Qt disables mouse delivery to children too.
- Stealth (§10): enabled at startup and in `showEvent`; re-applied after every flag
  change. Independent of click-through.
- Toggle click-through and stealth with hardcoded shortcuts for the skeleton; production
  should use an OS-level global hotkey or tray menu because `WindowDoesNotAcceptFocus`
  may prevent keyboard delivery.
- Drag: only in interactive mode; use `windowHandle().startSystemMove()` where available,
  otherwise `mousePressEvent`/`mouseMoveEvent`.
- Platform note: always-on-top/focus behavior is validated on Windows for this iteration;
  macOS/X11/Wayland still render but are capture-visible.

### `audio.py`

- Use `sounddevice.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=800)`.
  At 16 kHz, 800 frames = 50 ms = 1,600 bytes.
- Expose `async def mic_frames() -> AsyncIterator[bytes]`.
- Resampling:
  - Simplest: open at 16 kHz and let PortAudio resample the device.
  - If capturing at the native rate, use `scipy.signal.resample_poly` to 16 kHz, then
    regroup into 800-sample (1,600-byte) frames. Do not double-resample.
- Bridge the PortAudio callback thread to asyncio with
  `loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))`.
- Bounded queue (e.g., 40 frames ≈ 2 s). Never block the audio callback; drop oldest or
  overwrite with a warning.
- On stop, pad a short tail with silence to a full 50 ms frame, then end the generator.
- No WebSocket logic here.

### `stealth.py`

See §10.2 for the full contract. Windows only; `UNSUPPORTED` elsewhere.

### `main.py`

- Use `qasync` to merge Qt and asyncio. Preferred skeleton pattern:
  `asyncio.run(main(app), loop_factory=QEventLoop)` (Python 3.12+).
  Fallback if the installed qasync/Qt combination rejects it:
  `loop = QEventLoop(app); asyncio.set_event_loop(loop); loop.run_until_complete(main()); loop.run_forever()`.
- Instantiate/show `OverlayWindow`; run a coroutine that reads one frame from
  `mic_frames()` and calls `set_partial(f"captured {len(frame)} bytes")`.
- No `/events` or `/audio` connection yet.

### Verification

- `python -m desktop_client.main` shows the overlay.
- It prints `captured 1600 bytes` (or the actual frame length) at least once.
- Overlay stays above a full-screen video call.
- Click-through toggle works; controls are clickable in interactive mode.
- Stealth badge reports `hidden from share` and the overlay is absent from a Teams share.
- No backend files or endpoints changed.

---

## 5. UI guide corrections

- **Click-through:** `WA_TransparentForMouseEvents` on the top-level widget disables
  mouse events for the widget **and its children**. Use window-level
  `WindowTransparentForInput` for click-through, and remove it in interactive mode so
  Stop/Close work.
- **Focus:** `WindowDoesNotAcceptFocus` prevents focus stealing but also keyboard input.
  Use an OS-level hotkey/tray for the click-through and stealth toggles; a Qt `QShortcut`
  may never fire while the overlay is active.
- **Transcript rendering:** a `QTextEdit` is read-only and scrollable; use a separate
  `QLabel` for the mutable partial to avoid reflowing the whole transcript on every delta.
- **Status indicator:** the red dot is client-side; the backend exposes
  `capture_status` but does not render it. The visible indicator is still required.
- **Background:** custom `paintEvent`; do not rely on a stylesheet background with
  `WA_TranslucentBackground`.
- **Drag:** only interactive mode; `startSystemMove()` is preferred on modern Qt.
- **Stealth badge:** state must be driven by the real API result, never assumed. A false
  "hidden" badge is worse than no badge.
- **Black-box fallback:** when the capture side shows an opaque rectangle, raise paint
  alpha toward `255` so it reads as intentional. User's own view is unaffected.

---

## 6. WebSocket integration guide (next iteration, not skeleton)

1. Acquire JWT and create a live session (see §2.7).
2. Open `/events` first; wait for the initial `session.status_changed`.
3. Open `/audio`; send 50 ms PCM frames.
4. Consume `/events`:
   - Partial → update partial label.
   - Final → append finalized text, store `turn_id`, dedup by `turn_id`/`segment_id`.
   - `speech.transcript_error` / `system.error` → surface retryable state.
   - `artifact.extracted` → optional artifact pane.
   - `debrief.status_changed` → update post-session state if visible.
5. Heartbeat every 10–30 s; never exceed 65 s of silence.
6. On `/events` reconnect: `client.resubscribe` with `last_known_sequence`; handle
   `system.replay_gap` by resyncing via `GET /sessions/{id}/transcript`.
7. On stop: send `client.audio_end`; wait for `audio.closed`/`audio.failed` and the final
   `speech.transcript_final`.
8. For live answers: `POST /practice/answer` (HTTP+SSE) with turn IDs and
   `Idempotency-Key`; render `answer_token` and finalize on `answer_done`/`answer_failed`.
9. On session end: `POST /sessions/{id}/end`; poll debrief status; fetch debrief; support
   retry; fetch/delete memory.

### Error/close handling

- `4401/4403/4404`: re-auth / wrong session; do not retry blindly.
- `/events` close `1000`: likely idle timeout; reconnect and resubscribe.
- `audio.failed`: inspect `retryable`; transient → restart the audio stream.
- `stt_provider_unavailable` via `system.error`: configuration/key problem; surface a
  non-retryable error.

---

## 7. Verification checklist

- [ ] Overlay renders above a full-screen video call.
- [ ] Click-through toggle works; controls clickable in interactive mode.
- [ ] One 50 ms frame captured and printed.
- [ ] No backend changes.
- [ ] `pytest` suite remains green (backend untouched).
- [ ] Overlay absent from a Teams share while badge reads `hidden from share`.
- [ ] Exclusion survives a click-through toggle round-trip.
- [ ] Exclusion survives hide/show and a session restart.
- [ ] Stealth badge matches actual capture behavior on every tool in §10.6.
- [ ] Next iteration: `/events` initial status received; `/audio` frames accepted;
      `speech.transcript_final` produces a `turn_id`; `audio.closed` received.
- [ ] Next iteration: live answer SSE renders tokens and finishes.
- [ ] Next iteration: end → debrief status/fetch → memory list.

---

## 8. Non-goals and constraints

- No business logic in the skeleton.
- No WebSocket connection in the skeleton.
- Overlay remains display-only; no TTS.
- Raw audio is never persisted; only transcript turns are persisted on `/audio`.
- Do not change backend code or the `/audio` + `/events` contract as part of the
  skeleton.
- `/stt` is not the product path; do not ship it as the Live transport.
- Stealth is Windows-only and best-effort by OS capability; never presented as a guarantee.
- No new dependencies for stealth (stdlib `ctypes` only).

---

## 9. Open items

1. Implement mic/loopback source tagging on `/audio` + `TranscriptService` (blocking).
2. Publish audio format on the session event stream or a config endpoint.
3. Decide whether to add `speaker` to transcript event payloads.
4. Smoke-test qasync's `loop_factory` pattern on the target Python/PySide6 versions.
5. Define packaging (PyInstaller/uv) and OS-level hotkey strategy.
6. Confirm PyInstaller `--name` in the packaging decision — it is a stealth input, not
   just a distribution detail.
7. Run the §10.6 matrix on the primary target machine before the overlay ships.

---

## 10. Stealth (Windows capture exclusion)

### 10.0 Design intent

Two orthogonal axes, two separate code paths, never merged:

| Axis | Mechanism | Effect |
|---|---|---|
| **Capture visibility** (stealth) | `SetWindowDisplayAffinity` | Window absent from screen share, recording, screenshots |
| **Input transparency** (click-through) | `WindowTransparentForInput` | Clicks pass through to apps underneath |

Stealth is not implemented via `WA_TransparentForMouseEvents` and is not bundled into the
click-through toggle. Independent toggles.

Naming discipline: the API is advisory. Say "excluded from capture," never "invisible."
DWM can drop the exclusion; the user must be able to see which state they are in (§10.5).

Stealth is **on by default** for a Live session. An interview overlay that leaks into the
share is worse than one that occasionally shows a black box.

**Platform scope: Windows only.** macOS and Linux return `UNSUPPORTED`, log once, and show
the "visible to share" badge. No PyObjC dependency, no platform branches beyond the one
enum value.

### 10.1 Supported configurations

| OS | API | Result | Quality |
|---|---|---|---|
| Windows 10 2004+ / Windows 11 | `WDA_EXCLUDEFROMCAPTURE` (`0x11`) | Window omitted from WGC, DXGI duplication, BitBlt, Game Bar, Snipping Tool (24H2+), Teams/Zoom/Meet capture | **Primary path** |
| Windows 10 < 2004 | `WDA_MONITOR` (`0x01`) | Black rectangle instead of a hole | Degraded, acceptable |

Caveats to encode, not to discover later:

- `SetWindowDisplayAffinity` returns `FALSE` for non-top-level windows and for handles not
  owned by the calling process. Always check the return and `GetLastError()`.
- A known subset of Windows 11 machines rejects **both** values (driver/compositor
  dependent). Handle failure as a real state, not an exception.
- Exclusion also applies to RDP/VNC capture: a remote viewer will not see the overlay.
  Note in user docs.
- Hardware capture cards and phone-camera-of-screen always see it.
- Fullscreen-exclusive renderers ignore both flags.
- Per-pixel-alpha layered windows can render as a **black rectangle** rather than a
  transparent hole on some capture pipelines. That is why §10.3 has a paint fallback.

### 10.2 New module `desktop_client/stealth.py`

```python
class StealthState(Enum):
    EXCLUDED    = "excluded"     # OS confirmed exclusion
    BLACK_BOX   = "black_box"    # excluded as opaque rect (WDA_MONITOR)
    UNSUPPORTED = "unsupported"  # not Windows
    FAILED      = "failed"       # API present, call rejected
```

Public API:

```python
def capability() -> StealthState      # probe, no side effects
def apply(widget: QWidget) -> StealthState
def clear(widget: QWidget) -> None
```

Rules:

- GUI thread only. Never call from the audio callback or a worker.
- `apply` requires a live native handle on a top-level widget. Call after `show()`, never before.
- Never cache the handle — see §10.3.
- Implementation is `ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), 0x11)`.
- Gate `0x11` on `sys.getwindowsversion().build >= 19041`; on older builds or on `FALSE`,
  retry once with `0x01` → `BLACK_BOX`. Second failure → `FAILED` plus one logged
  `GetLastError()`.
- `clear` sets `WDA_NONE` (`0x00`); idempotent.
- Non-Windows: `apply` returns `UNSUPPORTED` immediately, without touching `ctypes.windll`.

### 10.3 Re-application rules (the part that silently breaks)

Qt **destroys and recreates the native HWND** on `setWindowFlag()`, on `setParent`, and on
hide/show cycles — including the `WindowTransparentForInput` toggle in §5. Affinity dies
with the old handle, and a stale handle makes the call silently no-op.

Therefore:

1. Re-apply in `showEvent`.
2. After any `setWindowFlag()` call, re-apply via `QTimer.singleShot(0, ...)` — never
   inline; the new HWND does not exist yet.
3. The click-through toggle calls `apply()` as its last step, in both directions.
4. Keep the current `StealthState` in a widget field and re-apply from that field, so
   handle re-creation preserves the user's chosen mode.
5. Handle goes in a local, never on the instance.
6. If `state == BLACK_BOX`, raise the paint color alpha toward `255` so the capture-side
   black box looks intentional instead of broken. The user's own view is unaffected.

### 10.4 Detector surfaces

| Detector technique | Mitigation | Owner |
|---|---|---|
| Pixel capture (share/record/screenshot) | display affinity | `stealth.py` |
| `EnumWindows` enumeration | `Qt.Tool`, no taskbar entry | overlay (existing) |
| Alt-Tab presence | tool window | overlay (existing) |
| Process name enumeration | neutral binary name (PyInstaller `--name`) | packaging |
| Window title matching | neutral title (`"Overlay"`, not `"Interview Copilot"`) | overlay |
| Focus-steal heuristics | `WindowDoesNotAcceptFocus` | overlay (existing) |
| Z-order probing | `WindowStaysOnTopHint` | overlay (existing) |

Enumeration, title, and process mitigations are **obfuscation, not exclusion**. A detector
listing every top-level window still finds this one; it just won't look like an assistant.

### 10.5 UI affordance

Status row gains a stealth badge driven by `StealthState`:

| State | Badge |
|---|---|
| `EXCLUDED` | `hidden from share` (dim green) |
| `BLACK_BOX` | `hidden (opaque)` (dim amber) |
| `FAILED` | `capture exclusion failed` (dim red) |
| `UNSUPPORTED` | `visible to share` (dim red) |

Hardcoded toggle shortcut alongside the click-through shortcut. Same production caveat as
§5: `WindowDoesNotAcceptFocus` may swallow Qt shortcuts, so an OS-level hotkey or tray item
is required before ship.

### 10.6 Verification

Run on Windows 11 and Windows 10 22H2, record pass/fail per cell:

- [ ] Teams screen share (window + full screen)
- [ ] Zoom screen share
- [ ] Meet in Chrome
- [ ] OBS — WGC display capture **and** BitBlt display capture
- [ ] Snipping Tool (Win11 24H2+)
- [ ] Xbox Game Bar recording
- [ ] Discord screen share
- [ ] Exclusion survives a click-through toggle round-trip
- [ ] Exclusion survives hide/show and a session restart
- [ ] Badge matches actual behavior in every cell

Acceptance: `EXCLUDED` on all WGC-based tools. `FAILED` on the primary target machine is a
ship blocker. Any tool that shows the overlay despite `EXCLUDED` gets recorded in the matrix
as a known gap, not quietly dropped.

### 10.7 Known limitations

- No guarantee, per Microsoft's own documentation.
- Win11 driver subset rejects both affinity values.
- Fullscreen-exclusive renderers ignore it.
- RDP/VNC viewers lose the overlay.
- Photographing the monitor always works.
- Black-box degradation is possible on layered translucent windows.
- No macOS or Linux coverage.

### 10.8 Scope

`stealth.py`, the badge, the toggle, and the verification matrix. Client-side only. No
backend files, endpoints, or contracts touched. No new dependencies.