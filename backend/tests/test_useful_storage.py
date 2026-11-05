"""Offline tests: real SQLite journal, mocked Codex and Supabase HTTP/storage."""

import asyncio
import json
import sqlite3
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.models.ai import LLMDecision
from app.models.messages import IncomingMessage
from app.repositories.ingestion_jobs import IngestionJobs
from app.repositories.message_receipts import MessageReceiptRepository
from app.services.extraction_service import store_ai_memory
from app.services import ai_service
from app.services.ai_service import AIService, ExtractionFailure, ingestion_scope, parse_llm_decision, response_scope
from app.services.extraction_service import storage_items


def config(**updates):
    params = {
        "switch_requester_agent_id": "fixture-requester",
        "switch_requester_api_key": "fixture-client-key",
        "switch_ai_room_id": "private-room",
        "switch_ai_sender_id": "fixture-ai-participant",
        "require_save_confirmation": False,
        **updates,
    }
    return Settings(_env_file=None, **params)


def message(settings, body="Planning is Friday", **updates):
    params = {
        "tenant_id": settings.switch_tenant_id,
        "room_id": "test-room",
        "message_id": "unique-source",
        "sender": "human",
        "sender_name": "Person",
        "body": body,
        "timestamp_ms": 1793872800000,
        "thread_id": "thread-1",
        **updates,
    }
    return IncomingMessage(**params)


@pytest.mark.parametrize("text,category", [
    ("Plain text answer", "malformed_json"), ('{"action":"delete"}', "invalid_action"),
    ('{"answer":"hi"}', "missing_action"), ('{"action":"store_only"}', "invalid_fields"),
    ('{"action":"none","memories":[{"content":"hidden fact"}]}', "invalid_fields"),
    ('{"action":"query_only"}', "invalid_fields"),
    ('{"action":"store_only","decisions":[{"summary":"  "}]}', "invalid_fields"),
    ('{"action":"store_only","commitments":[{"description":"report","due_at":"Friday"}]}', "invalid_fields"),
    ('{"action":"store_only","commitments":[{"description":"report","due_at":"2026-11-06T09:00:00"}]}', "invalid_fields"),
    ('{"action":"none","tenant_id":"attacker"}', "invalid_fields"),
    ('{"action":"store_only","decisions":[{"summary":"FastAPI","approval_status":"approved"}]}', "invalid_fields"),
    ('{"action":"clarify"}', "invalid_fields"),
    ('{"action":"clarify","answer":""}', "invalid_fields"),
    ('{"action":"clarify","answer":"Please clarify","memories":[{"content":"fact"}]}', "invalid_fields"),
])
def test_invalid_model_output_is_never_none(text, category):
    with pytest.raises(ExtractionFailure, match=category):
        parse_llm_decision(text)


@pytest.mark.parametrize("body,extraction,counts", [
    ("Planning is Friday", {"memories": [{"content": "Planning is Friday"}]}, (0, 0, 1)),
    ("Good morning, planning is Friday", {"memories": [{"content": "Planning is Friday"}]}, (0, 0, 1)),
    ("I'll send the report by Friday", {"commitments": [{"description": "Send report by Friday"}]}, (1, 0, 1)),
    ("We decided to use FastAPI", {"decisions": [{"summary": "Use FastAPI"}]}, (0, 1, 1)),
    ("Good morning", {}, (0, 0, 0)),
    ("Maybe we should use Redis", {}, (0, 0, 0)),
])
def test_automatic_extraction_source_and_acknowledgement(monkeypatch, caplog, body, extraction, counts):
    async def run():
        settings, service, receipts = config(), AIService(), MessageReceiptRepository()
        saved, posted = [], []

        async def ask(settings, question):
            assert question == body
            return json.dumps({"action": "store_only" if extraction else "none", **extraction})

        async def store(settings, **kwargs):
            items = storage_items(kwargs["decision"], settings)
            saved.append((kwargs, items))
            return dict(messages=1, **{name: len(rows) for name, rows in items.items()})

        async def post(*args, **kwargs):
            posted.append(kwargs)

        monkeypatch.setattr(ai_service, "ask_switch", ask)
        monkeypatch.setattr(ai_service, "store_ai_memory", store)
        monkeypatch.setattr(ai_service.SwitchAPI, "post_room_message", post)
        try:
            receipt = await service.ingest(message(settings, body), settings, receipts)
            await service.queue.join()
            result = service.repository.recent(response_scope(settings))[0]
            assert result.status == "answered"
            if extraction:
                assert len(posted) == 1
                assert posted[0]["body"] == "Data has been saved into the database."
                assert posted[0]["room_id"] == "test-room"
                assert posted[0]["thread_id"] == "thread-1"
            else:
                assert posted == []
            job = await service.journal.get(receipt.message_ref)
            assert job["state"] == "done" and job["payload"] is None and job["decision"] is None
            if extraction:
                kwargs, items = saved[0]
                assert tuple(len(items[k]) for k in ("commitments", "decisions", "memories")) == counts
                assert kwargs["source_metadata"]["switch_author_id"] == "human"
                assert kwargs["source_metadata"]["thread_id"] == "thread-1"
                assert kwargs["occurred_at"] == "2026-11-05T10:00:00+00:00"
                assert kwargs["switch_message_id"] == "unique-source"
                assert kwargs["tenant_id"] == settings.switch_tenant_id
                assert kwargs["switch_room_id"] == "test-room"
                assert all("approval_status" not in item for rows in items.values() for item in rows)
                assert all("Good morning" not in item["content"] for item in items["memories"])
                if items["commitments"]:
                    assert items["commitments"][0]["owner_name"] is None
                    assert items["commitments"][0]["due_at"] is None
            else:
                assert saved == [] and result.stored_counts is None
            assert body not in caplog.text
        finally:
            await service.close()
    asyncio.run(run())


def test_unclear_message_triggers_clarification_reply_and_no_storage(monkeypatch):
    async def run():
        settings, service, receipts = config(), AIService(), MessageReceiptRepository()
        saved, posted = [], []

        async def ask(settings, question):
            return json.dumps({
                "action": "clarify",
                "answer": "Please clarify what topic or date you would like to set up the meeting for.",
            })

        async def store(settings, **kwargs):
            saved.append(kwargs)
            return {"messages": 1}

        async def post(*args, **kwargs):
            posted.append(kwargs)

        monkeypatch.setattr(ai_service, "ask_switch", ask)
        monkeypatch.setattr(ai_service, "store_ai_memory", store)
        monkeypatch.setattr(ai_service.SwitchAPI, "post_room_message", post)

        try:
            msg = message(settings, "set up a meeting")
            receipt = await service.ingest(msg, settings, receipts)
            await service.queue.join()
            result = service.repository.recent(response_scope(settings))[0]
            assert result.status == "answered"
            assert result.db_action == "clarify"
            assert result.stored_counts is None
            # Zero database writes occurred
            assert saved == []
            # Reply sent to Discord thread with clarification question
            assert len(posted) == 1
            assert posted[0]["body"] == "Please clarify what topic or date you would like to set up the meeting for."
            assert posted[0]["room_id"] == "test-room"
            assert posted[0]["thread_id"] == "thread-1"
        finally:
            await service.close()
    asyncio.run(run())


def test_confirmation_flow_yes_commits_and_no_cancels(monkeypatch):
    async def run():
        settings = config(require_save_confirmation=True)
        service = AIService()
        receipts = MessageReceiptRepository()
        saved = []
        posted = []

        async def ask(settings, question):
            return json.dumps({
                "action": "store_only",
                "commitments": [{"description": "Launch on Friday"}],
            })

        async def store(settings, **kwargs):
            saved.append(kwargs)
            return {"messages": 1, "commitments": 1, "decisions": 0, "memories": 1}

        async def post(self, room_id, body, thread_id=None):
            posted.append({"room_id": room_id, "body": body, "thread_id": thread_id})

        monkeypatch.setattr(ai_service, "ask_switch", ask)
        monkeypatch.setattr(ai_service, "store_ai_memory", store)
        monkeypatch.setattr(ai_service.SwitchAPI, "post_room_message", post)

        try:
            msg1 = message(settings, "I'll handle the launch on Friday")
            await service.ingest(msg1, settings, receipts)
            await service.queue.join()

            assert len(saved) == 0
            assert len(posted) == 1
            assert "I extracted the following data to save:" in posted[0]["body"]
            assert "Launch on Friday" in posted[0]["body"]
            assert "Reply 'yes' to confirm or 'no' to cancel" in posted[0]["body"]

            msg2 = IncomingMessage(
                tenant_id=settings.switch_tenant_id,
                room_id="test-room",
                message_id="confirm-msg-2",
                sender="human",
                sender_name="Person",
                body="yes",
                thread_id="thread-1",
                timestamp_ms=1793872801000,
            )
            await service.ingest(msg2, settings, receipts)

            assert len(saved) == 1
            assert len(posted) == 2
            assert posted[1]["body"] == "Data has been saved into the database."
            assert await service.pending_confirmations.get("test-room:thread-1") is None

            msg3 = message(settings, "Launch on Friday", message_id="msg-3", timestamp_ms=1793872802000)
            await service.ingest(msg3, settings, receipts)
            await service.queue.join()
            assert len(posted) == 3
            assert "Would you like to save this" in posted[2]["body"]

            msg4 = IncomingMessage(
                tenant_id=settings.switch_tenant_id,
                room_id="test-room",
                message_id="cancel-msg-4",
                sender="human",
                sender_name="Person",
                body="no",
                thread_id="thread-1",
                timestamp_ms=1793872803000,
            )
            await service.ingest(msg4, settings, receipts)

            assert len(saved) == 1
            assert len(posted) == 4
            assert posted[3]["body"] == "Cancelled. Data was not saved."
            assert await service.pending_confirmations.get("test-room:thread-1") is None
        finally:
            await service.close()
    asyncio.run(run())


def test_restart_recovers_accepted_work_and_completed_replay_does_not_extract(monkeypatch):
    async def run():
        settings = config()
        first = AIService()
        # No runtime configuration yet: acknowledge only after the SQLite commit.
        unavailable = settings.model_copy(update={"switch_ai_room_id": ""})
        receipt = await first.ingest(message(settings), unavailable, MessageReceiptRepository())
        assert first.pending == 0
        await first.close()
        calls = []

        async def ask(settings, question):
            calls.append(question)
            return '{"action":"none"}'

        monkeypatch.setattr(ai_service, "ask_switch", ask)
        second = AIService()
        jobs = await second.journal.recoverable(ingestion_scope(settings), settings.allowed_switch_rooms)
        assert len(jobs) == 1
        await second._enqueue_durable(jobs[0], settings)
        await second._enqueue_durable(jobs[0], settings)  # duplicate worker scheduling
        await second.queue.join()
        await second.close()
        third = AIService()
        replay = await third.ingest(message(settings), settings, MessageReceiptRepository())
        assert replay.duplicate and replay.message_ref == receipt.message_ref
        assert calls == ["Planning is Friday"] and third.pending == 0
        assert await third.journal.recoverable(ingestion_scope(settings), settings.allowed_switch_rooms) == []
        await third.close()
    asyncio.run(run())


def test_database_failure_is_recoverable_without_reextracting(monkeypatch):
    async def run():
        settings, service = config(), AIService()
        calls, writes = [], []

        async def ask(settings, question):
            calls.append(question)
            return '{"action":"store_only","memories":[{"content":"Planning Friday"}]}'

        async def store(settings, **kwargs):
            writes.append(kwargs)
            if len(writes) == 1:
                raise HTTPException(503, "database_write_failed")
            return {"messages": 1, "commitments": 0, "decisions": 0, "memories": 1}

        monkeypatch.setattr(ai_service, "ask_switch", ask)
        monkeypatch.setattr(ai_service, "store_ai_memory", store)
        receipt = await service.ingest(message(settings), settings, MessageReceiptRepository())
        await service.queue.join()
        row = service.repository.recent(response_scope(settings))[0]
        assert row.status == "failed" and row.stored_counts is None
        job = await service.journal.get(receipt.message_ref)
        assert job["state"] == "failed" and job["decision"] and job["counts"] is None
        await service.close()
        restarted = AIService()
        await restarted.journal.retry(receipt.message_ref)
        await restarted._enqueue_durable(await restarted.journal.get(receipt.message_ref), settings)
        await restarted.queue.join()
        assert restarted.repository.recent(response_scope(settings))[0].status == "answered"
        assert len(calls) == 1 and len(writes) == 2
        await restarted.close()
    asyncio.run(run())


def test_journal_failure_never_acknowledges_or_retains_preview(monkeypatch):
    async def run():
        settings, service, receipts = config(), AIService(), MessageReceiptRepository()

        async def fail(*args):
            raise sqlite3.OperationalError("fixture unavailable")

        monkeypatch.setattr(service.journal, "accept", fail)
        with pytest.raises(HTTPException) as error:
            await service.ingest(message(settings), settings, receipts)
        assert error.value.status_code == 503
        assert receipts.recent(settings.switch_tenant_id, settings.allowed_switch_rooms) == []
        await service.close()
    asyncio.run(run())


def test_invalid_extraction_retries_are_bounded_and_visible(monkeypatch):
    async def run():
        settings, service = config(), AIService()
        calls = []

        async def ask(settings, question):
            calls.append(question)
            return "Invalid extraction"

        monkeypatch.setattr(ai_service, "ask_switch", ask)
        receipt = await service.ingest(message(settings), settings, MessageReceiptRepository())
        await service.queue.join()
        for _ in range(2):
            await service.journal._execute(lambda db: db.execute("UPDATE jobs SET retry_at=0").rowcount)
            jobs = await service.journal.recoverable(ingestion_scope(settings), settings.allowed_switch_rooms)
            assert len(jobs) == 1
            await service._enqueue_durable(jobs[0], settings)
            await service.queue.join()
        await service.journal._execute(lambda db: db.execute("UPDATE jobs SET retry_at=0").rowcount)
        assert await service.journal.recoverable(ingestion_scope(settings), settings.allowed_switch_rooms) == []
        job = await service.journal.get(receipt.message_ref)
        assert job["state"] == "failed" and job["attempts"] == 3
        assert job["error"] == "extraction_malformed_json" and job["decision"] is None
        assert len(calls) == 3
        assert service.repository.recent(response_scope(settings))[0].db_action is None
        await service.close()
    asyncio.run(run())


def test_delivery_failure_does_not_repeat_extraction_and_echo_guard_survives_restart(monkeypatch):
    async def run():
        settings, service = config(), AIService()
        calls = []

        async def ask(settings, question, **kwargs):
            calls.append((question, kwargs))
            return "Fixture answer" if kwargs.get("synthesis") else '{"action":"query_only","search_query":"planning"}'

        async def query(*args, **kwargs):
            return {}

        async def post(*args, **kwargs):
            raise httpx.ConnectError("fixture delivery failed")

        monkeypatch.setattr(ai_service, "ask_switch", ask)
        monkeypatch.setattr(ai_service, "query_team_memory", query)
        monkeypatch.setattr(ai_service.SwitchAPI, "post_room_message", post)
        receipt = await service.ingest(message(settings), settings, MessageReceiptRepository())
        await service.queue.join()
        assert (await service.journal.get(receipt.message_ref))["state"] == "done"
        assert service.repository.recent(response_scope(settings))[0].status == "answered"
        await service.close()
        restarted = AIService()
        await restarted.ingest(message(settings), settings, MessageReceiptRepository())
        echo = message(settings, "Fixture answer").model_copy(update={"message_id": "echo-with-opaque-sender"})
        await restarted.ingest(echo, settings, MessageReceiptRepository())
        assert restarted.pending == 0 and len(calls) == 2
        assert calls[1][1] == {"synthesis": True}
        await restarted.close()
    asyncio.run(run())


@pytest.mark.parametrize("status,response", [(200, {"messages": 1, "commitments": 1, "decisions": 0, "memories": 1}), (500, {}), (200, {})])
def test_repository_uses_one_atomic_rpc_and_validates_acknowledgement(status, response):
    async def run():
        settings = config().model_copy(update={"supabase_url": "https://fixture.invalid", "supabase_service_role_key": "fixture-key"})
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(status, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            kwargs = dict(tenant_id=settings.switch_tenant_id, switch_room_id="test-room",
                          switch_message_id="source-1", author_name="Person", body="Report by Friday",
                          occurred_at="2026-11-05T10:00:00Z", client=client,
                          decision=LLMDecision(action="store_only", commitments=[{"description": "Report by Friday"}]))
            if status != 200 or not response:
                with pytest.raises(HTTPException):
                    await store_ai_memory(settings, **kwargs)
            else:
                assert await store_ai_memory(settings, **kwargs) == response
            assert len(requests) == 1 and requests[0].url.path == "/rest/v1/rpc/store_message_extraction"
            payload = json.loads(requests[0].content)
            assert payload["p_items"]["commitments"][0]["due_at"] is None
            assert len(payload["p_items"]["memories"]) == 1
            with pytest.raises(HTTPException) as error:
                await store_ai_memory(settings, **{**kwargs, "tenant_id": UUID(int=999)})
            assert error.value.status_code == 403 and len(requests) == 1
    asyncio.run(run())
