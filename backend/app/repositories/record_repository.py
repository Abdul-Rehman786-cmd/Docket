from datetime import datetime, timezone
import json
import logging
from uuid import UUID

import httpx
from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.core.http_clients import database_client
from app.core.message_logging import message_text_for_log
from app.models.ai import LLMDecision

ALLOWED_TABLES = ("commitments", "decisions", "messages", "memories")
logger = logging.getLogger("uvicorn.error")


def _supabase_headers(settings: Settings, *, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _rest_url(settings: Settings, table: str) -> str:
    if not settings.supabase_configured:
        missing = []
        if not settings.supabase_url.strip():
            missing.append("SUPABASE_URL")
        if not settings.supabase_service_role_key.strip():
            missing.append("SUPABASE_SERVICE_ROLE_KEY")
        logger.warning(
            "Supabase is not configured; set %s in backend/.env",
            " and ".join(missing),
        )
        raise HTTPException(503, "Database is not configured")
    return f"{settings.supabase_url.rstrip('/')}/rest/v1/{table}"


def _redact(text: str | None, settings: Settings) -> str | None:
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    return json.loads(message_text_for_log(cleaned, settings))


def _valid_iso_datetime(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        return None


@database_client
async def check_database_connection(settings: Settings | None = None, *, client) -> dict[str, str]:
    """Verify Supabase REST connectivity using configured service-role credentials."""
    active_settings = settings or get_settings()
    url = _rest_url(active_settings, "tenants")
    try:
        response = await client.get(
            url,
            params={"select": "id", "limit": "1"},
            headers=_supabase_headers(active_settings),
            timeout=10,
        )
        response.raise_for_status()
        if not isinstance(response.json(), list):
            raise ValueError("Unexpected database response")
        return {"status": "connected"}
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Supabase connection check failed status=%d (verify schema migration and service-role key)",
            exc.response.status_code,
        )
        raise HTTPException(503, "Database request failed") from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Supabase connection check failed reason=%s", type(exc).__name__)
        raise HTTPException(503, "Database request failed") from exc


@database_client
async def list_records(table: str, *, client) -> list[dict]:
    """Read records via Supabase REST. Only invoked by protected backend endpoints."""
    if table not in ALLOWED_TABLES:
        raise ValueError("Unknown table")
    settings = get_settings()
    url = _rest_url(settings, table)
    params: dict[str, str] = {"select": "*", "order": "created_at.desc", "limit": "50"}
    if settings.switch_tenant_id is not None:
        params["tenant_id"] = f"eq.{settings.switch_tenant_id}"
    try:
        response = await client.get(
            url,
            params=params,
            headers=_supabase_headers(settings),
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Unexpected database response")
        return data
    except (httpx.HTTPError, ValueError) as exc:
        # Do not expose database URLs, API responses, or credentials to the client.
        logging.getLogger("team_memory").debug("Record read failed table=%s", table)
        raise HTTPException(503, "Database request failed") from exc


async def _ensure_tenant_and_room(
    settings: Settings, tenant_id: UUID, switch_room_id: str, *, client
) -> str:
    rooms_url = _rest_url(settings, "rooms")
    tenants_url = _rest_url(settings, "tenants")
    tenant_id_str = str(tenant_id)

    room_lookup = await client.get(
        rooms_url,
        params={
            "switch_room_id": f"eq.{switch_room_id}",
            "select": "id,tenant_id,enabled",
            "limit": "1",
        },
        headers=_supabase_headers(settings),
        timeout=10,
    )
    room_lookup.raise_for_status()
    rooms = room_lookup.json()
    if not isinstance(rooms, list):
        raise ValueError("Unexpected database response")

    if not rooms:
        tenant_resp = await client.post(
            tenants_url,
            params={"on_conflict": "id"},
            headers=_supabase_headers(
                settings, prefer="resolution=ignore-duplicates,return=minimal"
            ),
            json={"id": tenant_id_str, "name": f"Tenant {tenant_id_str}"},
            timeout=10,
        )
        tenant_resp.raise_for_status()

        room_upsert = await client.post(
            rooms_url,
            params={
                "on_conflict": "switch_room_id",
                "select": "id,tenant_id,enabled",
            },
            headers=_supabase_headers(
                settings, prefer="resolution=merge-duplicates,return=representation"
            ),
            json={
                "tenant_id": tenant_id_str,
                "switch_room_id": switch_room_id,
                "name": switch_room_id,
                "platform": "discord",
            },
            timeout=10,
        )
        room_upsert.raise_for_status()
        rooms = room_upsert.json()
        if not isinstance(rooms, list) or not rooms:
            raise ValueError("Unexpected database response")

    room = rooms[0]
    if not isinstance(room, dict) or "id" not in room or "tenant_id" not in room:
        raise ValueError("Unexpected database response")
    if str(room["tenant_id"]) != tenant_id_str or not room.get("enabled", True):
        raise HTTPException(403, "Message tenant or room is not authorized")
    return str(room["id"])


@database_client
async def store_ai_memory(
    settings: Settings,
    *,
    tenant_id: UUID,
    switch_room_id: str,
    switch_message_id: str,
    author_name: str,
    body: str,
    occurred_at: datetime,
    decision: LLMDecision,
    client,
) -> dict[str, int]:
    """Persist the message and extracted commitments, decisions, and memories when decided by the LLM."""
    messages_url = _rest_url(settings, "messages")
    commitments_url = _rest_url(settings, "commitments")
    decisions_url = _rest_url(settings, "decisions")
    memories_url = _rest_url(settings, "memories")
    tenant_id_str = str(tenant_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        room_uuid = await _ensure_tenant_and_room(settings, tenant_id, switch_room_id, client=client)
        safe_author = _redact(author_name, settings) or "Unknown sender"
        safe_body = _redact(body, settings) or "[empty]"

        msg_resp = await client.post(
            messages_url,
            params={
                "on_conflict": "tenant_id,room_id,switch_message_id",
                "select": "id",
            },
            headers=_supabase_headers(
                settings, prefer="resolution=merge-duplicates,return=representation"
            ),
            json={
                "tenant_id": tenant_id_str,
                "room_id": room_uuid,
                "switch_message_id": switch_message_id,
                "platform": "discord",
                "author_name": safe_author,
                "body": safe_body,
                "occurred_at": occurred_at.isoformat(),
                "extraction_status": "completed",
                "extraction_completed_at": now_iso,
            },
            timeout=10,
        )
        msg_resp.raise_for_status()
        msg_rows = msg_resp.json()
        if not isinstance(msg_rows, list) or not msg_rows or "id" not in msg_rows[0]:
            raise ValueError("Unexpected database response")
        message_uuid = str(msg_rows[0]["id"])

        commitment_rows = []
        memory_rows = []

        for idx, item in enumerate(decision.commitments, start=1):
            desc = _redact(item.description, settings)
            if not desc:
                continue
            key = f"commitment-{idx}"
            owner = _redact(item.owner_name, settings)
            due_iso = _valid_iso_datetime(item.due_at)
            commitment_rows.append(
                {
                    "tenant_id": tenant_id_str,
                    "room_id": room_uuid,
                    "message_id": message_uuid,
                    "extraction_key": key,
                    "description": desc,
                    "owner_name": owner,
                    "due_at": due_iso,
                }
            )
            summary_parts = [f"Commitment ({owner}): {desc}" if owner else f"Commitment: {desc}"]
            if due_iso:
                summary_parts.append(f"due {due_iso}")
            memory_rows.append(
                {
                    "tenant_id": tenant_id_str,
                    "room_id": room_uuid,
                    "message_id": message_uuid,
                    "memory_type": "commitment",
                    "extraction_key": key,
                    "content": " - ".join(summary_parts),
                }
            )

        if commitment_rows:
            c_resp = await client.post(
                commitments_url,
                params={"on_conflict": "message_id,extraction_key"},
                headers=_supabase_headers(
                    settings, prefer="resolution=merge-duplicates,return=minimal"
                ),
                json=commitment_rows,
                timeout=10,
            )
            c_resp.raise_for_status()

        decision_rows = []
        for idx, item in enumerate(decision.decisions, start=1):
            summary = _redact(item.summary, settings)
            if not summary:
                continue
            key = f"decision-{idx}"
            topic = _redact(item.topic, settings)
            decision_rows.append(
                {
                    "tenant_id": tenant_id_str,
                    "room_id": room_uuid,
                    "message_id": message_uuid,
                    "extraction_key": key,
                    "topic": topic,
                    "summary": summary,
                }
            )
            memory_rows.append(
                {
                    "tenant_id": tenant_id_str,
                    "room_id": room_uuid,
                    "message_id": message_uuid,
                    "memory_type": "decision",
                    "extraction_key": key,
                    "content": f"Decision ({topic}): {summary}" if topic else f"Decision: {summary}",
                }
            )

        if decision_rows:
            d_resp = await client.post(
                decisions_url,
                params={"on_conflict": "message_id,extraction_key"},
                headers=_supabase_headers(
                    settings, prefer="resolution=merge-duplicates,return=minimal"
                ),
                json=decision_rows,
                timeout=10,
            )
            d_resp.raise_for_status()

        for idx, mem in enumerate(decision.memories, start=1):
            content = _redact(mem.content, settings)
            if not content:
                continue
            memory_rows.append(
                {
                    "tenant_id": tenant_id_str,
                    "room_id": room_uuid,
                    "message_id": message_uuid,
                    "memory_type": mem.memory_type,
                    "extraction_key": f"memory-{idx}",
                    "content": content,
                }
            )

        if not memory_rows:
            memory_rows.append(
                {
                    "tenant_id": tenant_id_str,
                    "room_id": room_uuid,
                    "message_id": message_uuid,
                    "memory_type": "fact",
                    "extraction_key": "memory-message-1",
                    "content": f"{safe_author}: {safe_body}",
                }
            )

        mem_resp = await client.post(
            memories_url,
            params={"on_conflict": "message_id,memory_type,extraction_key"},
            headers=_supabase_headers(
                settings, prefer="resolution=merge-duplicates,return=minimal"
            ),
            json=memory_rows,
            timeout=10,
        )
        mem_resp.raise_for_status()

        return {
            "messages": 1,
            "commitments": len(commitment_rows),
            "decisions": len(decision_rows),
            "memories": len(memory_rows),
        }
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Supabase REST store failed status=%d (verify schema migration and service-role key)",
            exc.response.status_code,
        )
        raise HTTPException(503, "Database request failed") from exc
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Supabase REST store failed reason=%s", type(exc).__name__)
        raise HTTPException(503, "Database request failed") from exc


@database_client
async def query_team_memory(
    settings: Settings, *, search_query: str | None = None, client
) -> dict[str, list[dict]]:
    """Query Supabase for stored memories, commitments, decisions, and messages for the response."""
    if settings.switch_tenant_id is None:
        raise HTTPException(503, "Message scope is not configured")
    tenant_filter = f"eq.{settings.switch_tenant_id}"
    keywords = [
        word.lower()
        for word in (search_query or "").split()
        if len(word.strip()) >= 2
    ]

    specs = (
        ("memories", "memory_type,content,created_at", "content"),
        ("commitments", "description,owner_name,due_at,approval_status,task_status,created_at", "description"),
        ("decisions", "topic,summary,approval_status,decision_status,created_at", "summary"),
        ("messages", "author_name,body,occurred_at", "body"),
    )

    result: dict[str, list[dict]] = {}
    try:
        for table, select_cols, text_field in specs:
            url = _rest_url(settings, table)
            params = {
                "tenant_id": tenant_filter,
                "select": select_cols,
                "order": "created_at.desc" if table != "messages" else "occurred_at.desc",
                "limit": "20",
            }
            if table == "memories":
                params["active"] = "eq.true"
            response = await client.get(
                url,
                params=params,
                headers=_supabase_headers(settings),
                timeout=10,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Unexpected database response")
            if keywords and rows:
                matched = [
                    row
                    for row in rows
                    if any(
                        kw in json.dumps(row, default=str).lower()
                        for kw in keywords
                    )
                ]
                result[table] = (matched or rows)[:10]
            else:
                result[table] = rows[:10]
        return result
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Supabase REST store failed status=%d (verify schema migration and service-role key)",
            exc.response.status_code,
        )
        raise HTTPException(503, "Database request failed") from exc
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Supabase REST store failed reason=%s", type(exc).__name__)
        raise HTTPException(503, "Database request failed") from exc
