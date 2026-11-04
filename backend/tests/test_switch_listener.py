import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.asyncio.server import serve

from app.core.config import Settings
from app.main import app
from app.repositories.listener_state import ListenerState
from app.services.switch_listener import ListenerStopped, RetryConnection, SwitchListener


def make_listener(tmp_path, **updates):
    return SwitchListener(Settings(_env_file=None, **updates), state_path=tmp_path / "listener.json")


def event(room="test-room", sequence=1):
    return {
        "event": "message", "id": sequence,
        "data": {
            "type": "message", "room_id": room, "sequence": sequence,
            "payload": {
                "message_id": "test-message", "sender": "test-sender", "sender_name": "Fixture teammate",
                "body": "Private test content",
                "timestamp": 1793788800000, "addressed": False, "thread_id": None,
            },
        },
    }


def test_connection_url_declares_rooms_and_resumes_with_generation_fencing(tmp_path):
    listener = make_listener(tmp_path)
    listener.state.cursor, listener.state.generation = 12, 3
    listener.state.save(listener.state_path)
    restarted = make_listener(tmp_path)
    url = urlsplit(restarted.socket_url())
    query = parse_qs(url.query)
    assert url.scheme == "ws"
    assert query["connection_id"] == [listener.state.connection_id]
    assert query["start_from"] == ["12"]
    assert query["expected_generation"] == ["3"]
    assert query["scope"] == ["all"] and query["filter"] == ["all"]
    assert query["rooms"] == ["second-room,test-room"]
    assert "rooms" not in parse_qs(urlsplit(restarted.socket_url(claim_rooms=False)).query)
    assert "test-agent-key" not in restarted.socket_url()


def test_changing_agent_does_not_reuse_old_cursor(tmp_path):
    first = make_listener(tmp_path)
    first.state.cursor = 50
    first.state.save(first.state_path)
    other = make_listener(tmp_path, switch_agent_id="another-agent")
    assert other.state.cursor is None
    assert other.state.connection_id != first.state.connection_id


@pytest.mark.parametrize("status", [401, 403, 422, 503])
def test_api_failure_does_not_advance_cursor(tmp_path, status):
    async def run():
        listener = make_listener(tmp_path)
        listener.state.cursor = 0
        queue = asyncio.Queue()
        item = event()
        queue.put_nowait(("message", item["data"], item["id"]))
        transport = httpx.MockTransport(lambda request: httpx.Response(status))
        async with httpx.AsyncClient(transport=transport, base_url="http://test/") as client:
            expected = RetryConnection if status == 503 else ListenerStopped
            with pytest.raises(expected):
                await listener._forward_events(queue, client)
        assert listener.state.cursor == 0
        assert not listener.state_path.exists()
    asyncio.run(run())


def test_unmapped_room_is_not_forwarded(tmp_path):
    async def run():
        listener = make_listener(tmp_path)
        listener.state.cursor = 0
        queue = asyncio.Queue()
        item = event(room="not-allowed")
        queue.put_nowait(("message", item["data"], item["id"]))
        requests = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: requests.append(request)), base_url="http://test/"
        ) as client:
            task = asyncio.create_task(listener._forward_events(queue, client))
            for _ in range(100):
                if listener.state.cursor == 1:
                    break
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert listener.state.cursor == 1
        assert requests == []
    asyncio.run(run())


@pytest.mark.parametrize("control", [
    {"type": "evicted", "code": "taken_over"},
    {"event": "refused", "data": {"status": 409}},
    {"type": "gap", "resumed_at": 50},
])
def test_permanent_control_frames_stop_without_advancing_cursor(tmp_path, control):
    class Socket:
        def __aiter__(self):
            async def items():
                yield json.dumps(control)
            return items()

    async def run():
        listener = make_listener(tmp_path)
        listener.state.cursor = 4
        with pytest.raises(ListenerStopped):
            await listener._read_frames(Socket(), asyncio.Queue())
        assert listener.state.cursor == 4
    asyncio.run(run())


def test_full_websocket_delivery_keeps_heartbeat_responsive_during_slow_api(tmp_path, caplog):
    async def run():
        request_started = asyncio.Event()
        allow_response = asyncio.Event()
        posted = asyncio.Event()
        api_transport = httpx.ASGITransport(app=app)
        pongs = []

        class SlowAPI(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.url.path == "/messages":
                    request_started.set()
                    await allow_response.wait()
                response = await api_transport.handle_async_request(request)
                if request.url.path == "/messages":
                    posted.set()
                return response

        async def switch(socket):
            assert socket.request.headers["Authorization"] == "Bearer test-agent-key"
            await socket.send(json.dumps({
                "event": "connection_state", "data": {"generation": 1, "cursor": 0, "rooms": ["test-room"]},
            }))
            await socket.send(json.dumps(event()))
            await asyncio.wait_for(request_started.wait(), timeout=3)
            await socket.send(json.dumps({"event": "ping", "data": {}}))
            pongs.append(json.loads(await asyncio.wait_for(socket.recv(), timeout=2)))
            allow_response.set()
            await asyncio.wait_for(posted.wait(), timeout=3)
            while listener.state.cursor != 1:
                await asyncio.sleep(0)
            await socket.send(json.dumps({"type": "evicted", "code": "taken_over"}))

        async with serve(switch, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            listener = make_listener(tmp_path, switch_bridge_url=f"http://127.0.0.1:{port}")
            async with httpx.AsyncClient(
                transport=SlowAPI(), base_url="http://test/", headers={"X-Ingest-Token": "test-ingest-token"}
            ) as client:
                with pytest.raises(ListenerStopped):
                    await asyncio.wait_for(listener._session(client), timeout=8)
        assert pongs == [{"type": "pong", "cursor": 0}]
        assert app.state.message_receipts.status().received_count == 1
        recent = app.state.message_receipts.recent(listener.settings.switch_tenant_id, listener.settings.allowed_switch_rooms)
        assert recent[0].sender_name == "Fixture teammate"
        assert recent[0].text == "Private test content"
        saved = ListenerState.load(listener.state_path, listener.state.identity)
        assert saved.cursor == 1 and saved.generation == 1
        text = listener.state_path.read_text()
        assert "Private test content" not in text
        assert "test-agent-key" not in text
    asyncio.run(run())
