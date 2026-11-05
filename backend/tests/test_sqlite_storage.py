from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi import HTTPException
import numpy as np

from app.core.config import Settings
from app.repositories import sqlite_db, sqlite_records, sqlite_embeddings
from app.repositories.record_repository import check_database_connection, persist_extraction, query_team_memory, list_records
from app.repositories.embedding_repository import claim_embeddings, finish_embedding, search_embeddings, source_context


@pytest.fixture
def temp_sqlite_dir(tmp_path: Path):
    db_file = tmp_path / "custom_storage" / "team_memory.db"
    return db_file


@pytest.fixture
def sqlite_settings(temp_sqlite_dir: Path):
    tenant_id = uuid4()
    return Settings(
        database_engine="sqlite",
        sqlite_db_path=str(temp_sqlite_dir),
        switch_tenant_id=tenant_id,
        switch_room_ids="room-1,room-2",
        switch_ingest_token="test-token",
    )


def test_missing_sqlite_path_raises_http_503():
    empty_settings = Settings(
        database_engine="sqlite",
        sqlite_db_path="",
    )
    with pytest.raises(HTTPException) as exc_info:
        sqlite_db.get_sqlite_file_path(empty_settings)
    assert exc_info.value.status_code == 503
    assert "Database is not configured" in exc_info.value.detail


def test_sqlite_schema_initialization(sqlite_settings: Settings, temp_sqlite_dir: Path):
    sqlite_db.init_sqlite_db(sqlite_settings)
    assert temp_sqlite_dir.exists()

    with sqlite_db.get_sqlite_connection(sqlite_settings) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}

    expected_tables = {
        "tenants",
        "rooms",
        "messages",
        "commitments",
        "decisions",
        "memories",
        "documents",
        "document_versions",
        "document_chunks",
        "document_imports",
    }
    for expected in expected_tables:
        assert expected in tables, f"Missing table {expected}"


@pytest.mark.anyio
async def test_sqlite_check_connection(sqlite_settings: Settings):
    status = await check_database_connection(sqlite_settings)
    assert status["status"] == "connected"
    assert status["engine"] == "sqlite"


@pytest.mark.anyio
async def test_sqlite_persist_and_list_records(sqlite_settings: Settings):
    sqlite_db.init_sqlite_db(sqlite_settings)
    room_id = "room-1"
    msg_id = "switch-msg-123"

    items = {
        "commitments": [{"owner_name": "Alice", "description": "Finish API by Friday", "due_at": "2026-11-06T12:00:00Z"}],
        "decisions": [{"topic": "Architecture", "summary": "Use SQLite locally"}],
        "memories": [{"content": "Alice owns SQLite migration", "memory_type": "fact"}],
    }

    counts = await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id=room_id,
        switch_message_id=msg_id,
        author_name="Alice",
        body="I will finish API by Friday and we decided to use SQLite locally.",
        occurred_at=datetime.now(timezone.utc),
        items=items,
    )

    assert counts == {"messages": 1, "commitments": 1, "decisions": 1, "memories": 1}

    # Verify listing records
    messages_sqlite = sqlite_records.list_records("messages", sqlite_settings)
    assert len(messages_sqlite) == 1
    assert messages_sqlite[0]["switch_message_id"] == msg_id

    commitments = sqlite_records.list_records("commitments", sqlite_settings)
    assert len(commitments) == 1
    assert commitments[0]["description"] == "Finish API by Friday"


@pytest.mark.anyio
async def test_sqlite_query_team_memory(sqlite_settings: Settings):
    sqlite_db.init_sqlite_db(sqlite_settings)
    room_id = "room-1"

    items = {
        "commitments": [],
        "decisions": [{"topic": "Engine Selection", "summary": "Chose SQLite for zero cloud latency"}],
        "memories": [{"content": "Project uses SQLite for fast response", "memory_type": "decision"}],
    }

    await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id=room_id,
        switch_message_id="msg-query-1",
        author_name="Bob",
        body="We decided SQLite gives zero latency",
        occurred_at=datetime.now(timezone.utc),
        items=items,
    )

    result = await query_team_memory(sqlite_settings, search_query="SQLite latency", switch_room_id=room_id)
    assert len(result["decisions"]) >= 1
    assert "zero" in result["decisions"][0]["summary"].lower()


@pytest.mark.anyio
async def test_sqlite_embeddings_and_vector_search(sqlite_settings: Settings):
    sqlite_db.init_sqlite_db(sqlite_settings)
    room_id = "room-1"

    items = {
        "commitments": [],
        "decisions": [],
        "memories": [
            {"content": "Alpha release scheduled for December", "memory_type": "fact"},
            {"content": "Beta release delayed until March", "memory_type": "fact"},
        ],
    }

    await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id=room_id,
        switch_message_id="msg-vec-1",
        author_name="Charlie",
        body="Alpha in December, Beta in March",
        occurred_at=datetime.now(timezone.utc),
        items=items,
    )

    # Claim jobs
    jobs = await claim_embeddings(sqlite_settings)
    assert len(jobs) == 2

    # Finish embedding with normalized dummy vectors
    vec1 = np.zeros(384, dtype=np.float32)
    vec1[0] = 1.0  # Represents Alpha
    vec2 = np.zeros(384, dtype=np.float32)
    vec2[1] = 1.0  # Represents Beta

    await finish_embedding(sqlite_settings, jobs[0], vector=vec1.tolist())
    await finish_embedding(sqlite_settings, jobs[1], vector=vec2.tolist())

    # Search for vector close to Alpha
    query_vec = np.zeros(384, dtype=np.float32)
    query_vec[0] = 0.99
    query_vec[1] = 0.05
    query_vec /= np.linalg.norm(query_vec)

    matches = await search_embeddings(sqlite_settings, query_vec.tolist(), switch_room_id=room_id)
    assert len(matches) == 2
    assert matches[0]["id"] == jobs[0]["id"]
    assert matches[0]["similarity"] > matches[1]["similarity"]

    # Verify source context join
    context = await source_context(sqlite_settings, matches)
    assert len(context["messages"]) >= 1
    assert context["messages"][0]["switch_message_id"] == "msg-vec-1"


@pytest.mark.anyio
async def test_migration_service_unconfigured_path_raises(tmp_path: Path):
    from app.services.migration_service import migrate_supabase_to_sqlite
    empty_settings = Settings(
        database_engine="sqlite",
        sqlite_db_path="",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="secret",
    )
    with pytest.raises(HTTPException) as exc:
        await migrate_supabase_to_sqlite(empty_settings)
    assert exc.value.status_code == 503
    assert "SQLite storage path is not configured" in exc.value.detail


@pytest.mark.anyio
async def test_migration_service_copies_records(sqlite_settings: Settings, monkeypatch):
    import httpx
    from app.services.migration_service import migrate_supabase_to_sqlite

    # Mock settings with Supabase credentials
    sqlite_settings.supabase_url = "https://mock.supabase.co"
    sqlite_settings.supabase_service_role_key = "mock-key"

    msg_id = str(uuid4())
    room_uuid = str(uuid4())
    fake_data = {
        "tenants": [{"id": str(sqlite_settings.switch_tenant_id), "discord_guild_id": "guild-1", "name": "Team", "settings": "{}", "created_at": "2026-11-05T00:00:00Z"}],
        "rooms": [{"id": room_uuid, "tenant_id": str(sqlite_settings.switch_tenant_id), "switch_room_id": "room-1", "name": "General", "created_at": "2026-11-05T00:00:00Z"}],
        "messages": [{"id": msg_id, "tenant_id": str(sqlite_settings.switch_tenant_id), "room_id": room_uuid, "switch_message_id": "s-1", "author_name": "Bob", "body": "test", "occurred_at": "2026-11-05T00:00:00Z", "created_at": "2026-11-05T00:00:00Z"}],
        "commitments": [{"id": str(uuid4()), "tenant_id": str(sqlite_settings.switch_tenant_id), "room_id": room_uuid, "message_id": msg_id, "owner_name": None, "description": "No owner", "approval_status": "pending", "task_status": "open", "created_at": "2026-11-05T00:00:00Z"}],
        "decisions": [{"id": str(uuid4()), "tenant_id": str(sqlite_settings.switch_tenant_id), "room_id": room_uuid, "message_id": msg_id, "topic": None, "summary": "No topic", "approval_status": "pending", "decision_status": "accepted", "created_at": "2026-11-05T00:00:00Z"}],
        "memories": [],
        "documents": [],
        "document_versions": [],
        "document_chunks": [],
        "document_imports": [],
    }

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            for table, rows in fake_data.items():
                if f"/rest/v1/{table}" in str(request.url):
                    return httpx.Response(200, json=rows)
            return httpx.Response(404, json=[])

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: orig_client(transport=MockTransport()))

    result = await migrate_supabase_to_sqlite(sqlite_settings)
    assert result["status"] == "ok"
    assert result["counts"]["tenants"] == 1
    assert result["counts"]["rooms"] == 1
    assert result["counts"]["commitments"] == 1
    assert result["counts"]["decisions"] == 1

