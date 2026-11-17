import json

import httpx
import pytest

from desktop_client.api import ApiClient, ApiError, parse_sse_stream
from desktop_client.config import Settings


def make_settings(**overrides):
    values = {
        "api_base_url": "http://backend.test",
        "access_token": "jwt-token",
    }
    values.update(overrides)
    return Settings(**values)


def sse_body(events):
    frames = []
    for event_type, payload in events:
        frames.append(f"event: {event_type}\n")
        frames.append("id: abc\n")
        frames.append(f"data: {json.dumps(payload, separators=(',', ':'))}\n\n")
    return "".join(frames).encode()


@pytest.mark.asyncio
async def test_answer_stream_parses_tokens_and_done():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        captured["idempotency"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        body = sse_body(
            [
                (
                    "assistant.answer_token",
                    {
                        "payload": {
                            "token_index": 0,
                            "answer_text_delta": "Hello ",
                        }
                    },
                ),
                (
                    "assistant.answer_token",
                    {
                        "payload": {
                            "token_index": 1,
                            "answer_text_delta": "world.",
                        }
                    },
                ),
                (
                    "assistant.answer_done",
                    {
                        "payload": {
                            "turn_id": "a1",
                            "answer_status": "ok",
                            "validated_object": {"answer_text": "Hello world."},
                        }
                    },
                ),
            ]
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    client = ApiClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        tokens = []
        done = None
        async for event_type, envelope in client.answer("s1", "q1", "c1"):
            payload = envelope["payload"]
            if event_type == "assistant.answer_token":
                tokens.append(payload["answer_text_delta"])
            elif event_type == "assistant.answer_done":
                done = payload
    finally:
        await client.aclose()

    assert captured["path"] == "/api/v1/sessions/s1/practice/answer"
    assert captured["auth"] == "Bearer jwt-token"
    assert captured["idempotency"] == "answer-q1"
    assert captured["body"] == {"question_turn_id": "q1", "candidate_turn_id": "c1"}
    assert "".join(tokens) == "Hello world."
    assert done["validated_object"]["answer_text"] == "Hello world."


@pytest.mark.asyncio
async def test_answer_stream_raises_structured_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"detail": {"code": "llm_transient", "retryable": True, "message": "busy"}},
        )

    client = ApiClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as excinfo:
            async for _ in client.answer("s1", "q1"):
                pass
    finally:
        await client.aclose()

    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "llm_transient"
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_debrief_status_fetch_and_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/debrief/status"):
            return httpx.Response(200, json={"status": "writing", "retryable": False})
        if request.url.path.endswith("/debrief/retry"):
            return httpx.Response(200, json={"status": "writing", "retryable": False})
        if request.url.path.endswith("/debrief"):
            return httpx.Response(
                200,
                json={
                    "session_id": "s1",
                    "status": "ready",
                    "debrief": {"summary": "Good", "score": 4},
                },
            )
        raise AssertionError(f"unexpected {request.url}")

    client = ApiClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        assert (await client.debrief_status("s1"))["status"] == "writing"
        assert (await client.debrief("s1"))["debrief"]["score"] == 4
        assert (await client.retry_debrief("s1"))["status"] == "writing"
    finally:
        await client.aclose()

    assert calls == [
        ("GET", "/api/v1/sessions/s1/debrief/status"),
        ("GET", "/api/v1/sessions/s1/debrief"),
        ("POST", "/api/v1/sessions/s1/debrief/retry"),
    ]


@pytest.mark.asyncio
async def test_debrief_404_maps_to_api_error():
    client = ApiClient(
        make_settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                404, json={"detail": {"code": "debrief_no_content", "status": "no_content"}}
            )
        ),
    )
    try:
        with pytest.raises(ApiError) as excinfo:
            await client.debrief("s1")
    finally:
        await client.aclose()
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "debrief_no_content"


@pytest.mark.asyncio
async def test_memory_endpoints():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/retry"):
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(200, json={"items": [{"id": "m1"}], "total": 1})

    client = ApiClient(make_settings(), transport=httpx.MockTransport(handler))
    try:
        listing = await client.list_memory()
        assert listing["total"] == 1
        assert await client.delete_memory("m1") is None
        assert await client.clear_memory() is None
        await client.retry_memory("s1")
    finally:
        await client.aclose()

    assert ("DELETE", "/api/v1/memory/m1", "") in seen
    assert ("DELETE", "/api/v1/memory", "") in seen
    assert ("POST", "/api/v1/memory/retry", "session_id=s1") in seen


@pytest.mark.asyncio
async def test_parse_sse_ignores_comments_and_malformed_frames():
    async def lines():
        for line in [
            ": keep-alive",
            "event: ping",
            'data: {"ok":true}',
            "",
            "data: not-json",
            "",
        ]:
            yield line

    frames = [frame async for frame in parse_sse_stream(lines())]
    assert frames == [("ping", {"ok": True})]
