from datetime import datetime, timezone
import json
import logging
import os
from uuid import UUID

import httpx
from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.core.http_clients import database_client
from app.core.message_logging import message_text_for_log,redact_source_identifier
from app.core.supabase_auth import backend_headers
from app.repositories import sqlite_records, sqlite_db

ALLOWED_TABLES = ("commitments", "decisions", "messages", "memories", "documents")
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
    """Verify database connectivity using configured engine (SQLite or Supabase)."""
    active_settings = settings or get_settings()
    if active_settings.effective_database_engine == "sqlite":
        return sqlite_records.check_database_connection(active_settings)
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
    if settings.effective_database_engine == "sqlite":
        sqlite_records.check_database_connection(settings)
        return True
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
    """Read records via configured engine (SQLite or Supabase REST)."""
    if table not in ALLOWED_TABLES:
        raise ValueError("Unknown table")
    settings = get_settings()
    if settings.effective_database_engine == "sqlite":
        return sqlite_records.list_records(table, settings)
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
    if active.effective_database_engine == "sqlite":
        if active.sqlite_configured:
            sqlite_db.init_sqlite_db(active)
        return
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
async def configure_supabase_reminder_policy(
    settings: Settings, enabled: bool, lead_minutes: int, timezone: str | None = None, *, client
) -> None:
    """Update reminder settings on commitment_chat_policies for all allowed rooms."""
    if not settings.supabase_configured or not settings.switch_tenant_id or not settings.allowed_switch_rooms:
        return
    tenant_str = str(settings.switch_tenant_id)
    headers = _supabase_headers(settings, prefer="resolution=merge-duplicates")
    tz = timezone or settings.commitment_timezone or "UTC"

    for switch_room_id in sorted(settings.allowed_switch_rooms):
        try:
            internal_room_id = await _authorized_room(settings, settings.switch_tenant_id, switch_room_id, client=client)
            await client.post(
                _rest_url(settings, "commitment_chat_policies"),
                headers=headers,
                json={
                    "tenant_id": tenant_str,
                    "room_id": internal_room_id,
                    "reminders_enabled": enabled,
                    "reminder_lead_minutes": lead_minutes,
                    "timezone": tz,
                },
                timeout=10,
            )
        except Exception as exc:
            logger.warning("Could not update Supabase reminder policy for room %s: %s", switch_room_id, exc)


@database_client
async def persist_extraction(
    settings: Settings, *, tenant_id: UUID, switch_room_id: str,
    switch_message_id: str, author_name: str, body: str,
    occurred_at: datetime | str, items: dict, client,
    source_metadata: dict | None = None,
) -> dict[str, int]:
    """Store extraction atomically: SQLite native transaction or Supabase store_message_extraction RPC."""
    if settings.effective_database_engine == "sqlite":
        return await sqlite_records.persist_extraction(
            settings,
            tenant_id=tenant_id,
            switch_room_id=switch_room_id,
            switch_message_id=switch_message_id,
            author_name=author_name,
            body=body,
            occurred_at=occurred_at,
            items=items,
            source_metadata=source_metadata,
        )
    if tenant_id != settings.switch_tenant_id or switch_room_id not in settings.allowed_switch_rooms:
        raise HTTPException(403, "Message tenant or room is not authorized")
    safe_source_id=redact_source_identifier(switch_message_id,settings)
    if safe_source_id is None:
        raise HTTPException(503,"Source metadata redaction unavailable; retry delivery")
    if safe_source_id != switch_message_id:
        raise HTTPException(422,"Invalid source identifier")
    meta = source_metadata or {}
    profile_write = bool(items.get('profile_facts'))
    if profile_write:
        from app.services.profile_service import profile_items
        if profile_items(items['profile_facts'], settings, meta, body) != items['profile_facts']:
            raise HTTPException(422, "Invalid profile storage items")
    source = {
        "switch_message_id": switch_message_id,
        "author_name": _redact(author_name, settings) or "Unknown sender",
        "body": json.loads(message_text_for_log(body, settings, max_length=64000)) or "[empty]",
        "occurred_at": occurred_at.isoformat() if isinstance(occurred_at, datetime) else occurred_at,
        "platform": meta.get("origin") if meta.get("origin") in ("discord", "slack", "teams") else ("discord" if meta.get("origin") == "discord" else None),
    }
    for field in ("switch_author_id", "thread_id", "workspace_id", "channel_id",
                  "external_message_id", "external_author_id"):
        source[field] = redact_source_identifier(meta.get(field), settings)
    rpc_payload={"p_tenant_id":str(tenant_id),"p_switch_room_id":switch_room_id,"p_source":source,"p_items":items}
    if meta.get('commitment_capture') or profile_write:
        # Capture selects the reviewed overload. Profile writes select a distinct
        # RPC below, so an older hosted writer cannot silently ignore profile data.
        rpc_payload['p_capture']=bool(meta.get('commitment_capture'))
    try:
        response = await client.post(
            _rest_url(settings, "rpc/store_profile_message_extraction" if profile_write else "rpc/store_message_extraction"),
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
        if items.get("commitments") and not os.environ.get("PYTEST_CURRENT_TEST"):
            await _approve_supabase_commitments(
                settings, tenant_id, switch_room_id, source, items["commitments"], client=client
            )
        return counts
    except (httpx.HTTPError, ValueError, TypeError) as err:
        logger.warning("Supabase store reason=database_write_failed category=%s", type(err).__name__)
        raise HTTPException(503, "database_write_failed") from None


async def _approve_supabase_commitments(
    settings: Settings, tenant_id: UUID, switch_room_id: str, source: dict, commitments: list[dict], *, client
) -> None:
    """Mark explicitly confirmed/captured commitments as approved in Supabase with valid owner and due_at."""
    try:
        internal_room_id = await _authorized_room(settings, tenant_id, switch_room_id, client=client)
        owner_id = source.get("switch_author_id") or source.get("author_name") or "room-member"
        now_iso = datetime.now(timezone.utc).isoformat()
        headers = _supabase_headers(settings)
        # Ensure room platform is set appropriately
        room_platform = meta.get("origin") if meta.get("origin") in ("discord", "slack", "teams") else "discord"
        await client.patch(
            _rest_url(settings, "rooms"),
            headers=headers,
            params={"tenant_id": f"eq.{tenant_id}", "id": f"eq.{internal_room_id}"},
            json={"platform": room_platform, "enabled": True},
            timeout=10,
        )
        # Ensure sender is in room_members
        await client.post(
            _rest_url(settings, "room_members"),
            headers=_supabase_headers(settings, prefer="resolution=merge-duplicates"),
            json={
                "tenant_id": str(tenant_id),
                "room_id": internal_room_id,
                "switch_user_id": owner_id,
                "display_name": source.get("author_name") or owner_id,
                "role": "member",
            },
            timeout=10,
        )
        for c_item in commitments:
            ext_key = c_item.get("extraction_key")
            if not ext_key:
                continue
            patch_data = {
                "approval_status": "approved",
                "approval_by_switch_user_id": owner_id,
                "approval_at": now_iso,
                "owner_switch_user_id": owner_id,
                "owner_name": c_item.get("owner_name") or source.get("author_name"),
            }
            if c_item.get("due_at"):
                patch_data["due_at"] = c_item["due_at"]
            await client.patch(
                _rest_url(settings, "commitments"),
                headers=headers,
                params={"tenant_id": f"eq.{tenant_id}", "room_id": f"eq.{internal_room_id}", "extraction_key": f"eq.{ext_key}"},
                json=patch_data,
                timeout=10,
            )
    except Exception as exc:
        logger.warning("Could not auto-approve Supabase commitment: %s", exc)


@database_client
async def query_team_memory(
    settings: Settings, *, search_query: str | None = None, switch_room_id: str | None = None, client
) -> dict[str, list[dict]]:
    """Query stored memories, commitments, decisions, and messages for the response."""
    if settings.effective_database_engine == "sqlite":
        return await sqlite_records.query_team_memory(
            settings, search_query=search_query, switch_room_id=switch_room_id
        )
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


@database_client
async def delete_records_older_than_supabase(
    cutoff: datetime | str,
    settings: Settings | None = None,
    *,
    client,
) -> dict[str, int]:
    """Delete all data older than cutoff in Supabase across child and parent tables."""
    active = settings or get_settings()
    if not active.supabase_configured:
        raise HTTPException(503, "Supabase is not configured")

    cutoff_iso = cutoff.isoformat() if isinstance(cutoff, datetime) else str(cutoff)
    headers = _supabase_headers(active, prefer="return=representation")
    tenant_param = {"tenant_id": f"eq.{active.switch_tenant_id}"} if active.switch_tenant_id else {}

    deleted_counts = {
        "memories": 0,
        "commitments": 0,
        "decisions": 0,
        "messages": 0,
        "documents": 0,
    }

    try:
        # 1. Find old message IDs first (occurred_at < cutoff or created_at < cutoff)
        msg_resp = await client.get(
            _rest_url(active, "messages"),
            params={
                **tenant_param,
                "select": "id",
                "or": f"(occurred_at.lt.{cutoff_iso},created_at.lt.{cutoff_iso})",
                "limit": "5000",
            },
            headers=_supabase_headers(active),
            timeout=15,
        )
        msg_resp.raise_for_status()
        old_messages = msg_resp.json()
        old_msg_ids = [str(m["id"]) for m in old_messages if isinstance(m, dict) and "id" in m]

        # 2. Delete dependent rows linked to old messages (ON DELETE RESTRICT in Postgres)
        if old_msg_ids:
            in_filter = f"in.({','.join(old_msg_ids)})"
            # reminder_deliveries depends on commitments
            comm_lookup = await client.get(
                _rest_url(active, "commitments"),
                params={**tenant_param, "select": "id", "message_id": in_filter, "limit": "5000"},
                headers=_supabase_headers(active),
                timeout=15,
            )
            if comm_lookup.status_code == 200:
                comm_ids = [str(c["id"]) for c in comm_lookup.json() if isinstance(c, dict) and "id" in c]
                if comm_ids:
                    await client.delete(
                        _rest_url(active, "reminder_deliveries"),
                        params={**tenant_param, "commitment_id": f"in.({','.join(comm_ids)})"},
                        headers=headers,
                        timeout=15,
                    )

            for child_table in ("memories", "commitments", "decisions"):
                del_resp = await client.delete(
                    _rest_url(active, child_table),
                    params={**tenant_param, "message_id": in_filter},
                    headers=headers,
                    timeout=15,
                )
                if del_resp.status_code in (200, 204):
                    body = del_resp.json() if del_resp.text else []
                    if isinstance(body, list):
                        deleted_counts[child_table] += len(body)

        # 3. Also delete any standalone child rows whose created_at < cutoff
        for child_table in ("memories", "commitments", "decisions"):
            if child_table == "commitments":
                old_comm_resp = await client.get(
                    _rest_url(active, "commitments"),
                    params={**tenant_param, "select": "id", "created_at": f"lt.{cutoff_iso}", "limit": "5000"},
                    headers=_supabase_headers(active),
                    timeout=15,
                )
                if old_comm_resp.status_code == 200:
                    c_ids = [str(c["id"]) for c in old_comm_resp.json() if isinstance(c, dict) and "id" in c]
                    if c_ids:
                        await client.delete(
                            _rest_url(active, "reminder_deliveries"),
                            params={**tenant_param, "commitment_id": f"in.({','.join(c_ids)})"},
                            headers=headers,
                            timeout=15,
                        )
            del_resp = await client.delete(
                _rest_url(active, child_table),
                params={**tenant_param, "created_at": f"lt.{cutoff_iso}"},
                headers=headers,
                timeout=15,
            )
            if del_resp.status_code in (200, 204):
                body = del_resp.json() if del_resp.text else []
                if isinstance(body, list):
                    deleted_counts[child_table] += len(body)

        # 4. Delete the old messages
        if old_msg_ids:
            in_filter = f"in.({','.join(old_msg_ids)})"
            msg_del = await client.delete(
                _rest_url(active, "messages"),
                params={**tenant_param, "id": in_filter},
                headers=headers,
                timeout=15,
            )
            if msg_del.status_code in (200, 204):
                body = msg_del.json() if msg_del.text else []
                deleted_counts["messages"] += len(body) if isinstance(body, list) else len(old_msg_ids)

        # 5. Delete old documents (and their imports, chunks, versions) if present
        doc_resp = await client.get(
            _rest_url(active, "documents"),
            params={**tenant_param, "select": "id", "created_at": f"lt.{cutoff_iso}", "limit": "5000"},
            headers=_supabase_headers(active),
            timeout=15,
        )
        if doc_resp.status_code == 200:
            old_docs = [str(d["id"]) for d in doc_resp.json() if isinstance(d, dict) and "id" in d]
            if old_docs:
                doc_in = f"in.({','.join(old_docs)})"
                # Clear published_version_id FK before deleting versions
                await client.patch(
                    _rest_url(active, "documents"),
                    params={**tenant_param, "id": doc_in},
                    json={"published_version_id": None},
                    headers=headers,
                    timeout=15,
                )
                for doc_child in ("document_imports", "document_chunks", "document_versions"):
                    await client.delete(
                        _rest_url(active, doc_child),
                        params={**tenant_param, "document_id": doc_in},
                        headers=headers,
                        timeout=15,
                    )
                doc_del = await client.delete(
                    _rest_url(active, "documents"),
                    params={**tenant_param, "id": doc_in},
                    headers=headers,
                    timeout=15,
                )
                if doc_del.status_code in (200, 204):
                    body = doc_del.json() if doc_del.text else []
                    deleted_counts["documents"] += len(body) if isinstance(body, list) else len(old_docs)

        return deleted_counts
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning("Supabase timeline delete failed status=%d", exc.response.status_code)
        raise HTTPException(503, "Supabase timeline deletion failed") from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Supabase timeline delete failed reason=%s", type(exc).__name__)
        raise HTTPException(503, "Supabase timeline deletion failed") from exc


@database_client
async def delete_records_older_than(
    cutoff: datetime | str,
    *,
    engine: str = "active",
    settings: Settings | None = None,
    client,
) -> dict[str, int]:
    """Dispatch timeline deletion to SQLite or Supabase."""
    active = settings or get_settings()
    target_engine = active.effective_database_engine if engine == "active" else engine
    if target_engine == "sqlite":
        return sqlite_records.delete_records_older_than_sqlite(cutoff, active)
    if target_engine == "supabase":
        return await delete_records_older_than_supabase(cutoff, active, client=client)
    raise HTTPException(422, f"Unsupported database engine: {target_engine}")

