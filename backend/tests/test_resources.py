"""Offline lifecycle, overload, and async responsiveness regressions."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.http_clients import HTTPClients, use_http_clients
from app.main import app
from app.models.ai import AskRequest
from app.models.messages import IncomingMessage
from app.repositories.ai_requests import AIRequestRepository
from app.repositories.message_receipts import MessageReceiptRepository
from app.repositories.record_repository import check_database_connection
from app.services.ai_service import AIService, response_scope
from app.services.switch_api import SwitchAPI


def settings():
    return Settings(_env_file=None, switch_requester_agent_id="fixture-requester",
                    switch_requester_api_key="fixture-client-key",
                    switch_ai_room_id="private-room",
                    switch_ai_sender_id="fixture-ai-participant")


def test_lifespan_closes_both_clients_and_worker():
    async def run():
        async with app.router.lifespan_context(app):
            clients = app.state.http_clients
            assert not clients.database.is_closed
            assert not clients.switch.is_closed
        assert clients.database.is_closed and clients.switch.is_closed
        assert app.state.ai_service.closed
    asyncio.run(run())


def test_shared_clients_use_current_credentials_and_separate_service_headers():
    async def run():
        calls = []

        async def handle(request):
            calls.append((request.url.host, request.headers.get("authorization"),
                          request.headers.get("apikey")))
            return httpx.Response(200, json={"result": {}} if request.method == "POST" else [])

        clients = HTTPClients()
        await clients.close()
        clients.database = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.switch = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        db_settings = settings().model_copy(update={
            "supabase_url": "https://fixture-db.invalid", "supabase_service_role_key": "fixture-db-key",
        })
        try:
            with use_http_clients(clients):
                await check_database_connection(db_settings)
                await check_database_connection(db_settings.model_copy(update={
                    "supabase_service_role_key": "fixture-replacement-key",
                }))
                api = SwitchAPI("https://fixture-switch.invalid", "fixture-agent", "fixture-switch-key")
                async with api.http() as first:
                    await api.operation(first, "get_room_detail", {})
                async with api.http() as second:
                    assert first is second is clients.switch
                assert not clients.switch.is_closed
            assert calls == [
                ("fixture-db.invalid", "Bearer fixture-db-key", "fixture-db-key"),
                ("fixture-db.invalid", "Bearer fixture-replacement-key", "fixture-replacement-key"),
                ("fixture-switch.invalid", "Bearer fixture-switch-key", None),
            ]
        finally:
            await clients.close()
    asyncio.run(run())


def test_slow_database_does_not_block_health_or_message_ingestion():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def handle(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json=[])

        async with app.router.lifespan_context(app):
            clients = app.state.http_clients
            await clients.database.aclose()
            clients.database = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            db_settings = settings().model_copy(update={
                "supabase_url": "https://fixture-db.invalid", "supabase_service_role_key": "fixture-db-key",
            })
            with use_http_clients(clients):
                task = asyncio.create_task(check_database_connection(db_settings))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="http://test") as browser:
                        response = await asyncio.wait_for(browser.get("/health"), 2)
                        assert response.status_code == 200
                        response = await asyncio.wait_for(browser.post("/messages", headers={
                            "X-Ingest-Token": "test-ingest-token",
                        }, json={"tenant_id": str(db_settings.switch_tenant_id), "room_id": "test-room",
                                 "message_id": "fixture-message", "sender": "fixture-person", "body": "Hello"}), 2)
                        assert response.status_code == 200
                    assert not task.done()
                finally:
                    release.set()
                    await task
    asyncio.run(run())


def test_queue_rejection_does_not_record_receipt_and_retry_runs_once(monkeypatch):
    async def run():
        gate = asyncio.Event()
        calls = []

        async def answer(settings, question):
            calls.append(question)
            await gate.wait()
            return '{"action":"none","answer":"Done"}'

        monkeypatch.setattr("app.services.ai_service.ask_switch", answer)
        service, receipts, config = AIService(), MessageReceiptRepository(), settings()

        def message(index):
            return IncomingMessage(tenant_id=config.switch_tenant_id, room_id="test-room",
                                   message_id=f"message-{index}", sender="fixture-person",
                                   sender_name="Person", body=f"Question {index}")

        try:
            first = await service.ingest(message(0), config, receipts)
            for index in range(1, 4):
                await service.ingest(message(index), config, receipts)
            with pytest.raises(HTTPException) as error:
                await service.ingest(message(4), config, receipts)
            assert error.value.status_code == 429
            assert len(receipts.recent(config.switch_tenant_id, config.allowed_switch_rooms)) == 4
            duplicate = await service.ingest(message(0), config, receipts)
            assert duplicate.duplicate and duplicate.message_ref == first.message_ref
            bot = message(8).model_copy(update={"sender": config.switch_ai_sender_id})
            await service.ingest(bot, config, receipts)
            assert service.pending == 4
            # Queue retains only redacted text, never the raw body in metadata.
            assert all("body" not in job[3] for job in service.queue._queue)
            await asyncio.sleep(0)
            assert calls == ["Question 0"]
            gate.set()
            await asyncio.wait_for(service.queue.join(), 2)
            retry = await service.ingest(message(4), config, receipts)
            assert not retry.duplicate
            await asyncio.wait_for(service.queue.join(), 2)
            assert len(calls) == 5 and service.pending == 0
        finally:
            await service.close()
    asyncio.run(run())


def test_aliases_are_bounded_and_do_not_move_a_running_result():
    repository, scope = AIRequestRepository(), ("fixture-scope",)
    original = uuid4()
    repository.create(scope, original, "Question", "a" * 24)
    for _ in range(1000):
        repository.rekey(scope, original, uuid4())
    assert len(repository.aliases) == 100
    assert len(repository.rows) == 1
    repository.update(scope, original, status="answered", answer="Done")
    for (_, alias), _ in repository.aliases.items():
        assert repository.get(scope, alias).answer == "Done"
    for _ in range(100):
        request_id = uuid4()
        repository.create(scope, request_id, "Another", None)
        repository.update(scope, request_id, status="answered")
    assert len(repository.rows) == 100 and not repository.aliases


def test_alias_of_active_job_observes_completion(monkeypatch):
    async def run():
        gate = asyncio.Event()

        async def answer(settings, question):
            await gate.wait()
            return "Done"

        monkeypatch.setattr("app.services.ai_service.ask_switch", answer)
        service, config, receipts = AIService(), settings(), MessageReceiptRepository()
        source = "a" * 24
        receipts.record(source, tenant_id=config.switch_tenant_id, room_id="test-room",
                        redacted_sender_name="Person", redacted_text="Question")
        first_id, alias_id = uuid4(), uuid4()
        try:
            await service.submit(AskRequest(request_id=first_id, source_message_ref=source), config, receipts)
            await asyncio.sleep(0)
            await service.submit(AskRequest(request_id=alias_id, source_message_ref=source), config, receipts)
            gate.set()
            await asyncio.wait_for(service.queue.join(), 2)
            assert service.repository.get(response_scope(config), alias_id).status == "answered"
            assert len(service.repository.rows) == 1
        finally:
            await service.close()
    asyncio.run(run())


def test_simultaneous_submissions_cannot_exceed_total_capacity(monkeypatch):
    async def run():
        gate = asyncio.Event()

        async def answer(settings, question):
            await gate.wait()
            return "Done"

        monkeypatch.setattr("app.services.ai_service.ask_switch", answer)
        service, config, receipts = AIService(), settings(), MessageReceiptRepository()
        try:
            results = await asyncio.gather(*[
                service.submit(AskRequest(request_id=uuid4(), question=f"Question {index}"), config, receipts)
                for index in range(20)
            ], return_exceptions=True)
            assert sum(not isinstance(row, Exception) for row in results) == 4
            rejected = [row for row in results if isinstance(row, Exception)]
            assert all(isinstance(row, HTTPException) and row.status_code == 429 for row in rejected)
            assert service.pending == 4 and len(service.tasks) == 1
        finally:
            await service.close()
        assert service.pending == 0 and service.queue.empty()
    asyncio.run(run())
