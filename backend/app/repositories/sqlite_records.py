import json
import logging
from datetime import datetime, timedelta, timezone
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


def _ensure_tenant_and_room(conn, tenant_id: str, switch_room_id: str, platform: str = "discord") -> str:
    """Ensure tenant and room exist and return the internal room UUID."""
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, DATETIME('now'))",
            (tenant_id, "Default Tenant"),
        )

    cursor.execute(
        "SELECT id, platform FROM rooms WHERE tenant_id = ? AND switch_room_id = ?",
        (tenant_id, switch_room_id),
    )
    row = cursor.fetchone()
    if row:
        if platform and row[1] != platform:
            cursor.execute("UPDATE rooms SET platform = ? WHERE id = ?", (platform, row[0]))
        return row[0]

    room_uuid = str(uuid4())
    cursor.execute(
        "INSERT INTO rooms (id, tenant_id, switch_room_id, name, platform, created_at) VALUES (?, ?, ?, ?, ?, DATETIME('now'))",
        (room_uuid, tenant_id, switch_room_id, f"Room {switch_room_id}", platform),
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
    facts = items.get("profile_facts", [])
    memory_ids = {}
    if facts:
        from app.services.profile_service import profile_items
        from app.repositories.sqlite_profiles import replay_hash
        # Validate before opening a write transaction; never trust caller keys.
        checked = profile_items(facts, settings, meta, clean_body)
        if checked != facts:
            raise HTTPException(422, "Invalid profile storage items")
        request_hash = replay_hash(items, meta, clean_body, occurred_at)

    with get_sqlite_connection(settings) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            msg_platform = meta.get("origin") or meta.get("platform") or "discord"
            room_uuid = _ensure_tenant_and_room(conn, str_tenant_id, switch_room_id, platform=msg_platform)

            cursor.execute(
                "SELECT id FROM messages WHERE switch_message_id = ?",
                (switch_message_id,),
            )
            existing_msg = cursor.fetchone()
            if existing_msg:
                message_uuid = existing_msg[0]
                if facts:
                    source = conn.execute("SELECT * FROM messages WHERE id=?", (message_uuid,)).fetchone()
                    if (source['tenant_id'], source['room_id'], source['switch_author_id'], source['body']) != (
                            str_tenant_id, room_uuid, meta.get('switch_author_id'), clean_body):
                        raise HTTPException(409, "Profile source conflict")
                    replay = conn.execute("SELECT request_hash,counts FROM profile_extractions WHERE message_id=?",
                                          (message_uuid,)).fetchone()
                    if replay:
                        if replay['request_hash'] != request_hash:
                            raise HTTPException(409, "Profile extraction conflict")
                        return json.loads(replay['counts'])
                    raise HTTPException(409, "Profile backfill requires a separate operation")
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
                app_status = comm.get("approval_status") or ("approved" if comm.get("due_at") else "pending")
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
                        app_status,
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
                memory_ids[mem.get('extraction_key')] = mem_uuid
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

            if facts:
                from app.repositories.sqlite_profiles import store
                store(conn, settings, tenant_id=str_tenant_id, room_id=room_uuid, message_id=message_uuid,
                      author_name=clean_author, body=clean_body, metadata=meta, occurred_at=occurred_at,
                      facts=facts, memories=memory_ids)
                counts = dict(messages=1, commitments=len(commitments), decisions=len(decisions), memories=len(memories))
                conn.execute("INSERT INTO profile_extractions VALUES(?,?,?)", (message_uuid, request_hash, json.dumps(counts)))

    if settings.sqlite_max_space_enabled and settings.sqlite_max_space_value > 0:
        unit = (settings.sqlite_max_space_unit or "MB").strip().upper()
        multiplier = 1024 * 1024 * 1024 if unit == "GB" else (1024 if unit == "KB" else 1024 * 1024)
        max_bytes = int(settings.sqlite_max_space_value * multiplier)
        # When new messages/commitments/decisions/memories are added, target those tables
        added_tables = ["messages"]
        if commitments:
            added_tables.append("commitments")
        if decisions:
            added_tables.append("decisions")
        if memories:
            added_tables.append("memories")
        enforce_sqlite_max_space(
            max_bytes,
            settings,
            preserve_message_ids={message_uuid},
            target_tables=added_tables,
        )

    # Automatically prune records older than timeline if retention is not indefinite
    if settings.storage_retention_unit and settings.storage_retention_unit.lower() != "indefinite":
        now = datetime.now(timezone.utc)
        unit = settings.storage_retention_unit.lower()
        val = settings.storage_retention_value
        cutoff = None
        if val > 0:
            if unit == "minutes":
                cutoff = now - timedelta(minutes=val)
            elif unit == "hours":
                cutoff = now - timedelta(hours=val)
            elif unit == "days":
                cutoff = now - timedelta(days=val)
            elif unit == "weeks":
                cutoff = now - timedelta(weeks=val)
            elif unit == "months":
                cutoff = now - timedelta(days=val * 30)
            elif unit == "years":
                cutoff = now - timedelta(days=val * 365)
        if cutoff is not None:
            delete_records_older_than_sqlite(cutoff, settings)

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
            active_filter = " AND active=1" if table == "memories" else ""
            query = f"SELECT {cols} FROM {table} WHERE tenant_id = ? AND room_id IN ({room_placeholders}){active_filter} ORDER BY {order_col} DESC LIMIT 50"
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


def get_sqlite_storage_stats(settings: Settings | None = None) -> dict[str, int | float | str | bool]:
    """Return actual SQLite database disk size and logical page usage in bytes."""
    active = settings or get_settings()
    if not active.sqlite_configured:
        return {
            "configured": False,
            "engine": "sqlite",
            "used_bytes": 0,
            "file_bytes": 0,
            "logical_bytes": 0,
            "path": "",
        }
    init_sqlite_db(active)
    db_file = get_sqlite_file_path(active)
    file_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = db_file.parent / f"{db_file.name}{suffix}"
        if candidate.exists():
            try:
                file_bytes += candidate.stat().st_size
            except OSError:
                pass

    logical_bytes = 0
    payload_bytes = 0
    row_counts: dict[str, int] = {}
    with get_sqlite_connection(active) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA page_count;")
        page_count = (cursor.fetchone() or [0])[0]
        cursor.execute("PRAGMA freelist_count;")
        freelist_count = (cursor.fetchone() or [0])[0]
        cursor.execute("PRAGMA page_size;")
        page_size = (cursor.fetchone() or [4096])[0]
        logical_bytes = max(0, (page_count - freelist_count) * page_size)

        for tbl in ("messages", "commitments", "decisions", "memories", "documents", "person_profiles", "person_profile_claims", "person_profile_evidence"):
            try:
                if active.switch_tenant_id is not None:
                    cursor.execute(f"SELECT COUNT(*) FROM {tbl} WHERE tenant_id = ?", (str(active.switch_tenant_id),))
                else:
                    cursor.execute(f"SELECT COUNT(*) FROM {tbl}")
                row_counts[tbl] = (cursor.fetchone() or [0])[0]
            except Exception:
                row_counts[tbl] = 0

        try:
            cursor.execute("SELECT COALESCE(SUM(LENGTH(body) + LENGTH(author_name) + 128), 0) FROM messages")
            payload_bytes += int((cursor.fetchone() or [0])[0])
            cursor.execute("SELECT COALESCE(SUM(LENGTH(description) + COALESCE(LENGTH(owner_name), 0) + 128), 0) FROM commitments")
            payload_bytes += int((cursor.fetchone() or [0])[0])
            cursor.execute("SELECT COALESCE(SUM(LENGTH(summary) + COALESCE(LENGTH(topic), 0) + 128), 0) FROM decisions")
            payload_bytes += int((cursor.fetchone() or [0])[0])
            cursor.execute("SELECT COALESCE(SUM(LENGTH(content) + COALESCE(LENGTH(embedding), 0) + 128), 0) FROM memories")
            payload_bytes += int((cursor.fetchone() or [0])[0])
            cursor.execute("SELECT COALESCE(SUM(LENGTH(source_text) + 256), 0) FROM document_versions")
            payload_bytes += int((cursor.fetchone() or [0])[0])
            cursor.execute("SELECT COALESCE(SUM(LENGTH(text) + COALESCE(LENGTH(embedding), 0) + 128), 0) FROM document_chunks")
            payload_bytes += int((cursor.fetchone() or [0])[0])
            for table, fields in (("person_profiles", "LENGTH(switch_author_id)+LENGTH(display_name)"),
                                  ("person_profile_claims", "LENGTH(attribute)+LENGTH(value)"),
                                  ("person_profile_evidence", "LENGTH(quote)"),
                                  ("profile_extractions", "LENGTH(request_hash)+LENGTH(counts)")):
                cursor.execute(f"SELECT COALESCE(SUM({fields}+128),0) FROM {table}")
                payload_bytes += int(cursor.fetchone()[0])
        except Exception:
            pass

    return {
        "configured": True,
        "engine": "sqlite",
        "path": str(db_file),
        "used_bytes": logical_bytes if logical_bytes > 0 else file_bytes,
        "file_bytes": file_bytes,
        "logical_bytes": logical_bytes,
        "payload_bytes": payload_bytes,
        "row_counts": row_counts,
    }


def delete_records_older_than_sqlite(
    cutoff: datetime | str,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Atomically delete all records older than cutoff in SQLite following ATOM principles."""
    active = settings or get_settings()
    init_sqlite_db(active)
    if isinstance(cutoff, datetime):
        cutoff_param = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    else:
        cutoff_param = str(cutoff).replace("T", " ")[:19]

    tenant_clause = "AND tenant_id = ?" if active.switch_tenant_id is not None else ""
    tenant_params = (str(active.switch_tenant_id),) if active.switch_tenant_id is not None else ()

    deleted_counts = {
        "memories": 0,
        "commitments": 0,
        "decisions": 0,
        "messages": 0,
        "documents": 0,
    }

    with get_sqlite_connection(active) as conn:
        with conn:
            cursor = conn.cursor()
            # 1. Find old messages (by occurred_at or created_at)
            cursor.execute(
                f"""
                SELECT id FROM messages
                WHERE (datetime(occurred_at) < datetime(?) OR datetime(created_at) < datetime(?))
                {tenant_clause}
                """,
                (cutoff_param, cutoff_param, *tenant_params),
            )
            old_msg_ids = [r[0] for r in cursor.fetchall()]

            # Delete child records explicitly as well as standalone old child rows
            if old_msg_ids:
                placeholders = ",".join("?" for _ in old_msg_ids)
                for child_table in ("memories", "commitments", "decisions"):
                    cursor.execute(
                        f"DELETE FROM {child_table} WHERE message_id IN ({placeholders})",
                        tuple(old_msg_ids),
                    )
                    deleted_counts[child_table] += max(0, cursor.rowcount)

            for child_table in ("memories", "commitments", "decisions"):
                cursor.execute(
                    f"""
                    DELETE FROM {child_table}
                    WHERE datetime(created_at) < datetime(?)
                    {tenant_clause}
                    """,
                    (cutoff_param, *tenant_params),
                )
                deleted_counts[child_table] += max(0, cursor.rowcount)

            if old_msg_ids:
                placeholders = ",".join("?" for _ in old_msg_ids)
                cursor.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})",
                    tuple(old_msg_ids),
                )
                deleted_counts["messages"] += max(0, cursor.rowcount)

            # Old documents & chunks
            cursor.execute(
                f"""
                SELECT id FROM documents
                WHERE datetime(created_at) < datetime(?)
                {tenant_clause}
                """,
                (cutoff_param, *tenant_params),
            )
            old_doc_ids = [r[0] for r in cursor.fetchall()]
            if old_doc_ids:
                doc_ph = ",".join("?" for _ in old_doc_ids)
                cursor.execute(f"DELETE FROM document_imports WHERE document_id IN ({doc_ph})", tuple(old_doc_ids))
                cursor.execute(f"DELETE FROM document_chunks WHERE document_id IN ({doc_ph})", tuple(old_doc_ids))
                cursor.execute(f"DELETE FROM document_versions WHERE document_id IN ({doc_ph})", tuple(old_doc_ids))
                cursor.execute(f"DELETE FROM documents WHERE id IN ({doc_ph})", tuple(old_doc_ids))
                deleted_counts["documents"] += max(0, cursor.rowcount)

        # Reclaim disk space after transaction commits
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("VACUUM;")
        except Exception as exc:
            logger.debug("SQLite vacuum after timeline delete skipped: %s", exc)

    return deleted_counts


def enforce_sqlite_max_space(
    max_bytes: int,
    settings: Settings | None = None,
    *,
    preserve_message_ids: set[str] | None = None,
    target_tables: list[str] | None = None,
) -> dict[str, int | bool]:
    """Delete oldest data in SQLite atomically until storage space is <= max_bytes.
    
    If target_tables is provided (e.g. ['memories'] or ['messages']), eviction prioritizes
    the specific table(s) where new data was added.
    """
    active = settings or get_settings()
    if max_bytes <= 0 or not active.sqlite_configured:
        return {
            "enforced": False,
            "deleted_messages": 0,
            "deleted_memories": 0,
            "deleted_commitments": 0,
            "deleted_decisions": 0,
            "deleted_documents": 0,
            "used_bytes": 0,
            "payload_bytes": 0,
            "max_bytes": max_bytes,
        }

    init_sqlite_db(active)

    def _effective_size(st: dict) -> int:
        payload = int(st.get("payload_bytes", 0))
        logical = int(st.get("used_bytes", 0))
        if max_bytes < 262144 or payload > 0:
            return payload
        return logical

    stats = get_sqlite_storage_stats(active)
    current_size = _effective_size(stats)

    deleted_totals = {
        "deleted_messages": 0,
        "deleted_memories": 0,
        "deleted_commitments": 0,
        "deleted_decisions": 0,
        "deleted_documents": 0,
    }

    if current_size <= max_bytes:
        return {
            "enforced": True,
            **deleted_totals,
            "used_bytes": int(stats["used_bytes"]),
            "payload_bytes": int(stats.get("payload_bytes", 0)),
            "file_bytes": int(stats["file_bytes"]),
            "max_bytes": max_bytes,
        }

    preserve = preserve_message_ids or set()
    tenant_clause = "WHERE tenant_id = ?" if active.switch_tenant_id is not None else ""
    tenant_params = (str(active.switch_tenant_id),) if active.switch_tenant_id is not None else ()
    tables_to_evict = target_tables or ["messages", "memories", "commitments", "decisions", "documents"]

    # Iteratively evict oldest records from the targeted table first in atomic batches
    max_iterations = 500
    for _ in range(max_iterations):
        stats = get_sqlite_storage_stats(active)
        current_size = _effective_size(stats)
        if current_size <= max_bytes:
            break

        progressed = False
        with get_sqlite_connection(active) as conn:
            with conn:
                cursor = conn.cursor()

                for tbl in tables_to_evict:
                    if tbl == "memories":
                        cursor.execute(
                            f"SELECT id FROM memories {tenant_clause} ORDER BY created_at ASC LIMIT 1",
                            tenant_params,
                        )
                        row = cursor.fetchone()
                        if row:
                            cursor.execute("DELETE FROM memories WHERE id = ?", (row[0],))
                            deleted_totals["deleted_memories"] += max(0, cursor.rowcount)
                            progressed = True
                            break

                    elif tbl == "commitments":
                        cursor.execute(
                            f"SELECT id FROM commitments {tenant_clause} ORDER BY created_at ASC LIMIT 1",
                            tenant_params,
                        )
                        row = cursor.fetchone()
                        if row:
                            cursor.execute("DELETE FROM commitments WHERE id = ?", (row[0],))
                            deleted_totals["deleted_commitments"] += max(0, cursor.rowcount)
                            progressed = True
                            break

                    elif tbl == "decisions":
                        cursor.execute(
                            f"SELECT id FROM decisions {tenant_clause} ORDER BY created_at ASC LIMIT 1",
                            tenant_params,
                        )
                        row = cursor.fetchone()
                        if row:
                            cursor.execute("DELETE FROM decisions WHERE id = ?", (row[0],))
                            deleted_totals["deleted_decisions"] += max(0, cursor.rowcount)
                            progressed = True
                            break

                    elif tbl == "messages":
                        where_parts = []
                        params: list[str] = []
                        if active.switch_tenant_id is not None:
                            where_parts.append("tenant_id = ?")
                            params.append(str(active.switch_tenant_id))
                        if preserve:
                            excl_ph = ",".join("?" for _ in preserve)
                            where_parts.append(f"id NOT IN ({excl_ph})")
                            params.extend(sorted(preserve))

                        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
                        cursor.execute(
                            f"""
                            SELECT id FROM messages
                            {where_sql}
                            ORDER BY occurred_at ASC, created_at ASC
                            LIMIT 1
                            """,
                            tuple(params),
                        )
                        oldest_msg_ids = [r[0] for r in cursor.fetchall()]

                        if oldest_msg_ids:
                            ph = ",".join("?" for _ in oldest_msg_ids)
                            cursor.execute(f"DELETE FROM memories WHERE message_id IN ({ph})", tuple(oldest_msg_ids))
                            deleted_totals["deleted_memories"] += max(0, cursor.rowcount)
                            cursor.execute(f"DELETE FROM commitments WHERE message_id IN ({ph})", tuple(oldest_msg_ids))
                            deleted_totals["deleted_commitments"] += max(0, cursor.rowcount)
                            cursor.execute(f"DELETE FROM decisions WHERE message_id IN ({ph})", tuple(oldest_msg_ids))
                            deleted_totals["deleted_decisions"] += max(0, cursor.rowcount)
                            cursor.execute(f"DELETE FROM messages WHERE id IN ({ph})", tuple(oldest_msg_ids))
                            deleted_totals["deleted_messages"] += max(0, cursor.rowcount)
                            progressed = True
                            break

                    elif tbl == "documents":
                        doc_where = "WHERE tenant_id = ?" if active.switch_tenant_id is not None else ""
                        doc_params = (str(active.switch_tenant_id),) if active.switch_tenant_id is not None else ()
                        cursor.execute(
                            f"SELECT id FROM documents {doc_where} ORDER BY created_at ASC LIMIT 1",
                            doc_params,
                        )
                        oldest_docs = [r[0] for r in cursor.fetchall()]
                        if oldest_docs:
                            dph = ",".join("?" for _ in oldest_docs)
                            cursor.execute(f"DELETE FROM document_imports WHERE document_id IN ({dph})", tuple(oldest_docs))
                            cursor.execute(f"DELETE FROM document_chunks WHERE document_id IN ({dph})", tuple(oldest_docs))
                            cursor.execute(f"DELETE FROM document_versions WHERE document_id IN ({dph})", tuple(oldest_docs))
                            cursor.execute(f"DELETE FROM documents WHERE id IN ({dph})", tuple(oldest_docs))
                            deleted_totals["deleted_documents"] += max(0, cursor.rowcount)
                            progressed = True
                            break

                # If the targeted table had nothing to delete, fallback to any oldest message
                if not progressed and target_tables:
                    cursor.execute(
                        f"""
                        SELECT id FROM messages
                        {tenant_clause}
                        ORDER BY occurred_at ASC, created_at ASC
                        LIMIT 1
                        """,
                        tenant_params,
                    )
                    old_msg = cursor.fetchone()
                    if old_msg:
                        mid = old_msg[0]
                        cursor.execute("DELETE FROM memories WHERE message_id = ?", (mid,))
                        deleted_totals["deleted_memories"] += max(0, cursor.rowcount)
                        cursor.execute("DELETE FROM commitments WHERE message_id = ?", (mid,))
                        deleted_totals["deleted_commitments"] += max(0, cursor.rowcount)
                        cursor.execute("DELETE FROM decisions WHERE message_id = ?", (mid,))
                        deleted_totals["deleted_decisions"] += max(0, cursor.rowcount)
                        cursor.execute("DELETE FROM messages WHERE id = ?", (mid,))
                        deleted_totals["deleted_messages"] += max(0, cursor.rowcount)
                        progressed = True

            if progressed:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    conn.execute("VACUUM;")
                except Exception:
                    pass

        if not progressed:
            break

    final_stats = get_sqlite_storage_stats(active)
    return {
        "enforced": True,
        **deleted_totals,
        "used_bytes": int(final_stats["used_bytes"]),
        "payload_bytes": int(final_stats.get("payload_bytes", 0)),
        "file_bytes": int(final_stats["file_bytes"]),
        "max_bytes": max_bytes,
    }


