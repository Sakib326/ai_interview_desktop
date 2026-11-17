import asyncio
import json

import pytest

from desktop_client.audio_ws import AudioSender
from desktop_client.config import Settings


def make_settings(port: int) -> Settings:
    return Settings(
        api_base_url="http://127.0.0.1:1",
        ws_base_url=f"ws://127.0.0.1:{port}",
        access_token="jwt-token",
    )


async def frames(count=3, size=1600):
    for _ in range(count):
        yield b"\x01" * size


@pytest.mark.asyncio
async def test_audio_forwards_frames_then_waits_for_closed(ws_server_factory):
    seen = {"frames": [], "commands": [], "query": None, "protocol": None}
    done = asyncio.Event()

    async def handler(ws):
        seen["query"] = ws.request.path
        seen["protocol"] = ws.request.headers.get("Sec-WebSocket-Protocol")
        while True:
            message = await ws.recv()
            if isinstance(message, (bytes, bytearray)):
                seen["frames"].append(len(message))
                continue
            command = json.loads(message)
            seen["commands"].append(command)
            if command.get("type") == "client.audio_end":
                await ws.send(json.dumps({"type": "audio.closed"}))
                done.set()
                return

    async with ws_server_factory(handler) as port:
        sender = AudioSender(make_settings(port), "s1")
        outcome = await asyncio.wait_for(sender.run(frames()), timeout=10)

    assert done.is_set()
    assert outcome.status == "closed"
    assert seen["frames"] == [1600, 1600, 1600]
    assert seen["commands"] == [{"type": "client.audio_end"}]
    assert seen["query"].endswith("/sessions/s1/audio?source=mic")
    assert seen["protocol"] == "bearer.jwt-token"
    assert sender.frames_sent == 3


@pytest.mark.asyncio
async def test_audio_reports_failed_finalization(ws_server_factory):
    async def handler(ws):
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                command = json.loads(message)
                if command.get("type") == "client.audio_end":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "audio.failed",
                                "error_class": "STTProviderTransientError",
                                "retryable": True,
                                "message": "provider down",
                            }
                        )
                    )
                    return

    async with ws_server_factory(handler) as port:
        sender = AudioSender(make_settings(port), "s1")
        outcome = await asyncio.wait_for(sender.run(frames(1)), timeout=10)

    assert outcome.status == "failed"
    assert outcome.error_class == "STTProviderTransientError"
    assert outcome.retryable is True


@pytest.mark.asyncio
async def test_audio_reports_disconnect_without_closed(ws_server_factory):
    async def handler(ws):
        # Close the socket before any frame is acknowledged.
        await ws.close()

    async with ws_server_factory(handler) as port:
        sender = AudioSender(make_settings(port), "s1", finalize_timeout=0.5)
        outcome = await asyncio.wait_for(sender.run(frames(5)), timeout=10)

    assert outcome.status == "disconnected"


@pytest.mark.asyncio
async def test_audio_finalize_timeout(ws_server_factory):
    async def handler(ws):
        await ws.recv()
        # Never acknowledge audio_end; keep the socket open.
        await asyncio.sleep(1)

    async with ws_server_factory(handler) as port:
        sender = AudioSender(make_settings(port), "s1", finalize_timeout=0.2)
        outcome = await asyncio.wait_for(sender.run(frames(1)), timeout=10)

    assert outcome.status == "timeout"
