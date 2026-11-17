# Next Steps — Desktop Client

**Current state:** the client is fully wired to the backend contract.
Overlay, 50 ms mic capture, stealth exclusion, `/events`, `/audio`, SSE answers,
session end, debrief retrieval/retry, memory summary, Windows global hotkeys,
and a tray menu are implemented.

**Verified:** `56 passed, 1 skipped` with `QT_QPA_PLATFORM=offscreen pytest -q`.
Socket tests run against real local WebSocket servers; HTTP tests use
`httpx.MockTransport`. `python -m desktop_client.main --simulate-audio` prints
`captured 1600 bytes`.

**Live smoke verified against the real backend** (disposable migrated DB, dev
token from `tools/generate_dev_token.py`): preflight → session created
(`active`, policy `2026-11` acknowledged) → `/events` connected → `/audio`
reported `system.error stt_provider_unavailable` (no AssemblyAI key) → Stop →
session `ended` → debrief `no_content` (`total_turns: 0`) → memory listed.
Re-run with `DESKTOP_LIVE_API` / `DESKTOP_LIVE_TOKEN` and
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

---

## Remaining — backend (separate repo, not changed here)

These still block a fully reliable live-answer loop. All are handled
defensively client-side; none are patched locally.

1. **Source tagging on `/audio` (blocking for auto-answer).**
   `TranscriptService` persists finals as `speaker=segment.speaker or
   "candidate"`, and `/audio` has no source parameter. Interviewer speech is
   therefore stored as a candidate turn and cannot be a trustworthy
   `question_turn_id`. The client already sends `?source=mic`; the backend
   needs to accept and propagate it (and scope `(session_id, segment_id)`
   idempotency per source if loopback is added).
2. **Audio format exposure.** Publish the frozen format on
   `session.status_changed` or add `GET /sessions/{id}/audio-config`. Today the
   client assumes PCM S16LE mono 16 kHz 50 ms.
3. **`speaker` on transcript events.** Either add it to
   `speech.transcript_delta`/`final`, or accept the extra
   `GET /transcript` round-trip per final that the client currently performs.
4. **Turn Gate on the live path.** Confirm `POST /practice/answer` runs Turn
   Gate before generation.

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
4. **Packaging.** Decide PyInstaller vs uv; pick a neutral binary name
   (`initialize-plan.md` §10.4); installer + signing; document PortAudio.
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
