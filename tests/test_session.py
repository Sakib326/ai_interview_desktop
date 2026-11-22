import asyncio

import pytest

from desktop_client.config import Settings
from desktop_client.events import (
    SESSION_STATUS_CHANGED,
    SYSTEM_REPLAY_GAP,
    TRANSCRIPT_DELTA,
    TRANSCRIPT_FINAL,
    ServerEvent,
)
from desktop_client.overlay import OverlayWindow
from desktop_client.session import LiveSessionController, SessionError


class FakeApi:
    """In-memory stand-in for ApiClient."""

    def __init__(self):
        self.version = "2026-11"
        self.session = {
            "id": "s1",
            "status": "active",
            "kind": "live",
            "capture_status": {"mic": "inactive"},
        }
        self.preflight_result = {"can_start": True, "blockers": []}
        self.created = None
        self.acknowledged = []
        self.transcript_turns = []
        self.transcript_calls = 0
        self.answer_events = []
        self.answer_calls = []
        self.debrief_status_value = "ready"
        self.debrief_payload = {
            "session_id": "s1",
            "status": "ready",
            "debrief": {
                "summary": "Solid structure.",
                "score": 4,
                "strengths": ["clarity"],
                "areas_for_improvement": ["metrics"],
            },
        }
        self.memories = {"items": [{}, {}], "total": 2}
        self.ended = 0
        self.retried_debrief = 0
        self.cleared_memory = 0
        self.retried_memory = 0
        self.refreshed = 0
        self.closed = False
        self.artifact_calls = []
        self.artifact_answer_calls = []
        self.artifact_answer_events = [
            (
                "assistant.answer_token",
                {"payload": {"answer_text_delta": "def solve():"}},
            ),
            (
                "assistant.answer_done",
                {
                    "payload": {
                        "validated_object": {
                            "answer_text": "def solve():\n    return 42",
                            "artifact_kind": "code",
                        }
                    }
                },
            ),
        ]
        self.artifact_result = {
            "artifact_id": "a1",
            "artifact_type": "code",
            "content": "def solve():\n    return 42",
            "confidence": 0.0,
            "source_mode": "manual",
        }

    async def refresh_token(self):
        self.refreshed += 1
        return False

    async def ensure_fresh_token(self, within_seconds=120.0, force=False):
        self.refreshed += 1
        return False

    async def acceptable_use(self):
        return {"version": self.version}

    async def preflight(self, kind="live", acknowledged_policy_version=None, goal_id=None):
        return self.preflight_result

    async def create_session(self, kind="live", acknowledged_policy_version=None,
                             goal_id=None, idempotency_key=None):
        self.created = {
            "kind": kind,
            "ack": acknowledged_policy_version,
            "key": idempotency_key,
        }
        return self.session

    async def get_session(self, session_id):
        return self.session

    async def active_session(self):
        return self.session

    async def acknowledge(self, session_id, policy_version):
        self.acknowledged.append(policy_version)
        self.session = {**self.session, "status": "active"}
        return self.session

    async def transcript(self, session_id):
        self.transcript_calls += 1
        return self.transcript_turns

    async def solve_artifact(self, session_id, *, image_bytes=None,
                             extracted_text=None, auto_capture=False):
        self.artifact_calls.append({
            "session_id": session_id,
            "bytes": len(image_bytes or b""),
            "text": extracted_text,
            "auto": auto_capture,
        })
        if isinstance(self.artifact_result, Exception):
            raise self.artifact_result
        return self.artifact_result

    async def answer_artifact(self, session_id, artifact_id):
        self.artifact_answer_calls.append((session_id, artifact_id))
        for event in self.artifact_answer_events:
            yield event

    async def answer(self, session_id, question_turn_id, candidate_turn_id=None,
                     *, gated=False):
        self.answer_calls.append((question_turn_id, candidate_turn_id, gated))
        for event in self.answer_events:
            yield event

    async def end_session(self, session_id, idempotency_key=None):
        self.ended += 1
        return {"id": session_id, "status": "ended"}

    async def debrief_status(self, session_id):
        return {"status": self.debrief_status_value, "retryable": False}

    async def debrief(self, session_id):
        return self.debrief_payload

    async def retry_debrief(self, session_id):
        self.retried_debrief += 1
        return {"status": "writing"}

    async def list_memory(self, limit=50, offset=0):
        return self.memories

    async def clear_memory(self):
        self.cleared_memory += 1

    async def retry_memory(self, session_id):
        self.retried_memory += 1

    async def aclose(self):
        self.closed = True


def make_controller(qapp, api=None, **settings_overrides):
    settings = Settings(
        api_base_url="http://backend.test",
        access_token="token",
        **settings_overrides,
    )
    overlay = OverlayWindow()
    controller = LiveSessionController(overlay, settings, api=api or FakeApi())
    return controller, overlay


def event(event_type, payload, sequence=1):
    return ServerEvent(event_type, payload, sequence, f"e{sequence}", {})


@pytest.mark.asyncio
async def test_render_partial_and_final_then_speaker_refresh(qapp):
    api = FakeApi()
    api.transcript_turns = [
        {
            "id": "t1",
            "text": "Tell me about payments.",
            "sequence_number": 1,
            "speaker": "interviewer",
        }
    ]
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._on_event(
        event(TRANSCRIPT_DELTA, {"text": "Tell me", "segment_id": "seg1"}, 2)
    )
    assert overlay.partial_label.text() == "Tell me"

    await controller._on_event(
        event(
            TRANSCRIPT_FINAL,
            {
                "text": "Tell me about payments.",
                "turn_id": "t1",
                "segment_id": "seg1",
                "sequence_number": 1,
            },
            3,
        )
    )
    assert overlay.partial_label.text() == "Listening for speech…"

    await asyncio.sleep(0.05)
    assert overlay.transcript_edit.toPlainText() == (
        "Interviewer: Tell me about payments."
    )


@pytest.mark.asyncio
async def test_duplicate_final_is_not_rendered_twice(qapp):
    controller, overlay = make_controller(qapp)
    controller.session_id = "s1"
    payload = {
        "text": "One.",
        "turn_id": "t1",
        "segment_id": "seg1",
        "sequence_number": 1,
    }
    await controller._on_event(event(TRANSCRIPT_FINAL, payload, 2))
    await controller._on_event(event(TRANSCRIPT_FINAL, payload, 2))
    await asyncio.sleep(0.05)
    assert overlay.transcript_edit.toPlainText().count("One.") == 1


@pytest.mark.asyncio
async def test_status_event_updates_capture_state(qapp):
    controller, overlay = make_controller(qapp)
    await controller._on_event(
        event(
            SESSION_STATUS_CHANGED,
            {
                "old_status": "active",
                "new_status": "ended",
                "capture_status": {"mic": "inactive"},
            },
        )
    )
    assert overlay.status_label.text() == "ended"


@pytest.mark.asyncio
async def test_replay_gap_resyncs_transcript(qapp):
    api = FakeApi()
    api.transcript_turns = [
        {"id": "t1", "text": "recovered", "sequence_number": 1, "speaker": "candidate"}
    ]
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"
    await controller._on_replay_gap(
        event(SYSTEM_REPLAY_GAP, {"oldest_available_sequence": 5})
    )
    assert "recovered" in overlay.transcript_edit.toPlainText()


@pytest.mark.asyncio
async def test_answer_stream_renders_tokens_and_final_text(qapp):
    api = FakeApi()
    api.answer_events = [
        ("assistant.answer_token", {"payload": {"answer_text_delta": "Use STAR."}}),
        (
            "assistant.answer_done",
            {
                "payload": {
                    "turn_id": "a1",
                    "validated_object": {"answer_text": "Use STAR. Then metrics."},
                }
            },
        ),
    ]
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    controller._start_answer("q1", "c1")
    await controller._answer_task

    assert api.answer_calls == [("q1", "c1", False)]
    assert overlay.answer_edit.toPlainText() == "Use STAR. Then metrics."
    assert overlay.retry_btn.isHidden()


@pytest.mark.asyncio
async def test_answer_failure_exposes_retry(qapp):
    api = FakeApi()
    api.answer_events = [
        (
            "assistant.answer_failed",
            {
                "payload": {
                    "answer_status": "failed",
                    "error_class": "LLMProviderTransientError",
                    "retryable": True,
                    "partial_text": None,
                }
            },
        )
    ]
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    controller._start_answer("q1", None)
    await controller._answer_task

    assert "LLMProviderTransientError" in overlay.answer_edit.toPlainText()
    assert not overlay.retry_btn.isHidden()
    # Retry re-issues the same request.
    controller._on_retry_clicked()
    await controller._answer_task
    assert len(api.answer_calls) == 2


@pytest.mark.asyncio
async def test_end_then_debrief_and_memory(qapp):
    api = FakeApi()
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._end_and_debrief()

    assert api.ended == 1
    text = overlay.answer_edit.toPlainText()
    assert "Score: 4/5" in text
    assert "Solid structure." in text
    assert "memory: 2 item(s)" in overlay.partial_label.text()


@pytest.mark.asyncio
async def test_generate_failed_debrief_offers_retry(qapp):
    api = FakeApi()
    api.debrief_status_value = "generate_failed"
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._end_and_debrief()
    assert "Debrief: generate failed." in overlay.answer_edit.toPlainText()
    assert not overlay.retry_btn.isHidden()

    controller._on_retry_clicked()
    await asyncio.sleep(0.05)
    assert api.retried_debrief == 1


@pytest.mark.asyncio
async def test_ensure_session_reuses_and_acknowledges_pending(qapp):
    api = FakeApi()
    api.session = {**api.session, "status": "pending"}
    controller, _ = make_controller(qapp, api, session_id="s1")

    session = await controller._ensure_session()

    assert session["status"] == "active"
    assert api.acknowledged == ["2026-11"]
    assert api.created is None


@pytest.mark.asyncio
async def test_ensure_session_creates_with_policy_version(qapp):
    api = FakeApi()
    controller, _ = make_controller(qapp, api)

    await controller._ensure_session()

    assert api.created["kind"] == "live"
    assert api.created["ack"] == "2026-11"
    # The backend caches create responses per (user, key) for the process
    # lifetime, so the key must not be reused across launches.
    assert api.created["key"].startswith("desktop-")
    assert api.created["key"] != "desktop-2026-11"


@pytest.mark.asyncio
async def test_create_idempotency_key_is_unique_per_controller(qapp):
    api_one = FakeApi()
    api_two = FakeApi()
    first, _ = make_controller(qapp, api_one)
    second, _ = make_controller(qapp, api_two)

    await first._ensure_session()
    await second._ensure_session()

    assert api_one.created["key"] != api_two.created["key"]


@pytest.mark.asyncio
async def test_ensure_session_raises_on_preflight_blockers(qapp):
    api = FakeApi()
    api.preflight_result = {
        "can_start": False,
        "blockers": [{"code": "policy", "message": "ack required"}],
    }
    controller, _ = make_controller(qapp, api)

    with pytest.raises(SessionError) as excinfo:
        await controller._ensure_session()
    assert "ack required" in str(excinfo.value)


@pytest.mark.asyncio
async def test_preflight_applies_model_and_surfaces_warnings(qapp):
    api = FakeApi()
    api.preflight_result = {
        "can_start": True,
        "blockers": [],
        "warnings": [
            {"code": "headphone_check_recommended", "message": "Use headphones"}
        ],
        "policies": {
            "providers": {
                "llm": {"answer_generation": {"model": "openai/gpt-4o-mini"}}
            }
        },
    }
    controller, overlay = make_controller(qapp, api)

    await controller._ensure_session()

    assert controller.preflight_warnings == ["Use headphones"]
    assert overlay.model_label.text() == "openai/gpt-4o-mini"

    # The warning is shown once the socket is up, before any transcript text.
    controller._on_events_state("connected")
    assert overlay.partial_label.text() == "Use headphones"


@pytest.mark.asyncio
async def test_shutdown_ends_an_active_session(qapp):
    api = FakeApi()
    controller, _ = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller.shutdown()

    assert api.ended == 1
    assert api.closed is True


@pytest.mark.asyncio
async def test_shutdown_does_not_end_a_reused_session(qapp):
    api = FakeApi()
    controller, _ = make_controller(qapp, api, session_id="s1")
    controller.session_id = "s1"

    await controller.shutdown()

    # Reusing someone else's/pre-existing session must not end it on quit.
    assert api.ended == 0


@pytest.mark.asyncio
async def test_audio_restarts_once_after_disconnect(qapp, monkeypatch):
    import desktop_client.session as session_module
    from desktop_client.audio_ws import AudioOutcome

    api = FakeApi()
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"
    controller._events_ready.set()

    calls = []

    class _Sender:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, frames):
            calls.append(1)
            if len(calls) == 1:
                return AudioOutcome("disconnected", message="drop")
            return AudioOutcome("closed")

    async def _frames(**kwargs):
        if False:  # pragma: no cover - empty async generator
            yield b""

    monkeypatch.setattr(session_module, "AudioSender", _Sender)
    monkeypatch.setattr(session_module, "mic_frames", _frames)
    monkeypatch.setattr(session_module, "AUDIO_RETRY_DELAY_SECONDS", 0)

    await controller._run_audio()

    assert len(calls) == 2
    assert overlay.status_label.text() == "stopped"


@pytest.mark.asyncio
async def test_audio_does_not_restart_after_user_stop(qapp, monkeypatch):
    import desktop_client.session as session_module
    from desktop_client.audio_ws import AudioOutcome

    api = FakeApi()
    controller, _ = make_controller(qapp, api)
    controller.session_id = "s1"
    controller._events_ready.set()
    controller._stop_event.set()

    calls = []

    class _Sender:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, frames):
            calls.append(1)
            return AudioOutcome("disconnected")

    async def _frames(**kwargs):
        if False:  # pragma: no cover
            yield b""

    monkeypatch.setattr(session_module, "AudioSender", _Sender)
    monkeypatch.setattr(session_module, "mic_frames", _frames)

    await controller._run_audio()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_audio_uses_configured_source(qapp, monkeypatch):
    import desktop_client.session as session_module
    from desktop_client.audio_ws import AudioOutcome

    api = FakeApi()
    controller, _ = make_controller(qapp, api, audio_source="loopback")
    controller.session_id = "s1"
    controller._events_ready.set()

    captured = {}

    class _Sender:
        def __init__(self, settings, session_id, source=None, on_state_change=None):
            captured["sender_source"] = settings.audio_source
            captured["sender_channel"] = source

        async def run(self, frames):
            return AudioOutcome("closed")

    def _frames(*args, **kwargs):
        captured["frame_source"] = kwargs.get("source")

        async def _gen():
            if False:  # pragma: no cover - empty async generator
                yield b""

        return _gen()

    monkeypatch.setattr(session_module, "AudioSender", _Sender)
    monkeypatch.setattr(session_module, "mic_frames", _frames)

    await controller._run_audio()

    assert captured["sender_source"] == "loopback"
    assert captured["sender_channel"] == "loopback"
    assert captured["frame_source"] == "loopback"


@pytest.mark.asyncio
async def test_transcript_error_renders_as_capture_status(qapp):
    from desktop_client.events import TRANSCRIPT_ERROR

    controller, overlay = make_controller(qapp)
    await controller._on_event(
        event(
            TRANSCRIPT_ERROR,
            {
                "error_class": "STTProviderTransientError",
                "retryable": True,
                "message": "transcription failed",
            },
        )
    )
    assert overlay.status_label.text() == "transcription error"
    assert "STTProviderTransientError" in overlay.partial_label.text()


@pytest.mark.asyncio
async def test_memory_actions(qapp):
    api = FakeApi()
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller.forget_all_memory()
    assert api.cleared_memory == 1
    assert overlay.partial_label.text() == "memory erased"

    await controller.retry_memory()
    assert api.retried_memory == 1
    assert overlay.partial_label.text() == "memory extraction queued"


@pytest.mark.asyncio
async def test_final_speaker_is_used_without_transcript_refetch(qapp):
    api = FakeApi()
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._on_event(
        event(
            TRANSCRIPT_FINAL,
            {
                "text": "Tell me about payments.",
                "turn_id": "t1",
                "segment_id": "seg1",
                "sequence_number": 1,
                "speaker": "interviewer",
            },
        )
    )
    await asyncio.sleep(0)

    assert overlay.transcript_edit.toPlainText() == (
        "Interviewer: Tell me about payments."
    )
    assert api.transcript_calls == 0


@pytest.mark.asyncio
async def test_auto_answer_is_gated_and_ignores_candidate_turns(qapp):
    api = FakeApi()
    api.answer_events = [
        (
            "assistant.answer_done",
            {"payload": {"turn_id": "a1", "validated_object": {"answer_text": "Ok."}}},
        )
    ]
    controller, overlay = make_controller(
        qapp, api, auto_answer=True, auto_answer_delay_seconds=0.01
    )
    controller.session_id = "s1"

    candidate_final = {
        "text": "I led payments.",
        "turn_id": "c1",
        "segment_id": "seg-c",
        "sequence_number": 1,
        "speaker": "candidate",
    }
    await controller._on_event(event(TRANSCRIPT_FINAL, candidate_final))
    await asyncio.sleep(0.05)
    assert api.answer_calls == []

    interviewer_final = {
        "text": "Why did you leave?",
        "turn_id": "q1",
        "segment_id": "seg-q",
        "sequence_number": 2,
        "speaker": "interviewer",
    }
    await controller._on_event(event(TRANSCRIPT_FINAL, interviewer_final))
    await asyncio.sleep(0.05)
    await controller._answer_task

    assert api.answer_calls == [("q1", "c1", True)]
    assert overlay.answer_edit.toPlainText() == "Ok."


@pytest.mark.asyncio
async def test_answer_skipped_is_not_an_error(qapp):
    api = FakeApi()
    api.answer_events = [
        (
            "assistant.answer_skipped",
            {
                "payload": {
                    "turn_id": "q1",
                    "answer_status": "skipped",
                    "decision": "silent",
                    "intent": "filler",
                    "reason": "greeting_or_filler",
                }
            },
        )
    ]
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    controller._start_answer("q1", None, gated=True)
    await controller._answer_task

    assert overlay.answer_edit.isHidden()
    assert overlay.partial_label.text() == "answer skipped: greeting_or_filler"
    assert overlay.retry_btn.isHidden()


@pytest.mark.asyncio
async def test_manual_answer_prefers_interviewer_turn_and_is_ungated(qapp):
    api = FakeApi()
    api.answer_events = [
        ("assistant.answer_done", {"payload": {"validated_object": {"answer_text": "A"}}})
    ]
    controller, _ = make_controller(qapp, api)
    controller.session_id = "s1"

    for payload in (
        {"turn_id": "c1", "text": "answer", "sequence_number": 1, "speaker": "candidate"},
        {"turn_id": "q1", "text": "question", "sequence_number": 2, "speaker": "interviewer"},
        {"turn_id": "c2", "text": "answer 2", "sequence_number": 3, "speaker": "candidate"},
    ):
        await controller._on_event(event(TRANSCRIPT_FINAL, payload))
    await asyncio.sleep(0)

    controller.answer_latest()
    await controller._answer_task

    assert api.answer_calls == [("q1", "c1", False)]


@pytest.mark.asyncio
async def test_start_rejects_unknown_audio_source(qapp):
    controller, _ = make_controller(qapp, audio_source="screen")
    with pytest.raises(SessionError):
        await controller.start()


@pytest.mark.asyncio
async def test_capture_artifact_posts_screen_and_renders_content(qapp, monkeypatch):
    from desktop_client import session as session_module

    monkeypatch.setattr(session_module, "grab_screen_png", lambda *a, **k: b"\x89PNG fake")

    api = FakeApi()
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._capture_artifact()

    assert api.artifact_calls == [
        {"session_id": "s1", "bytes": 9, "text": None, "auto": False}
    ]
    assert api.artifact_answer_calls == [("s1", "a1")]
    assert overlay.answer_edit.toPlainText() == "def solve():\n    return 42"
    assert overlay.answer_header.text() == "answer (code)"
    assert "artifact: code" in overlay.partial_label.text()


@pytest.mark.asyncio
async def test_capture_artifact_renders_vision_solution_without_second_call(
    qapp, monkeypatch
):
    """A vision capture carries its solution; no /answer stream is opened."""
    from desktop_client import session as session_module

    monkeypatch.setattr(session_module, "grab_screen_png", lambda *a, **k: b"\x89PNG fake")

    api = FakeApi()
    api.artifact_result = {
        "artifact_id": "a1",
        "artifact_type": "problem_statement",
        "content": "Given nums, return the sum.",
        "confidence": 0.93,
        "source_mode": "manual",
        "extraction_method": "vision",
        "solution_text": "Use sum(nums) - O(n) time.",
    }
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._capture_artifact()

    # One round trip: the solution from /solve is rendered directly.
    assert api.artifact_answer_calls == []
    assert overlay.answer_edit.toPlainText() == "Use sum(nums) - O(n) time."
    assert overlay.answer_header.text() == "answer (problem_statement)"
    # The partial line must not keep saying "answering..." once the answer is up.
    assert overlay.partial_label.text() == "artifact: problem_statement"


@pytest.mark.asyncio
async def test_capture_artifact_falls_back_to_answer_stream_without_solution(
    qapp, monkeypatch
):
    """OCR-only captures have no stored solution, so /answer still streams."""
    from desktop_client import session as session_module

    monkeypatch.setattr(session_module, "grab_screen_png", lambda *a, **k: b"\x89PNG fake")

    api = FakeApi()
    api.artifact_result = {
        "artifact_id": "a1",
        "artifact_type": "code",
        "content": "def solve():",
        "confidence": 0.0,
        "source_mode": "manual",
        "extraction_method": "ocr",
        "solution_text": None,
    }
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._capture_artifact()

    assert api.artifact_answer_calls == [("s1", "a1")]
    assert overlay.answer_edit.toPlainText() == "def solve():\n    return 42"


@pytest.mark.asyncio
async def test_capture_artifact_reports_capture_failure(qapp, monkeypatch):
    from desktop_client import session as session_module
    from desktop_client.capture import CaptureError

    def _boom(*args, **kwargs):
        raise CaptureError("no screen")

    monkeypatch.setattr(session_module, "grab_screen_png", _boom)

    api = FakeApi()
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._capture_artifact()

    assert api.artifact_calls == []
    assert "Screen capture failed" in overlay.partial_label.text()


@pytest.mark.asyncio
async def test_capture_artifact_surfaces_api_error(qapp, monkeypatch):
    from desktop_client import session as session_module
    from desktop_client.api import ApiError

    monkeypatch.setattr(session_module, "grab_screen_png", lambda *a, **k: b"png")

    api = FakeApi()
    api.artifact_result = ApiError(
        503, "ocr_unavailable", "Tesseract missing", retryable=False
    )
    controller, overlay = make_controller(qapp, api)
    controller.session_id = "s1"

    await controller._capture_artifact()

    assert len(api.artifact_calls) == 1
    assert "ocr_unavailable" in overlay.partial_label.text()


@pytest.mark.asyncio
async def test_capture_artifact_without_session_does_nothing(qapp, monkeypatch):
    from desktop_client import session as session_module

    called = []
    monkeypatch.setattr(
        session_module, "grab_screen_png", lambda *a, **k: called.append(1) or b"png"
    )

    api = FakeApi()
    controller, overlay = make_controller(qapp, api)
    controller.capture_artifact()

    assert called == []
    assert api.artifact_calls == []
    assert "No session" in overlay.partial_label.text()
