import json
import logging
from datetime import datetime
from uuid import UUID, uuid4
from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.core.message_logging import message_text_for_log, redact_source_identifier
from app.repositories.sqlite_db import get_sqlite_connection, init_sqlite_db, get_sqlite_file_path

logger = logging.getLogger("uvicorn.error")
ALLOWED_TABLES = ("commitments", "decisions", "messages", "memories", "documents")


def check_database_connection(settings: Settings | None = None) -> dict[str, str]:
    """Verify SQLite database connectivity and schema readiness."""
    active_settings = settings or get_settings()
    init_sqlite_db(active_settings)
    db_path = get_sqlite_file_path(active_settings)
    with get_sqlite_connection(active_settings) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages';")
        row = cursor.fetchone()
        if not row:
            raise HTTPException(503, "SQLite schema tables are not initialized")
    return {"status": "connected", "engine": "sqlite", "path": str(db_path)}


def list_records(table: str, settings: Settings | None = None) -> list[dict]:
    """Read recent records from the user-selected SQLite database."""
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Unknown table: {table}")
    active_settings = settings or get_settings()
    init_sqlite_db(active_settings)
    order_col = "occurred_at" if table == "messages" else "created_at"

    with get_sqlite_connection(active_settings) as conn:
        cursor = conn.cursor()
        if active_settings.switch_tenant_id is not None:
            cursor.execute(
                f"SELECT * FROM {table} WHERE tenant_id = ? ORDER BY {order_col} DESC LIMIT 50",
                (str(active_settings.switch_tenant_id),),
            )
        else:
            cursor.execute(f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT 50")
        rows = cursor.fetchall()

    result = []
    for row in rows:
        item = dict(row)
        if "embedding" in item and item["embedding"] is not None:
            del item["embedding"]
        result.append(item)
    return result


def _ensure_tenant_and_room(conn, tenant_id: str, switch_room_id: str) -> str:
    """Ensure tenant and room exist and return the internal room UUID."""
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, DATETIME('now'))",
            (tenant_id, "Default Tenant"),
        )

    cursor.execute(
        "SELECT id FROM rooms WHERE tenant_id = ? AND switch_room_id = ?",
        (tenant_id, switch_room_id),
    )
    row = cursor.fetchone()
    if row:
        return row[0]

    room_uuid = str(uuid4())
    cursor.execute(
        "INSERT INTO rooms (id, tenant_id, switch_room_id, name, created_at) VALUES (?, ?, ?, ?, DATETIME('now'))",
        (room_uuid, tenant_id, switch_room_id, f"Room {switch_room_id}"),
    )
    return room_uuid


async def persist_extraction(
    settings: Settings,
    *,
    tenant_id: UUID,
    switch_room_id: str,
    switch_message_id: str,
    author_name: str,
    body: str,
    occurred_at: datetime | str,
    items: dict,
    source_metadata: dict | None = None,
) -> dict[str, int]:
    """Store extracted items atomically into user-selected SQLite database."""
    if tenant_id != settings.switch_tenant_id or switch_room_id not in settings.allowed_switch_rooms:
        raise HTTPException(403, "Message tenant or room is not authorized")

    safe_source_id = redact_source_identifier(switch_message_id, settings)
    if safe_source_id is None:
        raise HTTPException(503, "Source metadata redaction unavailable; retry delivery")
    if safe_source_id != switch_message_id:
        raise HTTPException(422, "Invalid source identifier")

    init_sqlite_db(settings)
    str_tenant_id = str(tenant_id)
    occurred_str = occurred_at.isoformat() if isinstance(occurred_at, datetime) else str(occurred_at)
    meta = source_metadata or {}
    clean_body = json.loads(message_text_for_log(body, settings, max_length=64000)) or "[empty]"
    clean_author = json.loads(message_text_for_log(author_name or "Unknown sender", settings))

    commitments = items.get("commitments", [])
    decisions = items.get("decisions", [])
    memories = items.get("memories", [])

    with get_sqlite_connection(settings) as conn:
        with conn:
            cursor = conn.cursor()
            room_uuid = _ensure_tenant_and_room(conn, str_tenant_id, switch_room_id)

            cursor.execute(
                "SELECT id FROM messages WHERE switch_message_id = ?",
                (switch_message_id,),
            )
            existing_msg = cursor.fetchone()
            if existing_msg:
                message_uuid = existing_msg[0]
            else:
                message_uuid = str(uuid4())
                cursor.execute(
                    """
                    INSERT INTO messages (id, tenant_id, room_id, switch_message_id, switch_author_id,
                                          author_name, body, occurred_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, DATETIME('now'))
                    """,
                    (
                        message_uuid,
                        str_tenant_id,
                        room_uuid,
                        switch_message_id,
                        meta.get("switch_author_id"),
                        clean_author,
                        clean_body,
                        occurred_str,
                    ),
                )

            # Insert commitments
            for comm in commitments:
                comm_uuid = str(uuid4())
                cursor.execute(
                    """
                    INSERT INTO commitments (id, tenant_id, room_id, message_id, owner_name,
                                             description, due_at, approval_status, task_status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, DATETIME('now'))
                    """,
                    (
                        comm_uuid,
                        str_tenant_id,
                        room_uuid,
                        message_uuid,
                        comm.get("owner_name") or clean_author,
                        comm.get("description", ""),
                        comm.get("due_at"),
                        comm.get("approval_status", "pending"),
                        comm.get("task_status", "open"),
                    ),
                )

            # Insert decisions
            for dec in decisions:
                dec_uuid = str(uuid4())
                cursor.execute(
                    """
                    INSERT INTO decisions (id, tenant_id, room_id, message_id, topic,
                                           summary, approval_status, decision_status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, DATETIME('now'))
                    """,
                    (
                        dec_uuid,
                        str_tenant_id,
                        room_uuid,
                        message_uuid,
                        dec.get("topic", "General"),
                        dec.get("summary", ""),
                        dec.get("approval_status", "pending"),
                        dec.get("decision_status", "accepted"),
                    ),
                )

            # Insert memories
            import hashlib
            for mem in memories:
                mem_uuid = str(uuid4())
                content = mem.get("content", "")
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                cursor.execute(
                    """
                    INSERT INTO memories (id, tenant_id, room_id, message_id, content,
                                          memory_type, content_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, DATETIME('now'))
                    """,
                    (
                        mem_uuid,
                        str_tenant_id,
                        room_uuid,
                        message_uuid,
                        content,
                        mem.get("memory_type", "fact"),
                        content_hash,
                    ),
                )

    return {
        "messages": 1,
        "commitments": len(commitments),
        "decisions": len(decisions),
        "memories": len(memories),
    }


async def query_team_memory(
    settings: Settings,
    *,
    search_query: str | None = None,
    switch_room_id: str | None = None,
) -> dict[str, list[dict]]:
    """Query user-selected SQLite database for memories, commitments, decisions, and messages."""
    if settings.switch_tenant_id is None:
        raise HTTPException(503, "Message scope is not configured")

    init_sqlite_db(settings)
    str_tenant_id = str(settings.switch_tenant_id)
    keywords = [
        word.lower()
        for word in (search_query or "").split()
        if len(word.strip()) >= 2
    ]

    with get_sqlite_connection(settings) as conn:
        cursor = conn.cursor()
        allowed = settings.allowed_switch_rooms
        if switch_room_id:
            if switch_room_id not in allowed:
                raise HTTPException(403, "Room is not authorized")
            allowed = frozenset({switch_room_id})

        placeholders = ",".join("?" for _ in allowed)
        cursor.execute(
            f"SELECT id FROM rooms WHERE tenant_id = ? AND switch_room_id IN ({placeholders})",
            (str_tenant_id, *sorted(allowed)),
        )
        room_ids = [row[0] for row in cursor.fetchall()]
        if not room_ids:
            return {"memories": [], "commitments": [], "decisions": [], "messages": []}

        room_placeholders = ",".join("?" for _ in room_ids)
        result: dict[str, list[dict]] = {}

        specs = (
            ("memories", "id,message_id,room_id,memory_type,content,created_at", "created_at"),
            ("commitments", "id,message_id,room_id,description,owner_name,due_at,approval_status,task_status,created_at", "created_at"),
            ("decisions", "id,message_id,room_id,topic,summary,approval_status,decision_status,created_at", "created_at"),
            ("messages", "id,room_id,switch_message_id,author_name,body,occurred_at", "occurred_at"),
        )

        for table, cols, order_col in specs:
            query = f"SELECT {cols} FROM {table} WHERE tenant_id = ? AND room_id IN ({room_placeholders}) ORDER BY {order_col} DESC LIMIT 50"
            cursor.execute(query, (str_tenant_id, *room_ids))
            rows = [dict(r) for r in cursor.fetchall()]

            if keywords and rows:
                matched = [
                    row
                    for row in rows
                    if any(kw in json.dumps(row, default=str).lower() for kw in keywords)
                ]
                result[table] = (matched or rows)[:10]
            else:
                result[table] = rows[:10]

    return result

