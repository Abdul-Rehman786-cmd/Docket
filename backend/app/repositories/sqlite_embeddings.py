import logging
import sqlite3
from uuid import UUID, uuid4
from fastapi import HTTPException
import numpy as np

from app.core.config import Settings, get_settings
from app.repositories.sqlite_db import get_sqlite_connection, init_sqlite_db

logger = logging.getLogger("uvicorn.error")


def _authorized_room_ids(settings: Settings, conn: sqlite3.Connection, switch_room_id: str | None = None) -> list[str]:
    if settings.switch_tenant_id is None or not settings.allowed_switch_rooms:
        raise HTTPException(503, "Message scope is not configured")
    rooms = settings.allowed_switch_rooms
    if switch_room_id:
        if switch_room_id not in rooms:
            raise HTTPException(403, "Room is not authorized")
        rooms = frozenset({switch_room_id})

    placeholders = ",".join("?" for _ in rooms)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT id FROM rooms WHERE tenant_id = ? AND switch_room_id IN ({placeholders})",
        (str(settings.switch_tenant_id), *sorted(rooms)),
    )
    rows = [r[0] for r in cursor.fetchall()]
    return rows


async def claim_embeddings(settings: Settings) -> list[dict]:
    """Claim pending memory embeddings for the local background embedder."""
    init_sqlite_db(settings)
    with get_sqlite_connection(settings) as conn:
        with conn:
            room_ids = _authorized_room_ids(settings, conn)
            if not room_ids:
                return []
            room_placeholders = ",".join("?" for _ in room_ids)
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT id, tenant_id, room_id, content, content_hash
                FROM memories
                WHERE tenant_id = ? AND room_id IN ({room_placeholders})
                  AND embedding IS NULL
                  AND (claim_expires_at IS NULL OR claim_expires_at < DATETIME('now'))
                LIMIT 8
                """,
                (str(settings.switch_tenant_id), *room_ids),
            )
            rows = cursor.fetchall()
            jobs = []
            for row in rows:
                token = str(uuid4())
                cursor.execute(
                    """
                    UPDATE memories
                    SET claim_token = ?, claim_expires_at = DATETIME('now', '+60 seconds')
                    WHERE id = ?
                    """,
                    (token, row["id"]),
                )
                jobs.append({
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "room_id": row["room_id"],
                    "content": row["content"],
                    "content_hash": row["content_hash"],
                    "claim_token": token,
                })
            return jobs


async def finish_embedding(settings: Settings, job: dict, *, vector: list[float] | None = None, error: str | None = None) -> bool:
    """Store computed 384d vector embedding into SQLite memories table."""
    init_sqlite_db(settings)
    with get_sqlite_connection(settings) as conn:
        with conn:
            cursor = conn.cursor()
            blob = None
            if vector is not None:
                arr = np.array(vector, dtype=np.float32)
                blob = arr.tobytes()

            cursor.execute(
                """
                UPDATE memories
                SET embedding = ?, error_code = ?, claim_token = NULL, claim_expires_at = NULL
                WHERE id = ? AND tenant_id = ? AND room_id = ?
                """,
                (blob, error, job["id"], job["tenant_id"], job["room_id"]),
            )
            changed = cursor.rowcount > 0

    if changed and settings.sqlite_max_space_enabled and settings.sqlite_max_space_value > 0:
        from app.repositories.sqlite_records import enforce_sqlite_max_space
        unit = (settings.sqlite_max_space_unit or "MB").strip().upper()
        multiplier = 1024 * 1024 * 1024 if unit == "GB" else (1024 if unit == "KB" else 1024 * 1024)
        max_bytes = int(settings.sqlite_max_space_value * multiplier)
        enforce_sqlite_max_space(max_bytes, settings)

    return changed


async def search_embeddings(
    settings: Settings,
    vector: list[float],
    *,
    switch_room_id: str | None = None,
) -> list[dict]:
    """Execute high-speed local cosine similarity search over SQLite embeddings."""
    init_sqlite_db(settings)
    query_vec = np.array(vector, dtype=np.float32)
    q_norm = np.linalg.norm(query_vec)
    if q_norm < 1e-7:
        return []

    with get_sqlite_connection(settings) as conn:
        room_ids = _authorized_room_ids(settings, conn, switch_room_id)
        if not room_ids:
            return []
        room_placeholders = ",".join("?" for _ in room_ids)
        cursor = conn.cursor()

        # 1. Search memories table
        cursor.execute(
            f"""
            SELECT id, tenant_id, room_id, message_id, content, memory_type, embedding, created_at
            FROM memories
            WHERE tenant_id = ? AND room_id IN ({room_placeholders}) AND embedding IS NOT NULL
            """,
            (str(settings.switch_tenant_id), *room_ids),
        )
        memory_rows = cursor.fetchall()
        scored_memories = []

        for row in memory_rows:
            blob = row["embedding"]
            if not blob:
                continue
            cand_vec = np.frombuffer(blob, dtype=np.float32)
            c_norm = np.linalg.norm(cand_vec)
            if c_norm < 1e-7:
                continue
            sim = float(np.dot(cand_vec, query_vec) / (c_norm * q_norm))
            item = dict(row)
            del item["embedding"]
            item["similarity"] = sim
            item["retrieval_mode"] = "semantic"
            scored_memories.append(item)

        scored_memories.sort(key=lambda x: x["similarity"], reverse=True)
        return scored_memories[:5]


async def source_context(settings: Settings, matches: list[dict]) -> dict:
    """Retrieve joined source messages, commitments, and decisions for vector matches."""
    init_sqlite_db(settings)
    if not matches:
        return {"commitments": [], "decisions": [], "messages": []}

    message_ids = list({str(m["message_id"]) for m in matches if m.get("message_id")})
    if not message_ids:
        return {"commitments": [], "decisions": [], "messages": []}

    with get_sqlite_connection(settings) as conn:
        room_ids = _authorized_room_ids(settings, conn)
        room_placeholders = ",".join("?" for _ in room_ids)
        msg_placeholders = ",".join("?" for _ in message_ids)
        cursor = conn.cursor()

        cursor.execute(
            f"""
            SELECT id, room_id, switch_message_id, author_name, occurred_at, body
            FROM messages
            WHERE tenant_id = ? AND room_id IN ({room_placeholders}) AND id IN ({msg_placeholders})
            """,
            (str(settings.switch_tenant_id), *room_ids, *message_ids),
        )
        messages = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            f"""
            SELECT id, message_id, room_id, description, owner_name, due_at, approval_status, task_status
            FROM commitments
            WHERE tenant_id = ? AND room_id IN ({room_placeholders}) AND message_id IN ({msg_placeholders})
            """,
            (str(settings.switch_tenant_id), *room_ids, *message_ids),
        )
        commitments = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            f"""
            SELECT id, message_id, room_id, topic, summary, approval_status, decision_status
            FROM decisions
            WHERE tenant_id = ? AND room_id IN ({room_placeholders}) AND message_id IN ({msg_placeholders})
            """,
            (str(settings.switch_tenant_id), *room_ids, *message_ids),
        )
        decisions = [dict(r) for r in cursor.fetchall()]

        return {
            "messages": messages,
            "commitments": commitments,
            "decisions": decisions,
        }

