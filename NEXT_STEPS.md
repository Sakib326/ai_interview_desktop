# Next Steps — Desktop Client

**Current state:** the client is fully wired to the backend contract.
Overlay, 50 ms mic capture, stealth exclusion, `/events`, `/audio`, SSE answers,
session end, debrief retrieval/retry, memory summary, Windows global hotkeys,
and a tray menu are implemented.

**Verified:** `75 passed, 1 skipped` with `QT_QPA_PLATFORM=offscreen pytest -q`.
Socket tests run against real local WebSocket servers; HTTP tests use
`httpx.MockTransport`. `python -m desktop_client.main --simulate-audio` prints
`captured 1600 bytes`.

**Live smoke verified against the real backend** (disposable migrated DB, dev
token from `tools/generate_dev_token.py`), three runs:

1. Degraded (no provider keys): lifecycle + `stt_provider_unavailable` +
   `debrief no_content`.
2. `mic` + `stub_stt`: final delivered with `speaker=candidate`, overlay shows
   `You: …`, no transcript refetch.
3. `loopback` + gated auto-answer: overlay shows `Interviewer: …`, `/live/answer`
   runs the Turn Gate, and the backend persists an `assistant` turn plus
   `session.answer_generated`.

Re-run with `DESKTOP_LIVE_API` / `DESKTOP_LIVE_TOKEN` (and optionally
`DESKTOP_LIVE_SOURCE=loopback`, `DESKTOP_LIVE_AUTO_ANSWER=1`) plus
`pytest -q -s tests/test_live_smoke.py`.

**No backend files were modified.**

---

## Done

- [x] `__pycache__` and `desktop_client.egg-info` untracked; `.gitignore` covers
      both plus `.commandcode/`.
- [x] `IMPLEMENTATION.md`, `initialize-plan.md`, `.env.example` tracked.
- [x] `.env` local; `.env.example` documents every setting.
- [x] `/events` client: bearer subprotocol, initial status, heartbeat (20 s),
      reconnect with backoff, `client.resubscribe`, `system.replay_gap` ->
      transcript refetch, terminal close codes 4401/4403/4404.
- [x] `/audio` client: 1,600-byte binary frames, `client.audio_end`,
      `audio.closed` / `audio.failed` / disconnect / timeout outcomes.
- [x] Transcript rendering: partials, finals, dedup by turn/segment, ordering,
      speaker labels via the authoritative transcript.
- [x] Live answers: SSE token rendering, final from `validated_object`, retry on
      retryable failures, `Idempotency-Key: answer-<turn_id>`.
- [x] End → debrief status poll → fetch → retry → memory summary.
- [x] Live prerequisites: acceptable-use version, preflight blockers,
      create-with-ack, adopt/ack an existing pending session.
- [x] Global hotkeys (F8/F9/F10) and tray menu — click-through is no longer a
      one-way door.
- [x] Stop drains the capture queue instead of dropping up to 2 s of audio.
- [x] Integration tests for events, audio, SSE, debrief, memory.
- [x] Token refresh: ahead of expiry, on HTTP 401, and once on a 4401 WS
      rejection; rotating refresh token kept in memory.
- [x] Audio restarts once on a retryable `audio.failed`/dropped socket.
- [x] Quit/Close ends the session; a reused `DESKTOP_SESSION_ID` is left alone.
- [x] Preflight warnings surfaced; model label from backend config.
- [x] Tray memory actions: erase-all (confirmed) and extraction retry.
- [x] `transcript_error` reported as a capture-status error, not an answer error.
- [x] Create idempotency key is per launch (backend caches per process).
- [x] `?source=mic|loopback` honored and validated; `loopback` labels turns as
      `interviewer`.
- [x] Speaker taken from transcript events; per-final rest refetch removed
      (fallback kept for a `speaker`-less backend).
- [x] Auto-answer only for `interviewer` finals, via gated `/live/answer`;
      `assistant.answer_skipped` handled as a non-error.
- [x] Question = newest interviewer turn; candidate context = newest candidate
      turn before it.
- [x] Stop waits for the last answer before ending the session.

### Still client-side

- [x] Interactive login: environment token -> stored refresh token -> browser
      portal handoff. Spec: `docs/desktop-auth-handoff.md`; change record:
      `docs/desktop-auth-changes.md`. The desktop never talks to the IdP token
      endpoint and never reads the portal's session cookie.
- [ ] Drive the debrief UI from `debrief.status_changed` instead of polling.
- [ ] Loopback/system-audio capture (the source tag exists; the capture path
      does not).

---

## Backend (separate repo; interactive login implemented in this pass)

The three blocking items are **resolved**: `/audio?source=`, speaker on
transcript events, and Turn Gate behind `POST /sessions/{id}/live/answer`.
One item remains open:

1. **Audio format exposure.** Publish the frozen format on
   `session.status_changed` or add `GET /sessions/{id}/audio-config`. Today the
   client assumes PCM S16LE mono 16 kHz 50 ms.
2. **Desktop interactive login — implemented.** Backend record:
   `E:\ai_interview\docs\desktop-auth-handoff-changes.md`. Added
   `GET /web/desktop-login`, `POST /api/v1/auth/handoff`, the one-time code
   store, `AuthService.issue_for_user`, and the loopback redirect rule in
   `settings.is_registered_redirect_uri()`. Verified with
   `backend/tests/evaluation/auth/test_handoff.py` (25 passed, 2 skipped with
   the existing auth suites).

### Desktop interactive login — still open

- [ ] **IdP redirect allowlist (deployment, not code).** The backend now accepts
      ephemeral loopback ports, but the identity provider must also allow the
      desktop redirect (exact URI or a loopback rule for native clients).
      Without it the browser reaches the IdP and is rejected.
- [ ] **Web session durability.** `/web/auth/callback` still discards the
      rotating refresh token and `device_id` (`backend/web/auth/routes.py`), so
      the web session itself cannot refresh.
- [ ] **`/authorize` deviations.** JSON response instead of `302`, in-memory
      state store, and a required `nonce`; unchanged, the desktop follows the
      JSON contract.
- [ ] **Device flow.** RFC 8628 remains an alternative; not implemented.
- [ ] **Backend restart during handoff.** Process-local `_HANDOFF_CODES`;
      Redis is required only if `WORKER_COUNT` is ever raised.

### Open questions (for the backend owner)

**Diarization — optional, not required.** Verified against `assemblyai>=1.10.11`
(the backend pin; inspected 1.10.11): streaming reports speaker at **both**
levels — `TurnEvent.speaker_label` (turn level) and `Word.speaker` (per word) —
and additionally emits `SpeakerRevisionEvent` with revised
`speaker_label`/`words` per `turn_order` after an offline recluster.
`_normalize` reads only the turn-level label and has no `SpeakerRevision`
handling.

Source tagging was chosen instead, so this only matters if diarization is
revived:
- Does `assemblyai-stt-3-7-realtime-agent` populate `speaker_label` when
  `speaker_labels=true`, and are revisions enabled by default
  (`speaker_labels_revision_interval_ms`)? Needs one real stream to confirm.
- If adopted: handle per-word speakers and `SpeakerRevision`, and keep
  `Turn.speaker == "candidate"` working for `/practice/answer`.

---

## Remaining — hardware / release

1. **Windows stealth matrix (§10.6).** Teams (window + full screen), Zoom, Meet
   in Chrome, OBS WGC + BitBlt, Snipping Tool (24H2+), Xbox Game Bar, Discord.
   Confirm the badge matches behavior in every cell, and that exclusion
   survives a click-through round-trip, hide/show, and a restart.
   `FAILED` on the primary target is a ship blocker.
2. **Real-device capture check.** This environment has no PortAudio; verify a
   real microphone on the target machine.
3. **Loopback capture.** Not implemented (mic only). Required before interviewer
   questions can be transcribed without headphones/room audio.
4. **Packaging.** PyInstaller one-folder build is in place
   (`packaging/build.ps1` → `dist\overlay\overlay.exe`, neutral name per
   `initialize-plan.md` §10.4; verified: prints `captured 1600 bytes` and stays
   running). Remaining: Windows installer and code signing.
5. **Live smoke with provider keys.** The HTTP/WS lifecycle is verified; the
   transcription → answer path still needs `ASSEMBLYAI_API_KEY` /
   `OPENROUTER_API_KEY` (or a config switch to `stub_stt` + a deterministic
   LLM) to exercise real finals and answers end to end.
6. **Repo placement.** Decide whether this stays separate or moves into
   `ai-interview` (the approved plan lives at
   `ai-interview/docs/desktop-client-plan.md`).

---

## Known limitations

- Stealth is Windows-only, best-effort, and untested on the target hardware.
- Transcript events carry no speaker; finals are assumed candidate and
  relabeled from the REST transcript.
- Auto-answer is off by default because of the speaker gap; F10 answers the
  latest turn deliberately.
- `/audio` format is an assumption, not a negotiated contract.
- No TTS; the overlay stays display-only.
- Raw audio is never persisted by the client; only the backend's transcript
  turns exist.
- `client.ack` is deliberately unused — it has no sequencing effect.
