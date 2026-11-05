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
        sqlite_max_space_enabled=False,
        storage_retention_unit="indefinite",
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


@pytest.mark.anyio
async def test_sqlite_delete_records_older_than_timeline(sqlite_settings: Settings):
    from datetime import timedelta
    from app.services.storage_service import prune_data_by_timeline

    sqlite_db.init_sqlite_db(sqlite_settings)
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=100)
    recent_time = now - timedelta(days=5)

    # Insert old message + child records
    await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id="room-1",
        switch_message_id="msg-old-1",
        author_name="OldUser",
        body="Old message from 100 days ago",
        occurred_at=old_time,
        items={
            "commitments": [{"owner_name": "OldUser", "description": "Old commitment"}],
            "decisions": [{"topic": "Old", "summary": "Old decision"}],
            "memories": [{"content": "Old fact", "memory_type": "fact"}],
        },
    )

    # Insert recent message + child records
    await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id="room-1",
        switch_message_id="msg-recent-1",
        author_name="NewUser",
        body="Recent message from 5 days ago",
        occurred_at=recent_time,
        items={
            "commitments": [{"owner_name": "NewUser", "description": "New commitment"}],
            "decisions": [{"topic": "New", "summary": "New decision"}],
            "memories": [{"content": "New fact", "memory_type": "fact"}],
        },
    )

    # Prune records older than 30 days
    res = await prune_data_by_timeline(
        engine="sqlite",
        retention_value=30,
        retention_unit="days",
        settings=sqlite_settings,
    )
    assert res["status"] == "ok"
    assert res["deleted"]["messages"] == 1
    assert res["deleted"]["commitments"] >= 1
    assert res["deleted"]["decisions"] >= 1
    assert res["deleted"]["memories"] >= 1

    # Verify only recent message and its children survive
    msgs = sqlite_records.list_records("messages", sqlite_settings)
    assert len(msgs) == 1
    assert msgs[0]["switch_message_id"] == "msg-recent-1"
    comms = sqlite_records.list_records("commitments", sqlite_settings)
    assert len(comms) == 1
    assert comms[0]["description"] == "New commitment"


@pytest.mark.anyio
async def test_supabase_delete_records_older_than_timeline(monkeypatch):
    import httpx
    from app.services.storage_service import prune_data_by_timeline

    supa_settings = Settings(
        database_engine="supabase",
        supabase_url="https://mock.supabase.co",
        supabase_service_role_key="mock-service-key",
        switch_tenant_id=uuid4(),
        switch_room_ids="room-1",
    )

    old_msg_id = str(uuid4())
    deleted_tables: list[str] = []

    async def mock_transport(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and "/rest/v1/messages" in url:
            return httpx.Response(200, json=[{"id": old_msg_id}])
        if request.method == "GET" and "/rest/v1/commitments" in url:
            return httpx.Response(200, json=[{"id": str(uuid4())}])
        if request.method == "GET" and "/rest/v1/documents" in url:
            return httpx.Response(200, json=[])
        if request.method == "DELETE":
            for tbl in ("reminder_deliveries", "memories", "commitments", "decisions", "messages"):
                if f"/rest/v1/{tbl}" in url:
                    deleted_tables.append(tbl)
                    return httpx.Response(200, json=[{"id": old_msg_id}])
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", mock_transport)

    res = await prune_data_by_timeline(
        engine="supabase",
        retention_value=30,
        retention_unit="days",
        settings=supa_settings,
    )
    assert res["status"] == "ok"
    assert res["engine"] == "supabase"
    assert "messages" in deleted_tables
    assert "memories" in deleted_tables
    assert "commitments" in deleted_tables
    assert "decisions" in deleted_tables
    assert res["deleted"]["messages"] == 1


@pytest.mark.anyio
async def test_sqlite_max_space_auto_evicts_oldest_data(sqlite_settings: Settings):
    from datetime import timedelta

    sqlite_db.init_sqlite_db(sqlite_settings)
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Insert 4 messages in chronological order (~1KB payload each, using spaces so not treated as opaque token)
    for i in range(4):
        await persist_extraction(
            sqlite_settings,
            tenant_id=sqlite_settings.switch_tenant_id,
            switch_room_id="room-1",
            switch_message_id=f"msg-seq-{i}",
            author_name=f"User{i}",
            body=f"Payload message {i} " + ("word " * 120),
            occurred_at=base_time + timedelta(days=i),
            items={
                "commitments": [],
                "decisions": [],
                "memories": [{"content": f"Memory {i} " + ("fact " * 60), "memory_type": "fact"}],
            },
        )

    # Enforce a tight max space limit of 2500 bytes (~2.5 KB) so only the newest 1-2 messages fit
    res = sqlite_records.enforce_sqlite_max_space(2500, sqlite_settings)
    assert res["enforced"] is True
    assert res["deleted_messages"] >= 2
    assert res["payload_bytes"] <= 2500

    surviving = sqlite_records.list_records("messages", sqlite_settings)
    surviving_ids = [m["switch_message_id"] for m in surviving]
    # Newest message msg-seq-3 must survive; oldest msg-seq-0 must be deleted
    assert "msg-seq-3" in surviving_ids
    assert "msg-seq-0" not in surviving_ids


@pytest.mark.anyio
async def test_sqlite_max_space_enforced_automatically_on_persist(sqlite_settings: Settings):
    from datetime import timedelta

    sqlite_settings.sqlite_max_space_enabled = True
    sqlite_settings.sqlite_max_space_value = 2.0  # 2 KB
    sqlite_settings.sqlite_max_space_unit = "KB"

    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        await persist_extraction(
            sqlite_settings,
            tenant_id=sqlite_settings.switch_tenant_id,
            switch_room_id="room-1",
            switch_message_id=f"auto-msg-{i}",
            author_name="Alice",
            body=f"Auto message {i} " + ("note " * 150),
            occurred_at=base_time + timedelta(hours=i),
            items={
                "commitments": [],
                "decisions": [],
                "memories": [{"content": f"Auto memory {i}", "memory_type": "fact"}],
            },
        )

    surviving = sqlite_records.list_records("messages", sqlite_settings)
    surviving_ids = [m["switch_message_id"] for m in surviving]
    assert "auto-msg-2" in surviving_ids
    assert "auto-msg-0" not in surviving_ids


@pytest.mark.anyio
async def test_storage_retention_worker_process_once(sqlite_settings: Settings):
    from datetime import timedelta
    from app.services.storage_service import StorageRetentionWorker

    sqlite_settings.storage_retention_unit = "indefinite"

    # Insert an old message (3 hours ago) and a fresh message (10 mins ago)
    now = datetime.now(timezone.utc)
    await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id="room-1",
        switch_message_id="worker-old-msg",
        author_name="Alice",
        body="Old body",
        occurred_at=now - timedelta(hours=3),
        items={"commitments": [], "decisions": [], "memories": [{"content": "old memory", "memory_type": "fact"}]},
    )
    await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id="room-1",
        switch_message_id="worker-fresh-msg",
        author_name="Bob",
        body="Fresh body",
        occurred_at=now - timedelta(minutes=10),
        items={"commitments": [], "decisions": [], "memories": [{"content": "fresh memory", "memory_type": "fact"}]},
    )

    # Now enable 1-hour retention and run worker
    sqlite_settings.storage_retention_value = 1
    sqlite_settings.storage_retention_unit = "hours"

    events = []
    worker = StorageRetentionWorker(on_change=lambda ev: events.append(ev))
    result = await worker.process_once(sqlite_settings)

    assert result["status"] == "ok"
    assert result["deleted"]["messages"] >= 1
    assert len(events) == 1
    assert events[0]["event"] == "records_pruned"

    surviving = sqlite_records.list_records("messages", sqlite_settings)
    surviving_ids = [m["switch_message_id"] for m in surviving]
    assert "worker-fresh-msg" in surviving_ids
    assert "worker-old-msg" not in surviving_ids


@pytest.mark.anyio
async def test_auto_prune_timeline_on_persist_extraction(sqlite_settings: Settings):
    from datetime import timedelta

    sqlite_settings.storage_retention_unit = "indefinite"
    now = datetime.now(timezone.utc)

    # Seed an old message from 5 hours ago
    await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id="room-1",
        switch_message_id="auto-prune-old",
        author_name="Alice",
        body="Old record",
        occurred_at=now - timedelta(hours=5),
        items={"commitments": [], "decisions": [], "memories": []},
    )

    # Configure 1 hour retention
    sqlite_settings.storage_retention_value = 1
    sqlite_settings.storage_retention_unit = "hours"

    # Persist new message: should automatically delete the 5-hour-old message
    await persist_extraction(
        sqlite_settings,
        tenant_id=sqlite_settings.switch_tenant_id,
        switch_room_id="room-1",
        switch_message_id="auto-prune-new",
        author_name="Bob",
        body="New record",
        occurred_at=now,
        items={"commitments": [], "decisions": [], "memories": []},
    )

    surviving = sqlite_records.list_records("messages", sqlite_settings)
    surviving_ids = [m["switch_message_id"] for m in surviving]
    assert "auto-prune-new" in surviving_ids
    assert "auto-prune-old" not in surviving_ids


@pytest.mark.anyio
async def test_storage_retention_worker_skips_indefinite(sqlite_settings: Settings):
    from app.services.storage_service import StorageRetentionWorker

    sqlite_settings.storage_retention_unit = "indefinite"
    worker = StorageRetentionWorker()
    result = await worker.process_once(sqlite_settings)
    assert result["status"] == "skipped"
    assert result["reason"] == "indefinite_retention"



