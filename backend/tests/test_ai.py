import asyncio
import json
import logging
from threading import Event
from contextlib import asynccontextmanager
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from app.core.config import Settings, get_settings
from app.main import app
from app.services import ai_service
from app.services.switch_api import SwitchAPI, SwitchError, require_internal_room
from app.models.ai import AskRequest
from app.services.ai_service import AIService
from app.repositories import record_repository
from app.repositories.message_receipts import MessageReceiptRepository

HEADERS = {"X-Ingest-Token": "test-ingest-token"}


@pytest.fixture
def ai_config(monkeypatch):
    for name, value in {
        "SWITCH_REQUESTER_AGENT_ID": "fixture-requester",
        "SWITCH_REQUESTER_API_KEY": "fixture-client-key",
        "SWITCH_AI_ROOM_ID": "private-room",
        "SWITCH_AI_SENDER_ID": "fixture-ai-participant",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def detail(**updates):
    return {"id": "private-room", "bridge_id": None, "external_channel_id": None,
            "agent_names": ["team-memory-client", "codex-discord"], **updates}


def input_detail(room_id):
    return {"id": room_id, "bridge_id": "fixture-bridge", "external_channel_id": "fixture-channel",
            "agent_names": ["team-memory-client"]}


@pytest.mark.parametrize("method", ["get", "post"])
def test_ai_endpoints_require_existing_local_authentication(method):
    with TestClient(app) as client:
        response = client.request(method, "/ai/requests", json={"question": "Private fixture"})
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        assert "Private fixture" not in response.text


def test_ai_is_disabled_until_private_setup_is_complete():
    with TestClient(app) as client:
        assert client.post("/ai/requests", headers=HEADERS, json={
            "request_id": str(uuid4()), "question": "A question"
        }).status_code == 503


@pytest.mark.parametrize("body", [{}, {"question": " "}, {"question": "x" * 4001},
    {"question": "text", "source_message_ref": "a" * 24}, {"question": "text", "room_id": "discord-room"}])
def test_ai_validation_never_reflects_input(body, ai_config):
    with TestClient(app) as client:
        response = client.post("/ai/requests", headers=HEADERS, json={"request_id": str(uuid4()), **body})
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid request payload"}


def test_question_answer_redaction_idempotency_and_scope(ai_config, monkeypatch):
    sent = []

    async def answer(settings, question):
        sent.append(question)
        return json.dumps({"action": "none", "answer": "Codex answer fixture-client-key test-ingest-token"})

    monkeypatch.setattr(ai_service, "ask_switch", answer)
    with TestClient(app) as client:
        body = {"request_id": str(uuid4()), "question": "Question fixture-client-key test-ingest-token"}
        first = client.post("/ai/requests", headers=HEADERS, json=body)
        assert first.status_code == 202
        for _ in range(30):
            feed = client.get("/ai/requests", headers=HEADERS)
            if feed.json()[0]["status"] == "answered":
                break
        assert feed.json()[0]["answer"] == "Codex answer [REDACTED] [REDACTED]"
        assert feed.json()[0]["db_action"] == "none"
        assert sent == ["Question [REDACTED] [REDACTED]"]
        assert feed.headers["cache-control"] == "no-store"
        assert client.post("/ai/requests", headers=HEADERS, json=body).status_code == 202
        assert client.post("/ai/requests", headers=HEADERS, json={**body, "question": "Different"}).status_code == 409
        assert len(sent) == 1
        monkeypatch.setenv("SWITCH_TENANT_ID", "00000000-0000-0000-0000-000000000002")
        get_settings.cache_clear()
        assert client.get("/ai/requests", headers=HEADERS).json() == []


def test_source_message_must_be_retained_and_authorized(ai_config, monkeypatch):
    sent = []

    async def answer(settings, question):
        sent.append(question)
        return '{"action":"none","answer":"The answer"}'

    monkeypatch.setattr(ai_service, "ask_switch", answer)
    with TestClient(app) as client:
        message = client.post("/messages", headers=HEADERS, json={
            "tenant_id": "00000000-0000-0000-0000-000000000001", "room_id": "test-room",
            "message_id": "fixture-question", "sender": "fixture-user", "body": "When will we ship?"
        }).json()
        body = {"request_id": str(uuid4()), "source_message_ref": message["message_ref"]}
        assert client.post("/ai/requests", headers=HEADERS, json=body).status_code == 202
        for _ in range(30):
            if client.get("/ai/requests", headers=HEADERS).json()[0]["status"] == "answered":
                break
        assert sent == ["When will we ship?"]
        monkeypatch.setenv("SWITCH_ROOM_IDS", "second-room")
        get_settings.cache_clear()
        assert client.post("/ai/requests", headers=HEADERS, json={**body, "request_id": str(uuid4())}).status_code == 404
        assert client.get("/ai/requests", headers=HEADERS).json() == []
        assert len(sent) == 1


@pytest.mark.parametrize("action", ["store_only", "query_only", "none", "store_and_query"])
def test_llm_four_way_database_decisions(ai_config, monkeypatch, caplog, action):
    monkeypatch.setenv("SWITCH_ROOM_IDS", "test-room")
    monkeypatch.setenv("SUPABASE_URL", "https://fixture.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-service-role-key")
    get_settings.cache_clear()

    db_writes = []
    db_reads = []
    llm_calls = []

    async def fake_get(self, url, *, params=None, headers=None, timeout=10):
        table = url.rsplit("/", 1)[-1]
        if table == "rooms":
            return httpx.Response(200, json=[{
                "id": "00000000-0000-0000-0000-000000000010",
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "enabled": True,
            }], request=httpx.Request("GET", url))
        db_reads.append(table)
        return httpx.Response(200, json=[{"content": f"Stored {table} row"}], request=httpx.Request("GET", url))

    async def fake_post(self, url, *, params=None, headers=None, json=None, timeout=10):
        table = url.rsplit("/", 1)[-1]
        db_writes.append((table, json))
        if table == "store_message_extraction":
            return httpx.Response(200, json={"messages": 1, "commitments": 1, "decisions": 1, "memories": 3}, request=httpx.Request("POST", url))
        return httpx.Response(201, content=b"", request=httpx.Request("POST", url))

    async def fake_ask_switch(settings, prompt, **kwargs):
        llm_calls.append(prompt)
        if len(llm_calls) == 1:
            return json.dumps({
                "action": action,
                "search_query": "launch" if action in ("query_only", "store_and_query") else None,
                "commitments": [{"description": "Send launch report", "owner_name": "Ali", "due_at": "2026-11-06T17:00:00Z"}] if action in ("store_only", "store_and_query") else [],
                "decisions": [{"topic": "Launch", "summary": "Ship on Friday"}] if action in ("store_only", "store_and_query") else [],
                "memories": [{"memory_type": "fact", "content": "Launch is Friday"}] if action in ("store_only", "store_and_query") else [],
                "answer": f"Direct answer for {action}",
            })
        return f"Database-grounded answer for {action}"

    monkeypatch.setattr(record_repository.httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(record_repository.httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(ai_service, "ask_switch", fake_ask_switch)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(app) as client:
            req_id = str(uuid4())
            resp = client.post("/ai/requests", headers=HEADERS, json={"request_id": req_id, "question": "Team update on launch"})
            assert resp.status_code == 202
            for _ in range(30):
                row = client.get("/ai/requests", headers=HEADERS).json()[0]
                if row["status"] in ("answered", "failed"):
                    break

    assert row["status"] == "answered"
    assert row["db_action"] == action
    assert f"AI decision ref=direct-question request_id={req_id} action={action}" in caplog.text
    assert f"AI response complete ref=direct-question request_id={req_id} action={action}" in caplog.text
    assert "fixture-service-role-key" not in caplog.text

    if action == "none":
        assert db_writes == []
        assert db_reads == []
        assert row["answer"] == "Direct answer for none"
        assert row["stored_counts"] is None
    elif action == "store_only":
        assert [t for t, _ in db_writes] == ["store_message_extraction"]
        assert db_reads == []
        assert row["answer"] == "Direct answer for store_only"
        assert row["stored_counts"] == {"messages": 1, "commitments": 1, "decisions": 1, "memories": 3}
    elif action == "query_only":
        assert db_writes == []
        assert set(db_reads) == {"memories", "commitments", "decisions", "messages"}
        assert row["answer"] == "Database-grounded answer for query_only"
        assert row["stored_counts"] is None
    elif action == "store_and_query":
        assert [t for t, _ in db_writes] == ["store_message_extraction"]
        assert set(db_reads) == {"memories", "commitments", "decisions", "messages"}
        assert row["answer"] == "Database-grounded answer for store_and_query"
        assert row["stored_counts"] == {"messages": 1, "commitments": 1, "decisions": 1, "memories": 3}


def test_incoming_message_automatically_logs_ai_decision(ai_config, monkeypatch, caplog):
    prompts_received = []

    async def fake_ask_switch(settings, prompt, **kwargs):
        prompts_received.append(prompt)
        return json.dumps({
            "action": "none",
            "answer": "No database update needed for casual greeting",
        })

    monkeypatch.setattr(ai_service, "ask_switch", fake_ask_switch)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(app) as client:
            receipt = client.post("/messages", headers=HEADERS, json={
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "room_id": "test-room",
                "message_id": "auto-msg-1",
                "sender": "fixture-user",
                "body": "Hey everyone!",
            }).json()
            for _ in range(30):
                rows = client.get("/ai/requests", headers=HEADERS).json()
                if rows and rows[0]["status"] == "answered":
                    break
    assert len(prompts_received) == 1
    assert "Hey everyone!" in prompts_received[0]
    assert "[message text suppressed" not in prompts_received[0]
    assert f"AI evaluating message ref={receipt['message_ref']}" in caplog.text
    assert f"AI decision ref={receipt['message_ref']}" in caplog.text
    assert "action=none" in caplog.text
    assert f"AI response complete ref={receipt['message_ref']}" in caplog.text


def test_remote_error_contents_are_never_exposed(ai_config, monkeypatch):
    async def fail(*args):
        raise httpx.ConnectError("fixture-client-key private-remote-error")

    monkeypatch.setattr(ai_service, "ask_switch", fail)
    with TestClient(app) as client:
        client.post("/ai/requests", headers=HEADERS, json={"request_id": str(uuid4()), "question": "Hello"})
        for _ in range(30):
            response = client.get("/ai/requests", headers=HEADERS)
            if response.json()[0]["status"] == "failed":
                break
        assert response.json()[0]["status"] == "failed"
        assert "private-remote-error" not in response.text
        assert "fixture-client-key" not in response.text


@pytest.mark.parametrize("updates", [{"bridge_id": "discord"}, {"external_channel_id": "discord-channel"},
    {"id": "other-room"}, {"agent_names": []}, {"archived": True}])
def test_bridged_or_wrong_room_is_rejected(updates):
    with pytest.raises(SwitchError):
        require_internal_room(detail(**updates), "private-room", "codex-discord")


def test_missing_routing_fields_fail_closed():
    with pytest.raises(SwitchError):
        require_internal_room({"id": "private-room", "agent_names": ["codex-discord"]}, "private-room", "codex-discord")


def test_bridged_destination_never_opens_a_socket_or_posts(ai_config, monkeypatch):
    calls = []

    async def operation(self, client, name, args, connection_id=None):
        calls.append(name)
        return detail(bridge_id="discord")

    monkeypatch.setattr(SwitchAPI, "operation", operation)
    with pytest.raises(SwitchError, match="no messaging bridge"):
        asyncio.run(ai_service.ask_switch(Settings(_env_file=None), "Private fixture question"))
    assert calls == ["get_room_detail"]


def test_added_room_participant_prevents_question_delivery(ai_config, monkeypatch):
    calls = []

    async def operation(self, client, name, args, connection_id=None):
        calls.append(name)
        if name == "get_room_detail":
            return detail() if args["room_id"] == "private-room" else input_detail(args["room_id"])
        if name == "get_agent_detail":
            return {"sessions": []}
        if name == "list_participants":
            return [{"id": "test-agent"}, {"id": "fixture-requester"}, {"id": "unexpected-agent"}]
        raise AssertionError("Question must not be posted")

    @asynccontextmanager
    async def room_connection(self, room_id):
        yield "fixture-connection", asyncio.Queue()

    monkeypatch.setattr(SwitchAPI, "operation", operation)
    monkeypatch.setattr(SwitchAPI, "room_connection", room_connection)
    with pytest.raises(SwitchError, match="only Codex"):
        asyncio.run(ai_service.ask_switch(Settings(_env_file=None), "Private fixture question"))
    assert calls == ["get_room_detail", "get_agent_detail", "get_room_detail", "get_room_detail", "list_participants"]


def test_codex_reinvited_to_input_room_blocks_api_question_before_posting(ai_config, monkeypatch):
    calls = []

    async def operation(self, client, name, args, connection_id=None):
        calls.append(name)
        if name == "get_agent_detail":
            return {"sessions": []}
        assert name == "get_room_detail"
        if args["room_id"] == "private-room":
            return detail()
        room = input_detail(args["room_id"])
        room["agent_names"].append("codex-discord")
        return room

    monkeypatch.setattr(SwitchAPI, "operation", operation)
    with pytest.raises(SwitchError, match="without Codex"):
        asyncio.run(ai_service.ask_switch(Settings(_env_file=None), "Private question"))
    assert calls == ["get_room_detail", "get_agent_detail", "get_room_detail"]


def test_live_discord_session_prevents_question_even_after_membership_removal(ai_config, monkeypatch):
    calls = []

    async def operation(self, client, name, args, connection_id=None):
        calls.append(name)
        if name == "get_room_detail":
            return detail() if args["room_id"] == "private-room" else input_detail(args["room_id"])
        assert name == "get_agent_detail"
        return {"sessions": [{"room_id": "test-room", "state": "live"}]}

    monkeypatch.setattr(SwitchAPI, "operation", operation)
    with pytest.raises(SwitchError, match="Stop that Discord session"):
        asyncio.run(ai_service.ask_switch(Settings(_env_file=None), "Private question"))
    assert calls == ["get_room_detail", "get_agent_detail"]


def test_pending_queue_is_bounded_and_shutdown_cancels_without_resubmission(ai_config, monkeypatch):
    async def run():
        sent = []
        gate = asyncio.Event()

        async def answer(settings, question):
            sent.append(question)
            await gate.wait()
            return "Answer"

        monkeypatch.setattr(ai_service, "ask_switch", answer)
        service = AIService()
        settings = Settings(_env_file=None)
        receipts = MessageReceiptRepository()
        requests = [AskRequest(request_id=uuid4(), question=f"Question {index}") for index in range(5)]
        try:
            for body in requests[:4]:
                await service.submit(body, settings, receipts)
            with pytest.raises(ai_service.HTTPException) as error:
                await service.submit(requests[4], settings, receipts)
            assert error.value.status_code == 429
            assert (await service.submit(requests[0], settings, receipts)).request_id == requests[0].request_id
            await asyncio.sleep(0)
            assert sent == ["Question 0"]
        finally:
            await service.close()
        results = service.repository.recent(ai_service.response_scope(settings))
        assert all(row.status == "failed" for row in results)
        assert len(sent) == 1

    asyncio.run(run())


def test_real_socket_correlates_exact_sender_and_thread_and_keeps_heartbeat(ai_config, monkeypatch):
    async def run():
        targeted = asyncio.Event()
        heartbeat = asyncio.Event()
        operations = []

        async def switch(socket):
            query = parse_qs(urlsplit(socket.request.path).query)
            assert query["rooms"] == ["private-room"]
            assert query["scope"] == ["single"]
            assert "/agents/fixture-requester/" in socket.request.path
            assert socket.request.headers["Authorization"] == "Bearer fixture-client-key"
            await socket.send(json.dumps({"event": "connection_state", "data": {"cursor": 0, "rooms": ["private-room"]}}))
            await targeted.wait()
            await socket.send(json.dumps({"type": "ping"}))
            assert json.loads(await socket.recv()) == {"type": "pong", "cursor": 0}
            heartbeat.set()
            for seq, sender, thread, body in [
                (1, "imposter", "request-root", "Wrong sender"),
                (2, "fixture-ai-participant", "another-request", "Wrong thread"),
                (3, "fixture-ai-participant", "request-root", "Correct answer"),
            ]:
                await socket.send(json.dumps({"event": "message", "id": seq, "data": {
                    "sequence": seq, "room_id": "private-room", "payload": {
                        "sender": sender, "thread_id": thread, "body": body,
                    },
                }}))
            await socket.wait_closed()

        async def handle(request):
            op = request.url.path.rsplit("/", 1)[-1]
            operations.append(op)
            assert "fixture-client-key" not in str(request.url)
            if op == "get_room_detail":
                room_id = json.loads(request.content)["room_id"]
                return httpx.Response(200, json={"result": detail() if room_id == "private-room" else input_detail(room_id)})
            if op == "list_participants":
                return httpx.Response(200, json={"result": [{"id": "fixture-requester"}, {"id": "test-agent"}]})
            if op == "get_agent_detail":
                return httpx.Response(200, json={"result": {"sessions": []}})
            assert op == "send_targeted_message"
            body = json.loads(request.content)
            assert body["target_names"] == ["codex-discord"]
            assert request.headers["X-Switch-Connection-Id"]
            targeted.set()
            await heartbeat.wait()
            return httpx.Response(200, json={"result": {"event_id": "request-root", "target_statuses": {"codex-discord": "delivered"}}})

        @asynccontextmanager
        async def fake_http(self):
            async with httpx.AsyncClient(base_url="http://fixture/", transport=httpx.MockTransport(handle), headers=self.headers) as client:
                yield client

        monkeypatch.setattr(SwitchAPI, "http", fake_http)
        async with serve(switch, "127.0.0.1", 0) as server:
            settings = Settings(_env_file=None, switch_bridge_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            answer = await asyncio.wait_for(ai_service.ask_switch(settings, "A fixture question"), timeout=5)
        assert answer == "Correct answer"
        assert operations == ["get_room_detail", "get_agent_detail", "get_room_detail", "get_room_detail", "list_participants", "get_room_detail", "send_targeted_message"]

    asyncio.run(run())


def test_incoming_message_replies_to_switch_discord_room(ai_config, monkeypatch, caplog):
    posted_messages = []
    delivered = Event()

    async def fake_ask_switch(settings, prompt, **kwargs):
        return json.dumps({
            "action": "query_only",
            "search_query": "deadline",
            "answer": "The project launch deadline is Friday.",
        })

    async def fake_post_room_message(self, room_id, body, thread_id=None):
        posted_messages.append({"room_id": room_id, "body": body, "thread_id": thread_id})
        delivered.set()
        return {"status": "ok"}

    monkeypatch.setattr(ai_service, "ask_switch", fake_ask_switch)
    async def empty_memory(settings, search_query, **kwargs):
        return {}

    monkeypatch.setattr(ai_service, "query_team_memory", empty_memory)
    monkeypatch.setattr(SwitchAPI, "post_room_message", fake_post_room_message)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(app) as client:
            receipt = client.post("/messages", headers=HEADERS, json={
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "room_id": "test-room",
                "message_id": "discord-msg-42",
                "sender": "fixture-discord-user",
                "body": "When is the launch deadline?",
                "addressed": True,
                "thread_id": "discord-thread-99",
            }).json()
            for _ in range(30):
                rows = client.get("/ai/requests", headers=HEADERS).json()
                if rows and rows[0]["status"] == "answered":
                    break

            assert delivered.wait(2), "The worker must finish the durable checkpoint and post before shutdown"

    assert len(posted_messages) == 2
    assert posted_messages[0]["body"] == "Processing your request..."
    assert posted_messages[1]["room_id"] == "test-room"
    assert posted_messages[1]["thread_id"] == "discord-thread-99"
    assert "Friday" in posted_messages[1]["body"]
    assert "AI reply posted to Switch room=test-room" in caplog.text



def test_viewer_source_request_never_posts_to_discord(ai_config, monkeypatch):
    posted = []

    async def fake_ask_switch(settings, prompt, **kwargs):
        return json.dumps({"action": "query_only", "search_query": "x", "answer": "Viewer-only answer"})

    async def fake_post_room_message(self, room_id, body, thread_id=None):
        posted.append(room_id)

    monkeypatch.setattr(ai_service, "ask_switch", fake_ask_switch)
    async def empty_memory(settings, search_query, **kwargs):
        return {}

    monkeypatch.setattr(ai_service, "query_team_memory", empty_memory)
    monkeypatch.setattr(SwitchAPI, "post_room_message", fake_post_room_message)

    async def run():
        settings = get_settings()
        receipts = MessageReceiptRepository()
        receipts.record(
            "a" * 24, tenant_id=settings.switch_tenant_id, room_id="test-room",
            redacted_sender_name="Ali", redacted_text="What did we decide?",
        )
        service = AIService()
        await service.submit(AskRequest(request_id=uuid4(), source_message_ref="a" * 24), settings, receipts)
        await service.queue.join()
        await service.close()
        return service.repository.recent(ai_service.response_scope(settings))

    rows = asyncio.run(run())
    assert rows[0].status == "answered"
    assert rows[0].db_action == "query_only"
    assert posted == []
