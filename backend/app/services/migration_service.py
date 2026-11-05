import json
import logging
from typing import Any
from uuid import UUID
import httpx
from fastapi import HTTPException
import numpy as np

from app.core.config import Settings, get_settings
from app.core.supabase_auth import backend_headers
from app.repositories.sqlite_db import get_sqlite_connection, init_sqlite_db, get_sqlite_file_path

logger = logging.getLogger("uvicorn.error")

MIGRATION_TABLES = [
    ("tenants", ["id", "discord_guild_id", "name", "settings", "created_at"]),
    ("rooms", ["id", "tenant_id", "switch_room_id", "name", "created_at"]),
    ("room_members", ["tenant_id", "room_id", "switch_user_id", "role", "created_at"]),
    ("messages", ["id", "tenant_id", "room_id", "switch_message_id", "switch_author_id", "author_name", "body", "occurred_at", "created_at"]),
    ("commitments", ["id", "tenant_id", "room_id", "message_id", "owner_name", "description", "due_at", "approval_status", "task_status", "created_at"]),
    ("decisions", ["id", "tenant_id", "room_id", "message_id", "topic", "summary", "approval_status", "decision_status", "created_at"]),
    ("memories", ["id", "tenant_id", "room_id", "message_id", "content", "memory_type", "content_hash", "embedding", "active", "created_at"]),
    ("person_profiles", ["id", "tenant_id", "room_id", "switch_author_id", "display_name", "created_at"]),
    ("person_profile_claims", ["id", "tenant_id", "room_id", "profile_id", "extraction_key", "attribute", "value", "status", "created_at"]),
    ("person_profile_evidence", ["tenant_id", "room_id", "claim_id", "message_id", "memory_id", "quote", "correction", "occurred_at"]),
    ("documents", ["id", "tenant_id", "room_id", "source_key", "source_type", "title", "created_by", "latest_version", "published_version_id", "active", "created_at"]),
    ("document_versions", ["id", "tenant_id", "room_id", "document_id", "version_number", "title", "filename", "source_text", "content_hash", "recipe", "model_recipe", "status", "error", "created_at"]),
    ("document_chunks", ["id", "tenant_id", "room_id", "document_id", "version_id", "position", "text", "content_hash", "start_char", "end_char", "start_line", "end_line", "section", "model_recipe", "status", "attempts", "error", "retry_at", "lease_until", "claim_token", "embedding", "embedding_content_hash", "created_at"]),
    ("document_imports", ["tenant_id", "room_id", "import_id", "document_id", "version_id", "request_hash"]),
]


def _vector_to_blob(val: Any) -> bytes | None:
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = json.loads(val)
        if isinstance(val, list):
            return np.array(val, dtype=np.float32).tobytes()
        if isinstance(val, (bytes, bytearray)):
            return bytes(val)
    except Exception as exc:
        logger.warning("Could not convert embedding vector to blob: %s", exc)
    return None


async def migrate_supabase_to_sqlite(settings: Settings | None = None) -> dict[str, Any]:
    """Pull records from Supabase REST and populate user-selected SQLite database."""
    active_settings = settings or get_settings()

    if not active_settings.sqlite_configured:
        logger.error("Migration aborted: SQLite storage path is not configured.")
        raise HTTPException(
            status_code=503,
            detail="SQLite storage path is not configured. Please select or specify a directory in Settings.",
        )

    if not active_settings.supabase_configured:
        logger.error("Migration aborted: Supabase credentials are not configured in backend/.env.")
        raise HTTPException(
            status_code=503,
            detail="Supabase credentials are not configured. Cannot read source records.",
        )

    init_sqlite_db(active_settings)
    target_path = get_sqlite_file_path(active_settings)
    logger.info("Starting Supabase -> SQLite migration targeting %s", target_path)

    key = active_settings.supabase_secret_key.strip() or active_settings.supabase_service_role_key.strip()
    headers = backend_headers(key)
    base_url = active_settings.supabase_url.rstrip("/")

    counts: dict[str, int] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        with get_sqlite_connection(active_settings) as conn:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('DROP TRIGGER IF EXISTS refresh_profile_insert')
                conn.execute('DROP TRIGGER IF EXISTS refresh_profile_delete')
                cursor = conn.cursor()
                for table, columns in MIGRATION_TABLES:
                    url = f"{base_url}/rest/v1/{table}"
                    params = {"select": "*", "limit": "5000"}
                    if active_settings.switch_tenant_id is not None and table != "tenants":
                        params["tenant_id"] = f"eq.{active_settings.switch_tenant_id}"

                    try:
                        resp = await client.get(url, headers=headers, params=params)
                        if resp.status_code == 404:
                            logger.info("Table %s not found in Supabase REST; skipping.", table)
                            counts[table] = 0
                            continue
                        resp.raise_for_status()
                        rows = resp.json()
                    except Exception as exc:
                        if table.startswith('person_profile'):
                            raise HTTPException(503, 'Profile migration failed; transaction rolled back') from None
                        logger.warning("Error fetching table %s from Supabase: %s", table, exc)
                        counts[table] = 0
                        continue

                    if not isinstance(rows, list):
                        if table.startswith('person_profile'):
                            raise HTTPException(503, 'Profile migration failed; transaction rolled back')
                        counts[table] = 0
                        continue

                    migrated_table_count = 0
                    for row in rows:
                        row_cols = [c for c in columns if c in row]
                        vals = []
                        for c in row_cols:
                            v = row[c]
                            if c == "embedding":
                                v = _vector_to_blob(v)
                            elif c == 'occurred_at' and table == 'person_profile_evidence':
                                from app.services.profile_service import observed_time
                                v = observed_time(v)
                            elif isinstance(v, (dict, list)) and c != "embedding":
                                v = json.dumps(v)
                            vals.append(v)

                        placeholders = ",".join("?" for _ in row_cols)
                        col_names = ",".join(row_cols)
                        # UPSERT preserves dependent evidence on repeated imports;
                        # REPLACE deletes its old parent and cascades child deletion.
                        primary_keys = {'room_members': ['tenant_id','room_id','switch_user_id'],
                            'person_profile_evidence': ['claim_id','message_id'],
                            'document_imports': ['tenant_id','import_id']}.get(table, ['id'])
                        updates = ','.join(f'{c}=excluded.{c}' for c in row_cols if c not in primary_keys)
                        query = (f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
                                 f"ON CONFLICT({','.join(primary_keys)}) DO " +
                                 (f"UPDATE SET {updates}" if updates else 'NOTHING'))
                        cursor.execute(query, tuple(vals))
                        migrated_table_count += 1

                    counts[table] = migrated_table_count
                    logger.info("Migrated %d records for table %s", migrated_table_count, table)

                # Evidence triggers recompute status during each insert; recompute
                # all imported profiles after the complete transaction's data is present.
                from app.repositories.sqlite_profiles import refresh_sql, trigger_statements
                for profile in conn.execute('SELECT id FROM person_profiles').fetchall():
                    # Execute statements individually: executescript commits implicitly.
                    for statement in refresh_sql("'" + str(UUID(profile[0])) + "'").split(';'):
                        if statement.strip():
                            conn.execute(statement)
                for statement in trigger_statements():
                    conn.execute(statement)

    logger.info("Supabase -> SQLite migration finished successfully: %s", counts)
    return {
        "status": "ok",
        "target_path": str(target_path),
        "counts": counts,
    }

