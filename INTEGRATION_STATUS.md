# Client ↔ Backend Integration Status

Scope: `E:\ai_interview_desktop` (client) against `E:\ai_interview\backend` (server, commit-blocked, unmodified).
Legend: **OK** = matched and exercised · **PARTIAL** = works with a caveat · **GAP** = contract or behavior missing.
Evidence: source files on both sides + three live runs against a migrated throwaway DB with a dev JWT, including a `stub_stt` + `deterministic_llm` happy path.

---

## 1. Verdict

The client speaks the backend's actual wire contract: session setup, `/events`, `/audio`, `practice/answer` + `live/answer` SSE, end/debrief/memory.

**All three previously blocking server gaps are resolved** in the backend working tree, and the client now uses them:

1. `/audio?source=mic|loopback` tags persisted turns (`candidate` / `interviewer`).
2. Transcript events carry `speaker`, so the client no longer refetches the transcript after each final.
3. `POST /sessions/{id}/live/answer` runs the Turn Gate and emits `assistant.answer_skipped` when it declines; the manual `practice/answer` stays ungated.

Remaining gaps are smaller: no audio-format negotiation, the IdP must still
allow the desktop loopback redirect, and the debrief UI still polls instead of
using `debrief.status_changed`. Interactive login itself is implemented
(portal handoff + loopback PKCE; see `docs/desktop-auth-handoff.md`).

---

## 2. Contract matrix

### Authentication & session setup

| Contract | Backend reality | Client behavior | Status |
|---|---|---|---|
| WebSocket auth | `Sec-WebSocket-Protocol: bearer.<jwt>`; rejects `?token=` (4401) | Sends exact subprotocol on both sockets | OK |
| HTTP auth | `Authorization: Bearer <jwt>` | Set on the shared `httpx` client | OK |
| Token issuance | OIDC PKCE `GET /auth/{provider}/authorize`, `POST /auth/{provider}/token`, `POST /auth/refresh` (rotating), `POST /auth/logout`; portal `GET /web/desktop-login` + `POST /auth/handoff` | First-run login uses the portal handoff (browser opens `/web/desktop-login`, one-time code redeemed at `/auth/handoff`); rotates via `POST /auth/refresh` and persists the rotated refresh token in the OS credential store | OK (IdP redirect registration is a deployment item) |
| Token lifetime | 60 min (`ACCESS_TOKEN_EXPIRE_MINUTES`) | Refreshes ahead of expiry, on HTTP 401, and once on a 4401 WS rejection (needs `DESKTOP_REFRESH_TOKEN` + `DESKTOP_DEVICE_ID`) | OK |
| Acceptable use | `GET /policies/acceptable-use` → `{version,text,effective_at}` | Reads `version`; policy text not displayed | PARTIAL |
| Preflight | `POST /sessions/preflight` (`kind`, ack version) → `can_start`, `blockers`, `warnings`, `policies` | Aborts on blockers; shows the first warning; takes the answer model from `policies.providers.llm.answer_generation` | OK |
| Create session | `POST /sessions`; one active per user → `409`; idempotency cached per `(user,key)` for process lifetime | Unique key per launch; on 409 adopts `/sessions/active`, acks if pending | OK |
| Acknowledge | `POST /sessions/{id}/acknowledge`; version mismatch → `409` | Calls it for a `pending` session | OK |
| Status model | `pending` → `active` → `ended` | Requires `active` before capture | OK |

### Event stream

| Contract | Backend reality | Client behavior | Status |
|---|---|---|---|
| Initial snapshot | `session.status_changed` published and sent before subscribe | Applied to status + capture badge | OK |
| Idle timeout | Socket closed `1000` after **65 s** without a client message | `client.heartbeat` every 20 s (clamped 1–30 s) | OK |
| Replay | `client.resubscribe {last_known_sequence}`; may emit `system.replay_gap` | Tracks last sequence; resubscribes; refetches transcript on gap | OK |
| `client.ack` | Increments a metric only | Deliberately not sent | OK |
| Close codes | `4401` auth, `4403` ownership, `4404` missing session | Refreshes the token once on 4401, then stops and shows "unauthorized"; 4403/4404 stop immediately | OK |
| Event catalog | status, delta, final, transcript_error, artifact.extracted, debrief.status_changed, heartbeat, system.error, replay_gap | All handled; `debrief.status_changed` only logged; heartbeat ignored | PARTIAL |
| `speaker` on events | Present (nullable) on delta/final, derived from `?source=` | Used directly; no per-final refetch | OK |
| Capture control | `client.capture_control`, `client.stop_all` supported | Never sent; no pause/resume UI | PARTIAL |

### Audio

| Contract | Backend reality | Client behavior | Status |
|---|---|---|---|
| Framing | Raw binary, no coalescing/padding; 20–1,000 ms accepted | 1,600-byte 50 ms S16LE frames | OK |
| Format | Never negotiated or normalized | Assumes 16 kHz mono S16LE | **GAP (server)** |
| Source tagging | `?source=mic|loopback` (default `mic`); unknown → `audio.failed` + `1008`; loopback segment ids namespaced | Sends `?source=` from `DESKTOP_AUDIO_SOURCE`; validates `mic|loopback` before connecting | OK |
| Finalization | `client.audio_end` → up to 5 s flush → `audio.closed`/`audio.failed` | Waits 12 s; maps closed/failed/disconnected/timeout | OK |
| Transcript delivery | Finals arrive on `/events`, not `/audio` | Consumed from `/events` | OK |
| `audio.failed` retry | `retryable` flag | One automatic restart on retryable failure/disconnect, then surfaced | OK |
| STT availability | Default provider `assemblyai`; missing key → `system.error stt_provider_unavailable` + close `1011` | Surfaces "degraded"; audio flagged disconnected | OK (verified live) |

### Answers

| Contract | Backend reality | Client behavior | Status |
|---|---|---|---|
| Endpoints | `POST /practice/answer` (explicit, never gated) and `POST /live/answer` (gated) | Auto-answer uses `live/answer`; F10 uses `practice/answer` | OK |
| Body | `{question_turn_id, candidate_turn_id?}` | Same | OK |
| Idempotency | Key replay returns the recorded answer; `answer_index` per question | `answer-<turn>` and `live-answer-<turn>` (separate modes) | OK |
| Events | `assistant.answer_token`, `answer_done` (`validated_object.answer_text`), `answer_failed`, and `answer_skipped` on the gated path | Renders tokens, prefers validated text, retries when retryable, treats skip as non-error | OK |
| Preconditions | Session must be `active` with a policy snapshot | Ensured; Stop waits for the last answer before ending | OK |
| **Turn Gate** | `live/answer` runs `TurnGateService` before generation and emits `assistant.answer_skipped` (`decision`, `intent`, `reason`) when it declines | Gated auto-answer only for `interviewer` finals; skip clears the pane and shows the reason | OK |
| Question selection | Speaker-tagged turns make the roles explicit | Answers the newest `interviewer` turn; `candidate_turn_id` = newest candidate turn before it | OK (mic-only capture still yields no interviewer turns, so auto-answer stays idle) |

### End, debrief, memory

| Contract | Backend reality | Client behavior | Status |
|---|---|---|---|
| End | `POST /sessions/{id}/end`; idempotent; enqueues debrief if finalized turns ≥ 3, else `no_content` | `Idempotency-Key: end-<session_id>` | OK |
| Debrief status | Never 404 for a valid session; `writing/ready/no_content/generate_failed/unavailable` | Polls every 2 s, 120 s cap | OK |
| Debrief fetch | `202` writing, `200` ready, `404` none | `200` rendered (score/summary/strengths/gaps); `404` mapped to status text | OK |
| Debrief retry | `409` already ready/not retryable, `429` limit | `generate_failed` exposes Retry | OK |
| Memory | `GET /memory` (limit/offset/total), `DELETE /memory/{id}`, `DELETE /memory`, `POST /memory/retry` | Lists count; tray exposes confirmed erase-all and extraction retry | OK |
| Debrief worker | Generation is an async job (taskiq broker/worker) | Client polls instead of relying on the event | OK (operational dependency) |
| `debrief.status_changed` | Published on ready / generate_failed | Logged only; polling is the source of truth | PARTIAL |

---

## 3. What the live run proved

Environment: `test.db` copied to a temp path, `alembic upgrade head`, dev token via `tools/generate_dev_token.py`, real `uvicorn` on `127.0.0.1:8011`, client driven by `tests/test_live_smoke.py`.

```
session : 2f3be75c-75e5-47c9-8109-47ad3f658257
status  : degraded
audio   : AudioOutcome(status='disconnected', ... 1011)
turns   : (empty)
partial : memory: 0 item(s)
```

Backend DB after the run:

```
sessions: ('2f3be75c-...', 'ended', 'live', 'no_content', '2026-11')
turns   : 0
debriefs: ('no_content', '{"total_turns": 0, "reason": "below_min_turns_for_evidence", ...}')
```

Proven end to end in the first (degraded) run: preflight → session create → policy ack → `/events` connect → `/audio` connect → `system.error` surfacing → `client.audio_end` → session end → debrief status/fetch → memory list. Re-run after the client fixes (session `441663e4-…`): same outcome, no regression.

Runs after the backend fixes, with the throwaway config switched to `stub_stt` + `deterministic_llm` (admin `/config/publish`, temp DB only):

- **mic + stub STT** (session `c2d25ea5`): `speech.transcript_final` arrived with `speaker=candidate` and the overlay rendered `You: This is a stubbed candidate transcript.` — no transcript refetch.
- **loopback + auto-answer** (sessions `07913e99`, `dc0ff814`): overlay rendered `Interviewer: …`; the gated `/live/answer` ran; the backend persisted an `assistant` turn (`I would lead with the constraint, then explain the trade-off.`) and a `session.answer_generated` event; the client recorded the question in `answered_turn_ids`.

Not proven live: real-provider transcripts/answers (needs `ASSEMBLYAI_API_KEY` / `OPENROUTER_API_KEY`) and generated debrief content (needs the taskiq worker). The stub/deterministic providers exercise the same code paths.

---

## 4. Gaps by owner

### Server-side (not changed here)

1. **No audio-format negotiation** — 16 kHz S16LE mono is an assumption on both sides.
2. *(Optional)* **Diarization** as a supplement to source tags — see below; not required now.

#### Why `?source=` rather than AssemblyAI diarization

Resolved in favour of explicit source tags. The diarization analysis still explains why it was not chosen:

- **The SDK answers the schema question.** `assemblyai>=1.10.11` (inspected 1.10.11, matching the backend pin) reports both: `TurnEvent.speaker_label` at the turn level and `Word.speaker` per word. `_normalize` reads only the turn-level label and ignores per-word speakers, so a turn containing two speakers is flattened to one label.
- **There is also a `SpeakerRevisionEvent`** (per-`turn_order` revised `speaker_label` + `words`) emitted after an offline recluster. The adapter does not handle it at all, and finalized Turns are immutable/persisted, so late corrections would be silently lost.
- It produces `speaker_A` / `speaker_B`, not roles. `practice/answer` selects an explicit candidate turn with `Turn.speaker == "candidate"`, and `TranscriptService` writes `segment.speaker or "candidate"` — so enabled diarization would change the stored speaker and break that filter unless a label→role mapping is added.
- Role still has to be inferred. With mic-only capture and headphones there is one speaker; with room audio there are two labels but no statement of which one is the interviewer.
- `source` also identifies the **capture path** (mic vs loopback), which diarization on one mixed stream cannot recover.

Conclusion: `?source=mic|loopback` is now implemented and maps mic→`candidate`, loopback→`interviewer`. Diarization remains an optional supplement, but would still need config, per-word parsing, `SpeakerRevision` handling, a role mapping, and `practice/answer` compatibility.

### Client-side

1. **Interactive login — implemented.** The client obtains its first token pair
   through the backend portal handoff (loopback PKCE + one-time code) and
   persists the rotating refresh token in Windows Credential Manager. The
   remaining deployment item is the IdP redirect allowlist for the loopback URI;
   `DESKTOP_ACCESS_TOKEN` remains a dev/CI override.
2. **`debrief.status_changed` is only logged** — polling remains the source of truth.
3. **Loopback capture is not implemented** — `DESKTOP_AUDIO_SOURCE=loopback` labels the stream correctly but only mic audio is captured, so it is a test/tooling setting, not a product path yet.

### Fixed in this pass

- **Create idempotency key was constant per process** (`desktop-<policy_version>`). The backend caches create responses for the process lifetime, so a second launch replayed the first (already ended) session. Now a per-launch UUID, with a regression test.
- **`/events` was closed before `POST /end`**, so the terminal `session.status_changed` and `debrief.status_changed` could never arrive. End now runs first.
- **Token refresh** — ahead of expiry, on HTTP 401, and once on a 4401 WS rejection; rotates the refresh token.
- **Audio restarts once** on a retryable `audio.failed` or a dropped socket (not after a user Stop).
- **Quit/Close now ends the session** so it is not left `active` for the next launch; a reused `DESKTOP_SESSION_ID` is deliberately left alone.
- **Memory controls** — tray actions for erase-all (confirmed) and extraction retry.
- **Preflight warnings surfaced** and the model label taken from backend config instead of the hardcoded `gemini-2.5-flash`.
- **`transcript_error`** now reports as a capture-status error instead of occupying the answer pane.
- **Speaker from events** — finals are labeled from the payload; the per-final `GET /transcript` refetch is gone (kept only as a fallback when `speaker` is absent).
- **Gated answers** — auto-answer now only fires for `interviewer` finals and calls `/live/answer`; `assistant.answer_skipped` clears the pane without an error.
- **Question selection** — newest `interviewer` turn is the question; `candidate_turn_id` is the newest candidate turn *before* it.
- **Stop waits for the last answer** before ending the session, avoiding an answer/end race.
- **Source validation** — `DESKTOP_AUDIO_SOURCE` must be `mic` or `loopback`; anything else fails fast instead of consuming a `1008`.

---

## 5. Test coverage

| Layer | Tests | Notes |
|---|---|---|
| Overlay / audio / stealth | 19 | flags, badges, click-through, frame size, tail padding, `aclose`, affinity mocks |
| Config, transcript, API/SSE | 23 | env precedence, JWT expiry, speaker labels, candidate-before ordering, SSE parsing, 401→refresh→retry, gated path + key prefix |
| `/events` + `/audio` sockets | 9 | real local `websockets` servers: heartbeat, resubscribe, 4401, replay gap, closed/failed/timeout |
| Controller | 24 | rendering, speaker from events (no refetch), gated auto-answer only for interviewer, skipped handling, question selection, answer success/failure+retry, end→debrief→memory, audio restart, end-on-quit, memory actions, source validation |
| Live smoke | 1 | opt-in; run 3× against the real server (degraded, mic+stub, loopback+gated answer) |

```
QT_QPA_PLATFORM=offscreen pytest -q
# 75 passed, 1 skipped
```

---

## 6. Recommended order

1. Server: audio-format negotiation (`session.status_changed` or `/audio-config`).
2. ~~Client: interactive OIDC login~~ — implemented as the portal handoff
   (`docs/desktop-auth-handoff.md`); the IdP redirect allowlist is the remaining
   deployment step.
3. Client: drive the debrief UI from `debrief.status_changed`.
4. Client: implement loopback capture, then set `DESKTOP_AUDIO_SOURCE=loopback` for real interviewer audio.
5. Run the live smoke with real provider keys and the §10.6 stealth matrix.
