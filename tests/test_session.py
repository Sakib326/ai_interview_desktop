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
        self.closed = False

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
        return self.transcript_turns

    async def answer(self, session_id, question_turn_id, candidate_turn_id=None):
        self.answer_calls.append((question_turn_id, candidate_turn_id))
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
    assert overlay.partial_label.text() == ""

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

    assert api.answer_calls == [("q1", "c1")]
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
    assert api.created["key"] == "desktop-2026-11"


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
