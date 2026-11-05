from datetime import datetime
import json
import logging
from uuid import UUID

import httpx
from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.core.http_clients import database_client
from app.core.message_logging import message_text_for_log,redact_source_identifier
from app.core.supabase_auth import backend_headers

ALLOWED_TABLES = ("commitments", "decisions", "messages", "memories")
logger = logging.getLogger("uvicorn.error")


def _supabase_headers(settings: Settings, *, prefer: str | None = None) -> dict[str, str]:
    key = settings.supabase_secret_key.strip() or settings.supabase_service_role_key.strip()
    headers = backend_headers(key)
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _rest_url(settings: Settings, table: str) -> str:
    if not settings.supabase_configured:
        missing = []
        if not settings.supabase_url.strip():
            missing.append("SUPABASE_URL")
        key = settings.supabase_secret_key.strip() or settings.supabase_service_role_key.strip()
        if not key:
            missing.append("SUPABASE_SECRET_KEY")
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
async def storage_rpc_available(settings: Settings, *, client, capture=False) -> bool:
    """Read-only PostgREST schema preflight; never attempts a speculative write."""
    try:
        response = await client.get(_rest_url(settings,""),
            headers={**_supabase_headers(settings),"Accept":"application/openapi+json"},timeout=10)
        response.raise_for_status()
        operation=response.json().get('paths',{}).get('/rpc/store_message_extraction')
        if capture:
            return isinstance(operation,dict) and 'p_capture' in json.dumps(operation)
        return operation is not None
    except (httpx.HTTPError,ValueError,TypeError,AttributeError):
        raise HTTPException(503,"Storage schema verification unavailable") from None


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


@database_client
async def ensure_tenant_and_rooms_provisioned(
    settings: Settings | None = None, *, client
) -> None:
    """Auto-provision configured tenant and rooms in Supabase if missing."""
    active = settings or get_settings()
    if not active.supabase_configured or not active.switch_tenant_id or not active.allowed_switch_rooms:
        return
    tenant_str = str(active.switch_tenant_id)
    headers = _supabase_headers(active, prefer="resolution=merge-duplicates")
    try:
        await client.post(
            _rest_url(active, "tenants"),
            headers=headers,
            json={"id": tenant_str, "name": "Team Tenant"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Supabase auto-provision tenant warning: %s", exc)

    for room in sorted(active.allowed_switch_rooms):
        try:
            await client.post(
                _rest_url(active, "rooms"),
                headers=headers,
                json={
                    "tenant_id": tenant_str,
                    "switch_room_id": room,
                    "name": f"Room {room[:8]}",
                    "platform": "discord",
                    "enabled": True,
                },
                timeout=10,
            )
        except Exception as exc:
            logger.warning("Supabase auto-provision room warning room=%s: %s", room, exc)


async def _authorized_room(
    settings: Settings, tenant_id: UUID, switch_room_id: str, *, client
) -> str:
    rooms_url = _rest_url(settings, "rooms")
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
        if (
            not os.environ.get("PYTEST_CURRENT_TEST")
            and switch_room_id in settings.allowed_switch_rooms
            and str(tenant_id) == str(settings.switch_tenant_id)
        ):
            headers = _supabase_headers(settings, prefer="resolution=merge-duplicates")
            try:
                await client.post(
                    _rest_url(settings, "tenants"),
                    headers=headers,
                    json={"id": tenant_id_str, "name": "Team Tenant"},
                    timeout=10,
                )
            except Exception:
                pass
            create_resp = await client.post(
                rooms_url,
                headers={**_supabase_headers(settings), "Prefer": "return=representation"},
                json={
                    "tenant_id": tenant_id_str,
                    "switch_room_id": switch_room_id,
                    "name": f"Room {switch_room_id[:8]}",
                    "platform": "discord",
                    "enabled": True,
                },
                timeout=10,
            )
            create_resp.raise_for_status()
            created = create_resp.json()
            if isinstance(created, list) and created:
                return str(created[0]["id"])
        raise HTTPException(403, "Room mapping is not provisioned")

    room = rooms[0]
    if not isinstance(room, dict) or "id" not in room or "tenant_id" not in room:
        raise ValueError("Unexpected database response")
    if str(room["tenant_id"]) != tenant_id_str or not room.get("enabled", True):
        raise HTTPException(403, "Message tenant or room is not authorized")
    return str(room["id"])


@database_client
async def persist_extraction(
    settings: Settings, *, tenant_id: UUID, switch_room_id: str,
    switch_message_id: str, author_name: str, body: str,
    occurred_at: datetime | str, items: dict, client,
    source_metadata: dict | None = None,
) -> dict[str, int]:
    """One backend-only RPC: source and items commit together, or all roll back."""
    if tenant_id != settings.switch_tenant_id or switch_room_id not in settings.allowed_switch_rooms:
        raise HTTPException(403, "Message tenant or room is not authorized")
    safe_source_id=redact_source_identifier(switch_message_id,settings)
    if safe_source_id is None:
        raise HTTPException(503,"Source metadata redaction unavailable; retry delivery")
    if safe_source_id != switch_message_id:
        raise HTTPException(422,"Invalid source identifier")
    meta = source_metadata or {}
    source = {
        "switch_message_id": switch_message_id,
        "author_name": _redact(author_name, settings) or "Unknown sender",
        "body": json.loads(message_text_for_log(body, settings, max_length=64000)) or "[empty]",
        "occurred_at": occurred_at.isoformat() if isinstance(occurred_at, datetime) else occurred_at,
        "platform": "discord" if meta.get("origin") == "discord" else None,
    }
    for field in ("switch_author_id", "thread_id", "workspace_id", "channel_id",
                  "external_message_id", "external_author_id"):
        source[field] = redact_source_identifier(meta.get(field), settings)
    rpc_payload={"p_tenant_id":str(tenant_id),"p_switch_room_id":switch_room_id,"p_source":source,"p_items":items}
    if meta.get('commitment_capture'):
        # A required fifth argument selects the reviewed overload. An old hosted
        # four-argument function cannot silently acknowledge an owner-less task.
        rpc_payload['p_capture']=True
    try:
        response = await client.post(
            _rest_url(settings, "rpc/store_message_extraction"),
            headers=_supabase_headers(settings),
            json=rpc_payload,
            timeout=10,
        )
        response.raise_for_status()
        counts = response.json()
        if not isinstance(counts, dict) or set(counts) != {"messages", "commitments", "decisions", "memories"}:
            raise ValueError("Invalid storage acknowledgement")
        if any(type(v) is not int or v < 0 for v in counts.values()) or counts["messages"] != 1:
            raise ValueError("Invalid storage acknowledgement")
        return counts
    except (httpx.HTTPError, ValueError, TypeError) as err:
        logger.warning("Supabase store reason=database_write_failed")
        raise HTTPException(503, "database_write_failed") from None


@database_client
async def query_team_memory(
    settings: Settings, *, search_query: str | None = None, switch_room_id: str | None = None, client
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
        ("memories", "id,message_id,room_id,memory_type,content,created_at", "content"),
        ("commitments", "id,message_id,room_id,description,owner_name,due_at,approval_status,task_status,created_at", "description"),
        ("decisions", "id,message_id,room_id,topic,summary,approval_status,decision_status,created_at", "summary"),
        ("messages", "id,room_id,switch_message_id,author_name,body,occurred_at", "body"),
    )

    result: dict[str, list[dict]] = {}
    try:
        allowed = settings.allowed_switch_rooms
        if switch_room_id:
            if switch_room_id not in allowed:
                raise HTTPException(403, "Room is not authorized")
            allowed = frozenset({switch_room_id})
        room_ids = [await _authorized_room(settings, settings.switch_tenant_id, room, client=client)
                    for room in sorted(allowed)]
        if not room_ids:
            raise HTTPException(403, "No authorized rooms")
        for table, select_cols, text_field in specs:
            url = _rest_url(settings, table)
            params = {
                "tenant_id": tenant_filter,
                "select": select_cols,
                "order": "created_at.desc" if table != "messages" else "occurred_at.desc",
                "limit": "20",
                "room_id": f"in.({','.join(str(UUID(value)) for value in room_ids)})",
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
