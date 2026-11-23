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
import os
import uuid
from typing import Any, Dict, Optional

from desktop_client.api import ApiClient, ApiError
from desktop_client.audio import SAMPLE_RATE, mic_frames
from desktop_client.audio_ws import AudioOutcome, AudioSender
from desktop_client.capture import CaptureError, grab_screen_png
from desktop_client.config import Settings, audio_source_channels
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
from desktop_client.moonshine_stt import (
    MoonshineCallbacks,
    MoonshineTranscriberSession,
    MoonshineUnavailableError,
)
from desktop_client.transcript import SPEAKER_CANDIDATE, SPEAKER_INTERVIEWER, TranscriptStore

logger = logging.getLogger(__name__)

DEBRIEF_POLL_SECONDS = 2.0
DEBRIEF_TIMEOUT_SECONDS = 120.0
AUDIO_FINALIZE_WAIT_SECONDS = 25.0
AUDIO_RETRY_DELAY_SECONDS = 1.0
AUTO_ANSWER_DELAY_SECONDS = 1.5  # fallback when settings value is unavailable


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
        self._token_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._events_ready = asyncio.Event()
        self._closing = False
        self._stopping = False
        self._ended = False
        self._active_stop_task: Optional[asyncio.Task] = None
        self.preflight_warnings: list = []
        self.audio_outcome: Optional[AudioOutcome] = None
        self._answered_turns: set = set()
        self._retry_handler = None
        self._pending_auto_answer: Optional[asyncio.TimerHandle] = None
        self._moonshine_partials: Dict[str, str] = {}
        self._moonshine_tasks: list[asyncio.Task] = []
        self._ingest_tasks: set[asyncio.Task] = set()
        # Backend caches create responses per (user, key) for the process
        # lifetime, so the key must be unique per launch attempt. A constant key
        # would replay the first session of the process (already ended) forever.
        self._create_idempotency_key = f"desktop-{uuid.uuid4()}"

        self.overlay.retry_requested.connect(self._on_retry_clicked)

    @property
    def answered_turn_ids(self) -> set:
        """Question turns already answered or explicitly skipped by the gate."""
        return set(self._answered_turns)

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if not self.settings.configured:
            raise SessionError("DESKTOP_ACCESS_TOKEN is required for a live session")
        try:
            audio_source_channels(self.settings.audio_source)
        except ValueError as exc:
            raise SessionError(str(exc)) from exc

        await self._ensure_stt_ready()

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
            on_auth_expired=self.api.refresh_token,
        )
        self._events_task = asyncio.create_task(
            self._events.run(), name="events"
        )
        self._audio_task = asyncio.create_task(self._run_audio(), name="audio")
        self._token_task = asyncio.create_task(
            self._maintain_token(), name="token-refresh"
        )

    async def stop(self) -> None:
        """Graceful stop: flush audio, then end the session and fetch debrief."""
        if self._stopping:
            return
        self._stopping = True
        self._cancel_pending_auto_answer()
        self._active_stop_task = asyncio.current_task()
        try:
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

            # A final emitted during the audio flush can start an answer. Let it
            # finish before ending the session, or the answer races the end (409).
            if self.settings.auto_answer and self._answer_task is None:
                deadline = asyncio.get_running_loop().time() + 3.0
                while (
                    self._answer_task is None
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.05)
            if self._answer_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._answer_task), 20.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    logger.exception("Last answer failed during stop")

            # Keep /events open through session end so the terminal
            # session.status_changed and debrief.status_changed still arrive.
            await self._end_and_debrief()

            if self._events is not None:
                await self._events.stop()
        finally:
            if self._active_stop_task is asyncio.current_task():
                self._active_stop_task = None

    async def shutdown(self) -> None:
        """Cancel everything. Safe to call multiple times."""
        if self._closing:
            return
        self._closing = True
        self._stop_event.set()
        self._cancel_pending_auto_answer()

        # Closing the window is a cancellation, not a Stop: audio is not
        # flushed. Still end the session so it does not stay 'active' and get
        # adopted by the next launch.
        if (
            self.session_id
            and not self._ended
            and not self._stopping
            and not self.settings.session_id
        ):
            try:
                await asyncio.wait_for(
                    self.api.end_session(
                        self.session_id,
                        idempotency_key=f"end-{self.session_id}",
                    ),
                    5.0,
                )
                self._ended = True
            except (asyncio.TimeoutError, ApiError) as exc:
                logger.warning("Session end on shutdown failed: %s", exc)

        for task in (
            self._active_stop_task,
            self._answer_task,
            self._audio_task,
            self._events_task,
            self._token_task,
            *self._moonshine_tasks,
            *list(self._ingest_tasks),
        ):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self.api is not None:
            await self.api.aclose()

    async def _maintain_token(self) -> None:
        """Refresh the access token shortly before it expires."""
        while True:
            await asyncio.sleep(30.0)
            try:
                await self.api.ensure_fresh_token(within_seconds=90.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Token maintenance failed: %s", exc)

    async def close(self) -> None:
        await self.shutdown()

    # ----------------------------------------------------------- stt / ingest
    async def _ensure_stt_ready(self) -> None:
        """Validate local STT dependencies and backend ingest capability."""
        if self.settings.stt_provider != "moonshine":
            return
        try:
            from desktop_client.moonshine_stt import _import_moonshine  # noqa: F401

            _import_moonshine()
        except MoonshineUnavailableError as exc:
            raise SessionError(str(exc)) from exc

        if not self.settings.configured:
            return
        # Probe the endpoint against a non-existent session. A 404 with "Session
        # not found" means the route exists and ran its ownership check. A 404/405
        # indicating the route/path is missing means the backend lacks support.
        try:
            await self.api.ingest_transcript_turn(
                "__capability_probe__",
                speaker=SPEAKER_CANDIDATE,
                text="capability probe",
                segment_id="capability-probe",
                idempotency_key="capability-probe",
            )
        except ApiError as exc:
            detail = (exc.message or "").lower()
            code = (exc.code or "").lower()
            route_missing = exc.status_code == 405 or code in {
                "not_found",
                "method_not_allowed",
            }
            session_probe_hit = "session not found" in detail or "session does not belong" in detail
            if exc.status_code == 404 and not session_probe_hit:
                route_missing = True
            if route_missing:
                raise SessionError(
                    "DESKTOP_STT_PROVIDER=moonshine requires backend support for "
                    "POST /sessions/{id}/transcript/turns. The running backend does "
                    "not expose it; keep DESKTOP_STT_PROVIDER=backend or update the "
                    "backend."
                ) from exc
            # 401/403/409/422/5xx on a fake session means the route exists but the
            # probe payload/session is not acceptable; that's enough to proceed.
            logger.info(
                "Moonshine ingest capability probe reached backend (%s: %s)",
                exc.status_code,
                exc.code,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Moonshine ingest capability probe failed: %s", exc)

    # ----------------------------------------------------------- session setup
    async def _ensure_session(self) -> Dict[str, Any]:
        try:
            policy = await self.api.acceptable_use()
        except ApiError as exc:
            raise SessionError(f"Could not read acceptable-use policy: {exc}") from exc

        version = policy.get("version")
        if not version:
            raise SessionError("Backend did not return an acceptable-use version")

        # Preflight is also the only pre-session source of the resolved provider
        # model and non-blocking warnings, so run it in both paths.
        try:
            preflight = await self.api.preflight(
                kind="live",
                acknowledged_policy_version=version,
                goal_id=self.settings.goal_id or None,
            )
        except ApiError as exc:
            if not self.settings.session_id:
                raise SessionError(f"Preflight failed: {exc}") from exc
            preflight = {}
        self._apply_preflight(preflight)

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
                idempotency_key=self._create_idempotency_key,
            )
        except ApiError as exc:
            if exc.status_code != 409:
                raise SessionError(f"Could not create live session: {exc}") from exc
            # Another session is already pending/active; only adopt a compatible
            # live session. Adopting a practice session would silently attach the
            # live overlay to the wrong session kind.
            session = await self.api.active_session()
            if not session:
                raise SessionError(f"Could not create live session: {exc}") from exc
            if session.get("kind") != "live":
                raise SessionError(
                    f"Cannot start live session: active {session.get('kind')} "
                    f"session {session.get('id')} exists. End it before starting a "
                    "live session."
                )
            if session.get("status") == "pending":
                await self.api.acknowledge(session["id"], version)
                session = await self.api.get_session(session["id"])
            if session.get("status") != "active":
                raise SessionError(
                    f"Cannot adopt session {session.get('id')}: status is "
                    f"'{session.get('status')}', not active"
                )
            return session

    def _apply_preflight(self, preflight: Dict[str, Any]) -> None:
        """Take the resolved model and non-blocking warnings from preflight."""
        warnings = [
            str(issue.get("message") or issue.get("code"))
            for issue in preflight.get("warnings", [])
            if isinstance(issue, dict)
        ]
        self.preflight_warnings = warnings
        for message in warnings:
            logger.warning("Preflight warning: %s", message)

        providers = (preflight.get("policies") or {}).get("providers") or {}
        llm = providers.get("llm") or {}
        answer = llm.get("answer_generation") or {}
        model = answer.get("model") or (llm.get("llm") or {}).get("model")
        if model:
            self.overlay.set_model(str(model))

    # ------------------------------------------------------------------- audio
    async def _run_audio(self) -> None:
        try:
            await asyncio.wait_for(self._events_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("Events socket not ready; starting audio anyway")

        try:
            sources = audio_source_channels(self.settings.audio_source)
        except ValueError as exc:
            logger.error("Audio configuration error: %s", exc)
            self.overlay.set_status("audio failed")
            self.overlay.set_partial(str(exc))
            return

        if self.settings.stt_provider == "moonshine":
            await self._run_moonshine_audio(sources)
            return

        if len(sources) == 1:
            try:
                outcome = await self._run_audio_channel(sources[0])
            except Exception as exc:
                logger.exception("Audio channel %s crashed", sources[0])
                outcome = AudioOutcome("failed", message=str(exc))
            self.audio_outcome = outcome
            self._apply_single_audio_outcome(outcome)
            return

        task_map = {
            asyncio.create_task(self._run_audio_channel(s), name=f"audio-{s}"): s
            for s in sources
        }
        outcomes: Dict[str, AudioOutcome] = {}
        pending = set(task_map.keys())
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    source = task_map[task]
                    try:
                        outcomes[source] = task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        msg = str(exc)
                        if isinstance(exc, RuntimeError) and "not available" in msg.lower():
                            logger.warning(
                                "Audio channel %s unavailable: %s", source, msg
                            )
                        else:
                            logger.exception("Audio channel %s crashed", source)
                        outcomes[source] = AudioOutcome("failed", message=msg)
                if pending and not self._stop_event.is_set():
                    self._report_partial_audio(outcomes)
        except asyncio.CancelledError:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise

        self.audio_outcome = outcomes
        self._apply_multi_audio_outcome(outcomes)

    async def _run_audio_channel(self, source: str) -> AudioOutcome:
        attempt = 0
        while True:
            sender = AudioSender(
                self.settings,
                self.session_id or "",
                source=source,
                on_state_change=lambda state, s=source: self._on_audio_state(state, s),
            )
            frames = mic_frames(
                source=source,
                allow_simulation_fallback=self.settings.simulate_audio,
                stop_event=self._stop_event,
            )
            try:
                outcome = await sender.run(frames)
            finally:
                await frames.aclose()

            if outcome is None:
                outcome = AudioOutcome("disconnected", message="no outcome")

            retryable = (
                outcome.status == "disconnected"
                or (outcome.status == "failed" and outcome.retryable)
            )
            if not retryable or attempt >= 1 or self._stop_event.is_set():
                return outcome

            attempt += 1
            logger.warning(
                "Restarting %s audio stream after %s", source, outcome.status
            )
            self.overlay.set_status("reconnecting audio")
            await self.api.ensure_fresh_token(force=True)
            await asyncio.sleep(AUDIO_RETRY_DELAY_SECONDS)

    async def _run_moonshine_audio(self, sources: list[str]) -> None:
        callbacks = MoonshineCallbacks(
            on_partial=self._on_moonshine_partial,
            on_final=self._on_moonshine_final,
            on_error=self._on_moonshine_error,
        )
        task_map = {
            asyncio.create_task(
                self._run_moonshine_channel(source, callbacks),
                name=f"moonshine-{source}",
            ): source
            for source in sources
        }
        self._moonshine_tasks = list(task_map.keys())
        outcomes: Dict[str, AudioOutcome] = {}
        pending = set(task_map.keys())
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    source = task_map[task]
                    try:
                        outcomes[source] = task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception("Moonshine channel %s crashed", source)
                        outcomes[source] = AudioOutcome("failed", message=str(exc))
                if pending and not self._stop_event.is_set():
                    self._report_partial_audio(outcomes)
        except asyncio.CancelledError:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise
        finally:
            self._moonshine_tasks = []

        self.audio_outcome = outcomes
        self._apply_multi_audio_outcome(outcomes)

    async def _run_moonshine_channel(
        self, source: str, callbacks: MoonshineCallbacks
    ) -> AudioOutcome:
        transcriber = MoonshineTranscriberSession(
            source=source,
            sample_rate=SAMPLE_RATE,
            model_path=self.settings.moonshine_model,
            language=self.settings.moonshine_language,
            callbacks=callbacks,
        )
        frames = mic_frames(
            source=source,
            allow_simulation_fallback=self.settings.simulate_audio,
            stop_event=self._stop_event,
        )
        fed_frames = 0
        fed_bytes = 0
        last_log = asyncio.get_running_loop().time()
        rms_last = 0.0
        rms_max = 0.0
        debug_dump_path = os.getenv("DESKTOP_DEBUG_AUDIO_DUMP", "").strip()
        debug_dump_file = None
        debug_dump_bytes = 0
        debug_dump_limit = int(os.getenv("DESKTOP_DEBUG_AUDIO_DUMP_SECONDS", "15")) * SAMPLE_RATE * 2
        try:
            self._on_audio_state("streaming", source)
            await transcriber.start()
            async for frame in frames:
                if not frame:
                    continue
                await transcriber.feed(frame)
                fed_frames += 1
                fed_bytes += len(frame)
                # Quick RMS for diagnosing silence/wrong device.
                try:
                    from array import array as _array

                    samples = _array("h")
                    samples.frombytes(bytes(frame))
                    if samples:
                        mean_sq = sum(s * s for s in samples) / len(samples)
                        rms_last = mean_sq ** 0.5
                        if rms_last > rms_max:
                            rms_max = rms_last
                except Exception:
                    pass
                if debug_dump_path and debug_dump_bytes < debug_dump_limit:
                    try:
                        import wave as _wave

                        if debug_dump_file is None:
                            os.makedirs(os.path.dirname(debug_dump_path) or ".", exist_ok=True)
                            debug_dump_file = _wave.open(debug_dump_path, "wb")
                            debug_dump_file.setnchannels(1)
                            debug_dump_file.setsampwidth(2)
                            debug_dump_file.setframerate(SAMPLE_RATE)
                            logger.info("[moonshine:%s] dumping debug audio to %s", source, debug_dump_path)
                        debug_dump_file.writeframes(bytes(frame))
                        debug_dump_bytes += len(frame)
                        if debug_dump_bytes >= debug_dump_limit:
                            debug_dump_file.close()
                            debug_dump_file = None
                            logger.info("[moonshine:%s] debug audio dump complete: %s", source, debug_dump_path)
                    except Exception:
                        logger.exception("[moonshine:%s] debug audio dump failed", source)
                        debug_dump_path = ""
                now = asyncio.get_running_loop().time()
                if now - last_log >= 5.0:
                    logger.info(
                        "[moonshine:%s] fed frames=%d bytes=%d (%.1fs audio) rms_last=%.1f rms_max=%.1f state=%s",
                        source, fed_frames, fed_bytes, fed_frames * 0.05,
                        rms_last, rms_max, transcriber.debug_state(),
                    )
                    last_log = now
            self._on_audio_state("finalizing", source)
            await transcriber.stop()
            if self._stop_event.is_set():
                return AudioOutcome("closed")
            return AudioOutcome("disconnected", message="capture ended")
        except asyncio.CancelledError:
            try:
                await transcriber.stop()
            except Exception:
                logger.debug("Moonshine stop during cancel failed", exc_info=True)
            raise
        except MoonshineUnavailableError as exc:
            logger.error("Moonshine unavailable for %s: %s", source, exc)
            return AudioOutcome("failed", message=str(exc))
        except Exception as exc:
            logger.exception("Moonshine channel %s failed", source)
            return AudioOutcome("failed", message=str(exc))
        finally:
            try:
                if debug_dump_file is not None:
                    debug_dump_file.close()
            except Exception:
                pass
            await frames.aclose()
            logger.info(
                "[moonshine:%s] channel finished fed_frames=%d fed_bytes=%d rms_max=%.1f",
                source, fed_frames, fed_bytes, rms_max,
            )

    def _on_moonshine_partial(self, source: str, segment_id: str, text: str) -> None:
        if self._stopping or self._stop_event.is_set():
            return
        logger.info("[moonshine:%s] partial seg=%s text=%r", source, segment_id, text[:80])
        self._moonshine_partials[f"{source}:{segment_id}"] = text
        # Keep the overlay partial simple: show the most recent partial.
        self.overlay.set_partial(text)

    def _on_moonshine_error(self, source: str, exc: Exception) -> None:
        logger.warning("[moonshine:%s] error: %s", source, exc)
        self.overlay.set_partial(f"local STT ({source}) error: {exc}")

    def _on_moonshine_final(
        self, source: str, segment_id: str, text: str, confidence: Optional[float]
    ) -> None:
        if self._stopping or self._stop_event.is_set():
            return
        logger.info(
            "[moonshine:%s] final seg=%s conf=%s text=%r",
            source, segment_id, confidence, text[:120],
        )
        self._moonshine_partials.pop(f"{source}:{segment_id}", None)
        speaker = SPEAKER_INTERVIEWER if source == "loopback" else SPEAKER_CANDIDATE
        task = asyncio.create_task(
            self._ingest_moonshine_final(source, speaker, segment_id, text, confidence),
            name=f"ingest-{source}-{segment_id}",
        )
        self._ingest_tasks.add(task)
        task.add_done_callback(self._ingest_tasks.discard)

    async def _ingest_moonshine_final(
        self,
        source: str,
        speaker: str,
        segment_id: str,
        text: str,
        confidence: Optional[float],
    ) -> None:
        if not self.session_id:
            return
        try:
            result = await self.api.ingest_transcript_turn(
                self.session_id,
                speaker=speaker,
                text=text,
                segment_id=segment_id,
                confidence=confidence,
                idempotency_key=f"moonshine-{self.session_id}-{segment_id}",
            )
        except ApiError as exc:
            logger.warning("Moonshine transcript ingest failed: %s", exc)
            self.overlay.set_status("transcription error")
            self.overlay.set_partial(f"Local STT ingest failed: {exc.code}")
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Moonshine transcript ingest crashed")
            self.overlay.set_status("transcription error")
            self.overlay.set_partial(f"Local STT ingest failed: {exc}")
            return

        if result.get("duplicate"):
            return

        payload = {
            "turn_id": result.get("turn_id"),
            "text": result.get("text") or text,
            "sequence_number": result.get("sequence_number"),
            "segment_id": result.get("segment_id") or segment_id,
            "speaker": result.get("speaker") or speaker,
        }
        turn = self.transcript.apply_final(payload)
        if turn is not None:
            self._render_transcript()
            if (
                self.settings.auto_answer
                and turn.speaker == SPEAKER_INTERVIEWER
            ):
                self._schedule_auto_answer(
                    turn.turn_id,
                    self.transcript.candidate_turn_id_before(turn.sequence_number),
                )

    def _apply_single_audio_outcome(self, outcome: Optional[AudioOutcome]) -> None:
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
        elif outcome.status == "disconnected":
            self.overlay.set_status("audio disconnected")
            self.overlay.set_partial(
                "Audio stream dropped; press Stop to end or restart the client."
            )

    def _report_partial_audio(self, outcomes: Dict[str, AudioOutcome]) -> None:
        problems = [
            f"{s}: {o.error_class or o.status}"
            for s, o in outcomes.items()
            if o.status in ("failed", "disconnected")
        ]
        if problems:
            self.overlay.set_status("listening")
            self.overlay.set_partial(
                "Audio issue (" + "; ".join(problems) + ") — other source active"
            )

    def _apply_multi_audio_outcome(self, outcomes: Dict[str, AudioOutcome]) -> None:
        failed = [
            f"{s}: {o.error_class or o.status or o.message or 'error'}"
            for s, o in outcomes.items()
            if o.status in ("failed", "disconnected")
        ]
        ok = [s for s, o in outcomes.items() if o.status in ("closed", "timeout")]
        if failed and ok:
            self.overlay.set_status("stopped" if self._stop_event.is_set() else "listening")
            msg = "Audio issue (" + "; ".join(failed) + ")"
            if not self._stop_event.is_set():
                msg += " — other source active"
            self.overlay.set_partial(msg)
            return
        if failed:
            self.overlay.set_status("audio failed")
            self.overlay.set_partial("Audio failed (" + "; ".join(failed) + ")")
            return
        if self._stop_event.is_set():
            self.overlay.set_status("stopped")
            label = "local STT" if self.settings.stt_provider == "moonshine" else "backend"
            self.overlay.set_partial(f"Audio closed ({label}, mic + system).")

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
                if not payload.get("speaker"):
                    # Older backends omit speaker; fall back to the transcript.
                    asyncio.create_task(self._refresh_speakers())
                if (
                    self.settings.auto_answer
                    and turn.speaker == SPEAKER_INTERVIEWER
                ):
                    # Debounce: wait briefly for more speech/finals so we don't
                    # answer mid-question when STT finalizes on a short pause.
                    self._schedule_auto_answer(
                        turn.turn_id,
                        self.transcript.candidate_turn_id_before(turn.sequence_number),
                    )

        elif kind == TRANSCRIPT_ERROR:
            message = payload.get("message") or "transcription failed"
            error_class = payload.get("error_class", "error")
            # This is a capture-path problem, not an answer failure.
            self.overlay.set_status("transcription error")
            self.overlay.set_partial(f"{message} ({error_class})")

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
            if self.preflight_warnings:
                self.overlay.set_partial(self.preflight_warnings[0])
        elif state == "reconnecting":
            self.overlay.set_status("reconnecting")
        elif state == "unauthorized":
            self.overlay.set_status("unauthorized")
            self.overlay.set_partial("Auth rejected; check DESKTOP_ACCESS_TOKEN")

    def _on_audio_state(self, state: str, source: str = "") -> None:
        if self._stopping or self._stop_event.is_set():
            if state == "finalizing":
                self.overlay.set_status("finalizing")
            return
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
        partial = self.transcript.partial_text
        if not partial and self._moonshine_partials:
            partial = next(reversed(self._moonshine_partials.values()))
        self.overlay.set_partial(partial)

    # ----------------------------------------------------------------- answers
    def _cancel_pending_auto_answer(self) -> None:
        if self._pending_auto_answer is not None:
            self._pending_auto_answer.cancel()
            self._pending_auto_answer = None

    def _schedule_auto_answer(
        self, question_turn_id: str, candidate_turn_id: Optional[str]
    ) -> None:
        """Delay auto-answer so a brief mid-speech pause doesn't trigger it."""
        self._cancel_pending_auto_answer()

        def _fire() -> None:
            self._pending_auto_answer = None
            if self._stopping or self._stop_event.is_set():
                return
            if question_turn_id in self._answered_turns:
                return
            self._start_answer(question_turn_id, candidate_turn_id, gated=True)

        delay = getattr(
            self.settings, "auto_answer_delay_seconds", AUTO_ANSWER_DELAY_SECONDS
        )
        self._pending_auto_answer = asyncio.get_running_loop().call_later(
            delay, _fire
        )

    def answer_latest(self) -> None:
        """Manual (F10) answer: prefer the interviewer's latest question."""
        turn = (
            self.transcript.latest_turn(SPEAKER_INTERVIEWER)
            or self.transcript.latest_turn()
        )
        if turn is None or turn.turn_id in self._answered_turns:
            return
        self._start_answer(
            turn.turn_id,
            self.transcript.candidate_turn_id_before(turn.sequence_number),
            gated=False,
        )

    def _start_answer(
        self,
        question_turn_id: str,
        candidate_turn_id: Optional[str],
        *,
        gated: bool = False,
    ) -> None:
        self._cancel_pending_auto_answer()
        if self._answer_task is not None and not self._answer_task.done():
            self._answer_task.cancel()
        self._answer_task = asyncio.create_task(
            self._stream_answer(question_turn_id, candidate_turn_id, gated),
            name="answer",
        )

    async def _stream_answer(
        self,
        question_turn_id: str,
        candidate_turn_id: Optional[str],
        gated: bool = False,
    ) -> None:
        self.overlay.set_answer_loading()
        accumulated = []
        try:
            async for event_type, envelope in self.api.answer(
                self.session_id or "",
                question_turn_id,
                candidate_turn_id,
                gated=gated,
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
                elif event_type == "assistant.answer_skipped":
                    # The Turn Gate declined: not an error, and nothing to show.
                    reason = payload.get("reason") or payload.get("decision") or "skipped"
                    self._answered_turns.add(question_turn_id)
                    self._retry_handler = None
                    self.overlay.clear_answer()
                    self.overlay.set_partial(f"answer skipped: {reason}")
                elif event_type == "assistant.answer_failed":
                    message = str(
                        payload.get("partial_text")
                        or payload.get("error_class")
                        or "answer failed"
                    )
                    self._set_retry(
                        bool(payload.get("retryable", False)),
                        lambda: self._start_answer(
                            question_turn_id, candidate_turn_id, gated=gated
                        ),
                    )
                    self.overlay.set_answer_error(
                        message, bool(payload.get("retryable", False))
                    )
        except asyncio.CancelledError:
            raise
        except ApiError as exc:
            self._set_retry(
                exc.retryable,
                lambda: self._start_answer(
                    question_turn_id, candidate_turn_id, gated=gated
                ),
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
            self._ended = True
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

    # ---------------------------------------------------------------- artifacts
    def capture_artifact(self) -> None:
        """Capture the screen and submit it as a session artifact (F11)."""
        if not self.session_id:
            self.overlay.set_partial("No session to capture an artifact for.")
            return
        asyncio.create_task(self._capture_artifact())

    async def _capture_artifact(self) -> None:
        try:
            image = grab_screen_png()
        except CaptureError as exc:
            logger.warning("Screen capture failed: %s", exc)
            self.overlay.set_partial(f"Screen capture failed: {exc}")
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected screen capture error")
            self.overlay.set_partial(f"Screen capture failed: {exc}")
            return

        self.overlay.set_partial("capturing screen\u2026")
        try:
            result = await self.api.solve_artifact(
                self.session_id, image_bytes=image, auto_capture=False
            )
        except ApiError as exc:
            logger.warning("Artifact submit failed: %s", exc)
            self.overlay.set_partial(f"artifact failed: {exc.code}")
            return

        artifact_id = result.get("artifact_id")
        artifact_type = result.get("artifact_type") or "artifact"
        self.overlay.set_partial(f"artifact: {artifact_type} \u2014 answering\u2026")

        # A vision read already produced the solution inside the /solve call,
        # so render it directly instead of opening a second stream for it.
        solution = (result.get("solution_text") or "").strip()
        if solution:
            self.overlay.set_answer(solution, title=f"answer ({artifact_type})")
            self.overlay.set_partial(f"artifact: {artifact_type}")
            return

        if not artifact_id:
            self.overlay.set_answer(
                result.get("content") or "", title=f"artifact ({artifact_type})"
            )
            return

        await self._stream_artifact_answer(artifact_id, artifact_type)

    async def _stream_artifact_answer(self, artifact_id: str, artifact_type: str) -> None:
        """Stream the LLM answer for a captured artifact into the answer pane."""
        self.overlay.set_answer_loading()
        self.overlay.answer_header.setText(f"answer ({artifact_type})")
        accumulated = []
        try:
            async for event_type, envelope in self.api.answer_artifact(
                self.session_id or "", artifact_id
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
                    self.overlay.set_answer(final_text, title=f"answer ({artifact_type})")
                    self.overlay.set_partial(f"artifact: {artifact_type}")
                elif event_type == "assistant.answer_failed":
                    message = str(
                        payload.get("error_class") or "answer failed"
                    )
                    self.overlay.set_answer_error(
                        f"{message}", bool(payload.get("retryable", False))
                    )
                    self.overlay.set_partial(f"artifact answer failed: {message}")
        except ApiError as exc:
            logger.warning("Artifact answer failed: %s", exc)
            self.overlay.set_answer_error(f"{exc.code}: {exc.message}", exc.retryable)
            self.overlay.set_partial(f"artifact answer failed: {exc.code}")

    # ------------------------------------------------------------ memory actions
    async def forget_all_memory(self) -> None:
        """Erase every stored memory item. Sessions and debriefs are untouched."""
        try:
            await self.api.clear_memory()
        except ApiError as exc:
            self.overlay.set_partial(f"Memory erase failed: {exc.message}")
            return
        self.overlay.set_partial("memory erased")

    async def retry_memory(self) -> None:
        """Re-enqueue extraction for the current session's ready debrief."""
        if not self.session_id:
            self.overlay.set_partial("No session to retry memory for.")
            return
        try:
            await self.api.retry_memory(self.session_id)
        except ApiError as exc:
            self.overlay.set_partial(f"Memory retry failed: {exc.message}")
            return
        self.overlay.set_partial("memory extraction queued")
