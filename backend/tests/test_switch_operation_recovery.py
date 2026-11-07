import asyncio
from contextlib import asynccontextmanager
import logging

import httpx
import pytest

from app.services.switch_api import SwitchAPI, SwitchError


@pytest.mark.parametrize("failure", [502, 503, 504, "timeout"])
def test_read_failure_recovers_in_place_and_logs_operation_without_secrets(failure, caplog):
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            if len(calls) == 1:
                if failure == "timeout":
                    raise httpx.ReadTimeout("fixture-private-secret", request=request)
                return httpx.Response(failure, text="fixture-private-secret")
            return httpx.Response(200, json={"result": {"id": "fixture-room"}})
        api = SwitchAPI("https://fixture.invalid", "fixture-agent", "fixture-private-secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert await api.operation(client, "get_room_detail", {"room_id": "fixture-private-secret"}) == {"id": "fixture-room"}
        assert len(calls) == 2
    with caplog.at_level(logging.INFO, logger="team_memory.switch"):
        asyncio.run(run())
    assert "operation=get_room_detail" in caplog.text
    assert "attempt=2" in caplog.text
    assert "fixture-private-secret" not in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429])
def test_permanent_or_rate_limit_errors_are_not_retried(status):
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            return httpx.Response(status, text="fixture-private-secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(SwitchError) as caught:
                await SwitchAPI("https://fixture.invalid", "agent", "fixture-key").operation(client, "get_room_detail", {})
        assert len(calls) == 1
        assert caught.value.status_code == status
        assert "fixture-private-secret" not in str(caught.value)
    asyncio.run(run())


def test_read_retries_are_bounded():
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            return httpx.Response(503)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(SwitchError):
                await SwitchAPI("https://fixture.invalid", "agent", "fixture-key").operation(client, "get_agent_detail", {})
        assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("failure", [503, "timeout", "invalid_json"])
def test_uncertain_sends_are_never_retried_or_sent_through_fallback(monkeypatch, failure):
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            if failure == "timeout":
                raise httpx.ReadTimeout("fixture-private-secret", request=request)
            return httpx.Response(200, text="not json") if failure == "invalid_json" else httpx.Response(failure)
        @asynccontextmanager
        async def http(self):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                yield client
        monkeypatch.setattr(SwitchAPI, "http", http)
        with pytest.raises(SwitchError):
            await SwitchAPI("https://fixture.invalid", "agent", "fixture-key").post_room_message("room", "fixture-private-secret")
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("thread_id", [None, "fixture-thread"])
def test_room_reply_uses_documented_direct_endpoint_once(monkeypatch, thread_id):
    async def run():
        calls = []
        async def handle(request):
            import json
            calls.append(request)
            assert request.url.path == "/agents/agent/message"
            expected = {"room_id": "fixture-room", "content": "fixture answer"}
            if thread_id:
                expected["thread_id"] = thread_id
            assert json.loads(request.content) == expected
            return httpx.Response(201, json={"event_id": "fixture-event"})
        @asynccontextmanager
        async def http(self):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                yield client
        monkeypatch.setattr(SwitchAPI, "http", http)
        result = await SwitchAPI("https://fixture.invalid", "agent", "fixture-key").post_room_message(
            "fixture-room", "fixture answer", thread_id)
        assert result == {"event_id": "fixture-event"}
        assert len(calls) == 1
    asyncio.run(run())
