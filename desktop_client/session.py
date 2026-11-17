"""Live session orchestration.

Owns the ordered lifecycle from the plan:

1. acceptable-use version -> preflight -> create/ack session
2. ``/events`` socket first, then the ``/audio`` socket
3. render transcript partials/finals, then answer the current question
4. on Stop: ``client.audio_end`` -> wait for ``audio.closed`` -> end session
5. poll the debrief, fetch it, then show the memory summary

Everything runs on the qasync loop, so overlay methods are called directly.
Long calls (transcript refresh, answers, debrief polling) run as tasks so they
never stall the event socket reader.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from desktop_client.api import ApiClient, ApiError
from desktop_client.audio import mic_frames
from desktop_client.audio_ws import AudioOutcome, AudioSender
from desktop_client.config import Settings
from desktop_client.events import (
    DEBRIEF_STATUS_CHANGED,
    SESSION_STATUS_CHANGED,
    SYSTEM_ERROR,
    ARTIFACT_EXTRACTED,
    TRANSCRIPT_DELTA,
    TRANSCRIPT_ERROR,
    TRANSCRIPT_FINAL,
    EventClient,
    ServerEvent,
)
from desktop_client.transcript import TranscriptStore

logger = logging.getLogger(__name__)

DEBRIEF_POLL_SECONDS = 2.0
DEBRIEF_TIMEOUT_SECONDS = 120.0
AUDIO_FINALIZE_WAIT_SECONDS = 25.0


class SessionError(Exception):
    """A fatal, user-visible session setup problem."""


class LiveSessionController:
    def __init__(
        self,
        overlay,
        settings: Settings,
        *,
        api: Optional[ApiClient] = None,
    ):
        self.overlay = overlay
        self.settings = settings
        self.api = api or ApiClient(settings)

        self.session_id: Optional[str] = None
        self.transcript = TranscriptStore()

        self._events: Optional[EventClient] = None
        self._events_task: Optional[asyncio.Task] = None
        self._audio_task: Optional[asyncio.Task] = None
        self._answer_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._events_ready = asyncio.Event()
        self._closing = False
        self._stopping = False
        self.audio_outcome: Optional[AudioOutcome] = None
        self._answered_turns: set = set()
        self._retry_handler = None

        self.overlay.retry_requested.connect(self._on_retry_clicked)

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if not self.settings.configured:
            raise SessionError("DESKTOP_ACCESS_TOKEN is required for a live session")

        session = await self._ensure_session()
        self.session_id = session["id"]
        self.overlay.set_status("connecting")
        self.overlay.set_capture_state(
            (session.get("capture_status") or {}).get("mic", "inactive")
        )

        self._events = EventClient(
            self.settings,
            self.session_id,
            self._on_event,
            on_replay_gap=self._on_replay_gap,
            on_state_change=self._on_events_state,
        )
        self._events_task = asyncio.create_task(
            self._events.run(), name="events"
        )
        self._audio_task = asyncio.create_task(self._run_audio(), name="audio")

    async def stop(self) -> None:
        """Graceful stop: flush audio, then end the session and fetch debrief."""
        if self._stopping:
            return
        self._stopping = True
        self.overlay.set_status("stopping")
        self.overlay.set_partial("Flushing audio\u2026")
        self._stop_event.set()

        if self._audio_task is not None:
            try:
                await asyncio.wait_for(self._audio_task, AUDIO_FINALIZE_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("Audio task did not finish in time")
                self._audio_task.cancel()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Audio task failed")

        if self._events is not None:
            await self._events.stop()

        await self._end_and_debrief()

    async def shutdown(self) -> None:
        """Cancel everything. Safe to call multiple times."""
        if self._closing:
            return
        self._closing = True
        self._stop_event.set()

        for task in (self._answer_task, self._audio_task, self._events_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self.api is not None:
            await self.api.aclose()

    async def close(self) -> None:
        await self.shutdown()

    # ----------------------------------------------------------- session setup
    async def _ensure_session(self) -> Dict[str, Any]:
        try:
            policy = await self.api.acceptable_use()
        except ApiError as exc:
            raise SessionError(f"Could not read acceptable-use policy: {exc}") from exc

        version = policy.get("version")
        if not version:
            raise SessionError("Backend did not return an acceptable-use version")

        if self.settings.session_id:
            session = await self.api.get_session(self.settings.session_id)
            if session.get("status") == "pending" and session.get("kind") == "live":
                try:
                    await self.api.acknowledge(session["id"], version)
                except ApiError as exc:
                    raise SessionError(
                        f"Could not acknowledge policy {version}: {exc}"
                    ) from exc
                session = await self.api.get_session(session["id"])
            if session.get("status") != "active":
                raise SessionError(
                    f"Session {session['id']} is '{session.get('status')}', not active"
                )
            return session

        preflight = await self.api.preflight(
            kind="live",
            acknowledged_policy_version=version,
            goal_id=self.settings.goal_id or None,
        )
        if not preflight.get("can_start", False):
            blockers = "; ".join(
                issue.get("message", issue.get("code", "blocker"))
                for issue in preflight.get("blockers", [])
            )
            raise SessionError(f"Preflight blocked live session: {blockers}")

        try:
            return await self.api.create_session(
                kind="live",
                acknowledged_policy_version=version,
                goal_id=self.settings.goal_id or None,
                idempotency_key=f"desktop-{version}",
            )
        except ApiError as exc:
            if exc.status_code != 409:
                raise SessionError(f"Could not create live session: {exc}") from exc
            # Another session is already pending/active; adopt it instead of failing.
            session = await self.api.active_session()
            if not session:
                raise SessionError(f"Could not create live session: {exc}") from exc
            if session.get("status") == "pending" and session.get("kind") == "live":
                await self.api.acknowledge(session["id"], version)
                session = await self.api.get_session(session["id"])
            return session

    # ------------------------------------------------------------------- audio
    async def _run_audio(self) -> None:
        try:
            await asyncio.wait_for(self._events_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("Events socket not ready; starting audio anyway")

        sender = AudioSender(
            self.settings,
            self.session_id or "",
            on_state_change=self._on_audio_state,
        )
        frames = mic_frames(
            allow_simulation_fallback=self.settings.simulate_audio,
            stop_event=self._stop_event,
        )
        try:
            self.audio_outcome = await sender.run(frames)
        finally:
            await frames.aclose()

        outcome = self.audio_outcome
        if outcome is None:
            return
        if outcome.status == "failed":
            self.overlay.set_status("audio failed")
            self.overlay.set_partial(
                f"Audio finalization failed ({outcome.error_class or 'error'})"
            )
        elif outcome.status == "closed":
            self.overlay.set_status("stopped")
            self.overlay.set_partial("Audio closed by backend.")

    # ------------------------------------------------------------------ events
    async def _on_event(self, event: ServerEvent) -> None:
        payload = event.payload
        kind = event.event_type

        if kind == SESSION_STATUS_CHANGED:
            capture = payload.get("capture_status") or {}
            if isinstance(capture, dict) and "mic" in capture:
                self.overlay.set_capture_state(str(capture["mic"]))
            status = payload.get("new_status")
            if status:
                self.overlay.set_status(str(status))

        elif kind == TRANSCRIPT_DELTA:
            self.transcript.apply_delta(payload)
            self.overlay.set_partial(self.transcript.partial_text)

        elif kind == TRANSCRIPT_FINAL:
            turn = self.transcript.apply_final(payload)
            if turn is not None:
                self._render_transcript()
                asyncio.create_task(self._refresh_speakers())
                if self.settings.auto_answer:
                    self.answer_latest()

        elif kind == TRANSCRIPT_ERROR:
            message = payload.get("message") or "transcription failed"
            retryable = bool(payload.get("retryable", False))
            self.overlay.set_answer_error(
                f"{message} ({payload.get('error_class', 'error')})", retryable
            )

        elif kind == ARTIFACT_EXTRACTED:
            title = payload.get("title") or payload.get("artifact_type")
            if title:
                self.overlay.set_partial(f"artifact: {title}")

        elif kind == DEBRIEF_STATUS_CHANGED:
            status = payload.get("status")
            if status:
                logger.info("Debrief status changed: %s", status)

        elif kind == SYSTEM_ERROR:
            code = payload.get("code") or payload.get("error_class") or "system error"
            self.overlay.set_partial(f"backend error: {code}")
            if not payload.get("retryable", False):
                self.overlay.set_status("degraded")

    async def _on_replay_gap(self, event: ServerEvent) -> None:
        logger.warning("Replay gap: %s", event.payload)
        self.overlay.set_partial("Resyncing transcript\u2026")
        await self._refresh_transcript()

    def _on_events_state(self, state: str) -> None:
        if state == "connected":
            self._events_ready.set()
            self.overlay.set_status("listening")
        elif state == "reconnecting":
            self.overlay.set_status("reconnecting")
        elif state == "unauthorized":
            self.overlay.set_status("unauthorized")
            self.overlay.set_partial("Auth rejected; check DESKTOP_ACCESS_TOKEN")

    def _on_audio_state(self, state: str) -> None:
        if state == "streaming":
            self.overlay.set_status("listening")
        elif state == "finalizing":
            self.overlay.set_status("finalizing")

    # -------------------------------------------------------------- transcript
    async def _refresh_speakers(self) -> None:
        """Pull the authoritative transcript so turns get real speaker labels."""
        await self._refresh_transcript()

    async def _refresh_transcript(self) -> None:
        if not self.session_id:
            return
        try:
            turns = await self.api.transcript(self.session_id)
        except ApiError as exc:
            logger.warning("Transcript refresh failed: %s", exc)
            return
        if not isinstance(turns, list):
            return
        if self.transcript.merge_authoritative(turns):
            self._render_transcript()

    def _render_transcript(self) -> None:
        self.overlay.set_turns(self.transcript.rendered_turns())
        self.overlay.set_partial(self.transcript.partial_text)

    # ----------------------------------------------------------------- answers
    def answer_latest(self) -> None:
        turn_id = self.transcript.latest_turn_id
        if not turn_id or turn_id in self._answered_turns:
            return
        self._start_answer(turn_id, self.transcript.candidate_turn_id())

    def _start_answer(self, question_turn_id: str, candidate_turn_id: Optional[str]) -> None:
        if self._answer_task is not None and not self._answer_task.done():
            self._answer_task.cancel()
        self._answer_task = asyncio.create_task(
            self._stream_answer(question_turn_id, candidate_turn_id),
            name="answer",
        )

    async def _stream_answer(self, question_turn_id: str, candidate_turn_id: Optional[str]) -> None:
        self.overlay.set_answer_loading()
        accumulated = []
        try:
            async for event_type, envelope in self.api.answer(
                self.session_id or "", question_turn_id, candidate_turn_id
            ):
                payload = envelope.get("payload", envelope)
                if event_type == "assistant.answer_token":
                    delta = str(payload.get("answer_text_delta") or "")
                    if delta:
                        accumulated.append(delta)
                        self.overlay.append_answer_token(delta)
                elif event_type == "assistant.answer_done":
                    validated = payload.get("validated_object") or {}
                    final_text = validated.get("answer_text") or "".join(accumulated)
                    self.overlay.set_answer(final_text)
                    self._answered_turns.add(question_turn_id)
                    self._retry_handler = None
                    self.overlay.retry_btn.hide()
                elif event_type == "assistant.answer_failed":
                    message = str(
                        payload.get("partial_text")
                        or payload.get("error_class")
                        or "answer failed"
                    )
                    self._set_retry(
                        bool(payload.get("retryable", False)),
                        lambda: self._start_answer(question_turn_id, candidate_turn_id),
                    )
                    self.overlay.set_answer_error(
                        message, bool(payload.get("retryable", False))
                    )
        except asyncio.CancelledError:
            raise
        except ApiError as exc:
            self._set_retry(
                exc.retryable,
                lambda: self._start_answer(question_turn_id, candidate_turn_id),
            )
            self.overlay.set_answer_error(f"{exc.code}: {exc.message}", exc.retryable)

    def _set_retry(self, retryable: bool, handler) -> None:
        self._retry_handler = handler if retryable else None

    def _on_retry_clicked(self) -> None:
        handler = self._retry_handler
        if handler is None:
            self.overlay.retry_btn.hide()
            return
        self.overlay.retry_btn.hide()
        handler()

    # ------------------------------------------------------ end / debrief / memory
    async def _end_and_debrief(self) -> None:
        if not self.session_id:
            return
        try:
            await self.api.end_session(
                self.session_id, idempotency_key=f"end-{self.session_id}"
            )
        except ApiError as exc:
            logger.warning("Session end failed: %s", exc)
            self.overlay.set_partial(f"End failed: {exc}")
            return

        status = await self._poll_debrief_status()
        if status == "ready":
            await self._show_debrief()
        elif status in {"no_content", "generate_failed", "unavailable"}:
            self.overlay.set_answer(
                f"Debrief: {status.replace('_', ' ')}.", title="debrief"
            )
            if status == "generate_failed":
                self._set_retry(
                    True, lambda: asyncio.create_task(self._retry_debrief())
                )
                self.overlay.retry_btn.show()

        await self._show_memory_summary()

    async def _retry_debrief(self) -> None:
        try:
            await self.api.retry_debrief(self.session_id or "")
        except ApiError as exc:
            self.overlay.set_answer(
                f"Debrief retry failed: {exc.message}", title="debrief"
            )
            return
        status = await self._poll_debrief_status()
        if status == "ready":
            await self._show_debrief()
        else:
            self.overlay.set_answer(
                f"Debrief: {status.replace('_', ' ')}.", title="debrief"
            )

    async def _poll_debrief_status(self) -> str:
        waited = 0.0
        last = "unavailable"
        while waited < DEBRIEF_TIMEOUT_SECONDS:
            try:
                data = await self.api.debrief_status(self.session_id or "")
            except ApiError as exc:
                logger.warning("Debrief status failed: %s", exc)
                return "unavailable"
            last = str(data.get("status") or "unavailable")
            if last != "writing":
                return last
            await asyncio.sleep(DEBRIEF_POLL_SECONDS)
            waited += DEBRIEF_POLL_SECONDS
        return last

    async def _show_debrief(self) -> None:
        try:
            data = await self.api.debrief(self.session_id or "")
        except ApiError as exc:
            self.overlay.set_answer(f"Debrief unavailable: {exc.message}", title="debrief")
            return

        detail = data.get("debrief") or data
        summary = detail.get("summary") or ""
        strengths = detail.get("strengths") or []
        gaps = detail.get("areas_for_improvement") or []
        score = detail.get("score")

        lines = []
        if score is not None:
            lines.append(f"Score: {score}/5")
        if summary:
            lines.append(summary)
        if strengths:
            lines.append("Strengths: " + "; ".join(map(str, strengths)))
        if gaps:
            lines.append("Improve: " + "; ".join(map(str, gaps)))
        self.overlay.set_answer(
            "\n\n".join(lines) or "Debrief ready (no summary text).", title="debrief"
        )

    async def _show_memory_summary(self) -> None:
        try:
            data = await self.api.list_memory()
        except ApiError as exc:
            logger.info("Memory list unavailable: %s", exc)
            return
        items = data.get("items") or []
        total = data.get("total", len(items))
        self.overlay.set_partial(f"memory: {total} item(s)")
