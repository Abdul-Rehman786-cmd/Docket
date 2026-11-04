import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.main import app
from app.services.ai_service import response_scope
from app.services import viewer_service
from app.services.viewer_service import ViewerClosed, ViewerHub

ORIGIN = {"Origin": "http://testserver"}
TOKEN = "test-ingest-token"
HEADERS = {"X-Ingest-Token": TOKEN}


def authenticate(socket, token=TOKEN):
    socket.send_json({"type": "authenticate", "token": token})
    return socket.receive_json()


def message(index=0, **updates):
    return {"tenant_id": "00000000-0000-0000-0000-000000000001", "room_id": "test-room",
            "message_id": f"socket-message-{index}", "sender": "person",
            "sender_name": "Teammate", "body": f"Message {index}", **updates}


def test_initial_snapshot_distinct_connections_and_disconnect_cleanup():
    with TestClient(app) as client:
        client.post("/messages", headers=HEADERS, json=message(body="Private initial message"))
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as first:
            initial = authenticate(first)
            assert initial["type"] == "snapshot"
            assert initial["messages"][0]["text"] == "Private initial message"
            assert initial["answers"] == []
            UUID(initial["connection_id"])
            with client.websocket_connect("/viewer/ws", headers=ORIGIN) as second:
                other = authenticate(second)
                assert other["connection_id"] != initial["connection_id"]
                assert other["messages"] == initial["messages"]
            first.send_json({"type": "resync"})
            assert first.receive_json()["connection_id"] == initial["connection_id"]
        assert not app.state.viewer_hub.connections
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as reconnected:
            assert authenticate(reconnected)["connection_id"] != initial["connection_id"]


@pytest.mark.parametrize("frame", [
    {"type": "authenticate", "token": "wrong"}, {"type": "authenticate", "token": ""},
    {"type": "resync"}, {"type": "authenticate", "token": TOKEN, "tenant_id": "other"},
])
def test_bad_authentication_never_returns_private_data(frame):
    with TestClient(app) as client:
        client.post("/messages", headers=HEADERS, json=message(body="Private fixture"))
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            socket.send_json(frame)
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 4401
        assert not app.state.viewer_hub.connections


@pytest.mark.parametrize("path,headers", [
    ("/viewer/ws", {}), ("/viewer/ws", {"Origin": "https://evil.invalid"}),
    ("/viewer/ws", {"Origin": "http://["}), ("/viewer/ws?token=fictional", ORIGIN),
])
def test_origin_and_query_parameters_are_rejected(path, headers):
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers=headers):
                pytest.fail("Unsafe handshake accepted")
        assert not app.state.viewer_hub.connections


def test_authentication_deadline_releases_unauthenticated_connection(monkeypatch):
    monkeypatch.setattr(viewer_service, "AUTH_TIMEOUT", 0.02)
    with TestClient(app) as client:
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 4408
        assert not app.state.viewer_hub.connections


def test_messages_push_without_polling_are_redacted_and_deduplicated(monkeypatch):
    monkeypatch.setenv("FIXTURE_API_KEY", "fixture-private-value")
    with TestClient(app) as client:
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            authenticate(socket)
            response = client.post("/messages", headers=HEADERS,
                                   json=message(body="Hello fixture-private-value test-ingest-token"))
            update = socket.receive_json()
            assert update["type"] == "message_added"
            assert update["message"]["text"] == "Hello [REDACTED] [REDACTED]"
            assert update["message"]["message_ref"] == response.json()["message_ref"]
            client.post("/messages", headers=HEADERS, json=message())
            socket.send_json({"type": "resync"})
            assert socket.receive_json()["type"] == "snapshot"  # No duplicate event.


def test_snapshot_and_updates_enforce_tenant_and_room_scope():
    with TestClient(app) as client:
        repository = app.state.message_receipts
        repository.record("a" * 24, tenant_id=UUID("00000000-0000-0000-0000-000000000002"),
                          room_id="test-room", redacted_sender_name="Other", redacted_text="Other tenant")
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            assert authenticate(socket)["messages"] == []
            repository.record("b" * 24, tenant_id=get_settings().switch_tenant_id,
                              room_id="forbidden-room", redacted_sender_name="Other", redacted_text="Other room")
            client.post("/messages", headers=HEADERS, json=message())
            event = socket.receive_json()
            assert event["type"] == "message_added" and event["message"]["text"] == "Message 0"
            socket.send_json({"type": "resync"})
            assert len(socket.receive_json()["messages"]) == 1


def test_ai_status_changes_push_only_matching_scope():
    with TestClient(app) as client:
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            authenticate(socket)
            repository = app.state.ai_service.repository
            scope, request_id = response_scope(get_settings()), uuid4()
            repository.create(("other-tenant",), uuid4(), "Hidden question", None)
            repository.create(scope, request_id, "Visible question", None)
            assert socket.receive_json()["type"] == "ai_queued"
            repository.update(scope, request_id, status="waiting")
            assert socket.receive_json()["type"] == "ai_waiting"
            repository.update(scope, request_id, status="answered", answer="Done")
            event = socket.receive_json()
            assert event["type"] == "ai_answered" and event["answer"]["answer"] == "Done"
            socket.send_json({"type": "resync"})
            assert len(socket.receive_json()["answers"]) == 1


@pytest.mark.parametrize("name,value,code", [
    ("SWITCH_INGEST_TOKEN", "replacement-token", 4401),
    ("SWITCH_ROOM_IDS", "second-room", 4403),
    ("SWITCH_TENANT_ID", "00000000-0000-0000-0000-000000000002", 4403),
])
def test_configuration_change_revokes_existing_socket(monkeypatch, name, value, code):
    with TestClient(app) as client:
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            authenticate(socket)
            monkeypatch.setenv(name, value)
            get_settings.cache_clear()
            socket.send_json({"type": "resync"})
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == code


def test_heartbeat_and_idle_authorization_check(monkeypatch):
    monkeypatch.setattr(viewer_service, "HEARTBEAT_INTERVAL", 0.02)
    with TestClient(app) as client:
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            authenticate(socket)
            assert socket.receive_json() == {"type": "ping"}
            socket.send_json({"type": "pong"})
            monkeypatch.setenv("SWITCH_INGEST_TOKEN", "replacement-token")
            get_settings.cache_clear()
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 4401


def test_retention_removal_is_pushed():
    with TestClient(app) as client:
        for index in range(100):
            client.post("/messages", headers=HEADERS, json=message(index))
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            initial = authenticate(socket)
            client.post("/messages", headers=HEADERS, json=message(100))
            removed = socket.receive_json()
            assert removed == {"type": "message_removed", "message_ref": initial["messages"][-1]["message_ref"]}
            assert socket.receive_json()["type"] == "message_added"


def test_slow_clients_and_connection_count_are_bounded(monkeypatch):
    async def run():
        hub = ViewerHub()
        subscription = hub.reserve()
        subscription.scope = response_scope(get_settings())
        for _ in range(viewer_service.MAX_BUFFERED_EVENTS + 1):
            hub.publish("message_added", (subscription.scope[0], "test-room"), {"message": {}})
        assert subscription.queue.qsize() == 1
        assert isinstance(subscription.queue.get_nowait(), ViewerClosed)
        monkeypatch.setattr(viewer_service, "MAX_CONNECTIONS", 1)
        with pytest.raises(ViewerClosed):
            hub.reserve()
        hub.close()
        assert isinstance(subscription.queue.get_nowait(), ViewerClosed)
        hub.remove(subscription)
        assert not hub.connections
    asyncio.run(run())


def test_busy_feeds_do_not_postpone_heartbeats(monkeypatch):
    async def run():
        clock, sent = [0], []
        monkeypatch.setattr(viewer_service, "monotonic", lambda: clock[0])
        monkeypatch.setattr(viewer_service, "current_settings", lambda subscription: get_settings())
        subscription = ViewerHub().reserve()
        subscription.last_pong = 0
        subscription.queue.put_nowait({"type": "message_added"})
        subscription.queue.put_nowait({"type": "message_added"})

        class Socket:
            async def send_json(self, payload):
                sent.append(payload["type"])
                clock[0] += 6
                if payload["type"] == "ping":
                    raise ViewerClosed(1000)

        with pytest.raises(ViewerClosed):
            await viewer_service.write_updates(Socket(), subscription, None)
        assert sent == ["message_added", "message_added", "ping"]
    asyncio.run(run())


def test_csp_permits_only_the_same_host_socket():
    with TestClient(app) as client:
        csp = client.get("/").headers["content-security-policy"]
        assert "connect-src 'self' ws://testserver;" in csp
        assert "ws:" not in csp.replace("ws://testserver", "")


def test_ai_retention_removal_is_pushed():
    with TestClient(app) as client:
        repository, scope = app.state.ai_service.repository, response_scope(get_settings())
        for _ in range(100):
            request_id = uuid4()
            repository.create(scope, request_id, "Question", None)
            repository.update(scope, request_id, status="answered", answer="Done")
        with client.websocket_connect("/viewer/ws", headers=ORIGIN) as socket:
            initial = authenticate(socket)
            repository.create(scope, uuid4(), "New question", None)
            assert socket.receive_json() == {
                "type": "ai_removed", "request_id": initial["answers"][-1]["request_id"],
            }
            assert socket.receive_json()["type"] == "ai_queued"
