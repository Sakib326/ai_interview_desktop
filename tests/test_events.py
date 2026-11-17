import asyncio
import json

import pytest

from desktop_client.config import Settings
from desktop_client.events import (
    EventClient,
    ServerEvent,
    TRANSCRIPT_DELTA,
    TRANSCRIPT_FINAL,
)


def make_settings(port: int) -> Settings:
    return Settings(
        api_base_url="http://127.0.0.1:1",
        ws_base_url=f"ws://127.0.0.1:{port}",
        access_token="jwt-token",
    )


@pytest.mark.asyncio
async def test_events_consumes_initial_status_and_transcript(ws_server_factory):
    headers = {}
    events = []
    seen_three = asyncio.Event()

    async def handler(ws):
        headers["protocol"] = ws.request.headers.get("Sec-WebSocket-Protocol")
        await ws.send(
            json.dumps(
                {
                    "event_id": "e1",
                    "session_id": "s1",
                    "sequence_number": 1,
                    "event_type": "session.status_changed",
                    "payload": {
                        "old_status": "active",
                        "new_status": "active",
                        "capture_status": {"mic": "inactive"},
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "event_id": "e2",
                    "session_id": "s1",
                    "sequence_number": 2,
                    "event_type": TRANSCRIPT_DELTA,
                    "payload": {"text": "I led", "segment_id": "seg1"},
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "event_id": "e3",
                    "session_id": "s1",
                    "sequence_number": 3,
                    "event_type": TRANSCRIPT_FINAL,
                    "payload": {
                        "text": "I led the migration.",
                        "is_final": True,
                        "turn_id": "t1",
                        "sequence_number": 7,
                        "segment_id": "seg1",
                    },
                }
            )
        )
        await asyncio.sleep(0.2)

    async def on_event(event: ServerEvent):
        events.append(event)
        if len(events) >= 3:
            seen_three.set()

    async with ws_server_factory(handler) as port:
        client = EventClient(make_settings(port), "s1", on_event, heartbeat_seconds=5)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(seen_three.wait(), timeout=5)
        finally:
            await client.stop()
            await asyncio.wait_for(task, timeout=5)

    assert headers["protocol"] == "bearer.jwt-token"
    assert [event.event_type for event in events] == [
        "session.status_changed",
        TRANSCRIPT_DELTA,
        TRANSCRIPT_FINAL,
    ]
    assert client.last_sequence == 3
    assert events[1].payload["text"] == "I led"


@pytest.mark.asyncio
async def test_events_sends_heartbeats(ws_server_factory):
    received = []
    got_heartbeat = asyncio.Event()

    async def handler(ws):
        while True:
            try:
                raw = await ws.recv()
            except Exception:
                return
            message = json.loads(raw)
            received.append(message)
            if message.get("type") == "client.heartbeat":
                got_heartbeat.set()
                return

    async with ws_server_factory(handler) as port:
        client = EventClient(
            make_settings(port), "s1", lambda event: asyncio.sleep(0),
            heartbeat_seconds=0.1,
        )
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(got_heartbeat.wait(), timeout=5)
        finally:
            await client.stop()
            task.cancel()

    assert received[0] == {"type": "client.heartbeat"}


@pytest.mark.asyncio
async def test_events_resubscribes_after_reconnect(ws_server_factory):
    connections = 0
    resubscribe = {}
    got_resubscribe = asyncio.Event()

    async def handler(ws):
        nonlocal connections
        connections += 1
        if connections == 1:
            await ws.send(
                json.dumps(
                    {
                        "sequence_number": 7,
                        "event_type": "system.heartbeat",
                        "payload": {"server_time": "now"},
                    }
                )
            )
            await asyncio.sleep(0.05)
            await ws.close()
            return
        raw = await ws.recv()
        resubscribe.update(json.loads(raw))
        got_resubscribe.set()
        await asyncio.sleep(0.2)

    async def on_event(event: ServerEvent):
        return None

    async with ws_server_factory(handler) as port:
        client = EventClient(
            make_settings(port),
            "s1",
            on_event,
            heartbeat_seconds=5,
            initial_backoff_seconds=0.05,
        )
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(got_resubscribe.wait(), timeout=5)
        finally:
            await client.stop()
            await asyncio.wait_for(task, timeout=5)

    assert resubscribe == {"type": "client.resubscribe", "last_known_sequence": 7}


@pytest.mark.asyncio
async def test_events_auth_close_code_stops_reconnecting(ws_server_factory):
    states = []
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        await ws.close(code=4401)

    async with ws_server_factory(handler) as port:
        client = EventClient(
            make_settings(port),
            "s1",
            lambda event: asyncio.sleep(0),
            heartbeat_seconds=5,
            initial_backoff_seconds=0.05,
            on_state_change=states.append,
        )
        await asyncio.wait_for(client.run(), timeout=5)

    assert connections == 1
    assert states[-1] == "unauthorized"


@pytest.mark.asyncio
async def test_replay_gap_routes_to_handler(ws_server_factory):
    gaps = []
    delivered = []
    got_gap = asyncio.Event()

    async def handler(ws):
        await ws.send(
            json.dumps(
                {
                    "event_type": "system.replay_gap",
                    "sequence_number": 1,
                    "payload": {
                        "oldest_available_sequence": 10,
                        "last_known_sequence": 2,
                    },
                }
            )
        )
        await asyncio.sleep(0.2)

    async def on_gap(event: ServerEvent):
        gaps.append(event.payload)
        got_gap.set()

    async with ws_server_factory(handler) as port:
        client = EventClient(
            make_settings(port), "s1", lambda event: delivered.append(event),
            on_replay_gap=on_gap, heartbeat_seconds=5,
        )
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(got_gap.wait(), timeout=5)
        finally:
            await client.stop()
            await asyncio.wait_for(task, timeout=5)

    assert delivered == []
    assert gaps[0]["oldest_available_sequence"] == 10
