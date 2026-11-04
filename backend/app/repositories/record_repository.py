from datetime import datetime

import httpx
from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.models.messages import IncomingMessage

ALLOWED_TABLES = ("commitments", "decisions", "messages")


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
        raise HTTPException(503, "Database is not configured")
    return f"{settings.supabase_url.rstrip('/')}/rest/v1/{table}"


def check_database_connection(settings: Settings | None = None) -> dict[str, str]:
    """Verify Supabase REST connectivity using configured service-role credentials."""
    active_settings = settings or get_settings()
    url = _rest_url(active_settings, "tenants")
    try:
        response = httpx.get(
            url,
            params={"select": "id", "limit": "1"},
            headers=_supabase_headers(active_settings),
            timeout=10,
        )
        response.raise_for_status()
        if not isinstance(response.json(), list):
            raise ValueError("Unexpected database response")
        return {"status": "connected"}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Database request failed") from exc


def list_records(table: str) -> list[dict]:
    """Read records via Supabase REST. Only invoked by protected backend endpoints."""
    if table not in ALLOWED_TABLES:
        raise ValueError("Unknown table")
    settings = get_settings()
    url = _rest_url(settings, table)
    params: dict[str, str] = {"select": "*", "order": "created_at.desc", "limit": "50"}
    if settings.switch_tenant_id is not None:
        params["tenant_id"] = f"eq.{settings.switch_tenant_id}"
    try:
        response = httpx.get(
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
        raise HTTPException(503, "Database request failed") from exc


def persist_message(
    message: IncomingMessage,
    settings: Settings,
    *,
    redacted_sender_name: str,
    redacted_text: str,
    occurred_at: datetime,
) -> bool:
    """Ensure tenant and room rows exist, then idempotently store the redacted message."""
    rooms_url = _rest_url(settings, "rooms")
    tenants_url = _rest_url(settings, "tenants")
    messages_url = _rest_url(settings, "messages")
    tenant_id_str = str(message.tenant_id)
    try:
        room_lookup = httpx.get(
            rooms_url,
            params={
                "switch_room_id": f"eq.{message.room_id}",
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
            tenant_resp = httpx.post(
                tenants_url,
                params={"on_conflict": "id"},
                headers=_supabase_headers(
                    settings, prefer="resolution=ignore-duplicates,return=minimal"
                ),
                json={"id": tenant_id_str, "name": f"Tenant {tenant_id_str}"},
                timeout=10,
            )
            tenant_resp.raise_for_status()

            room_upsert = httpx.post(
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
                    "switch_room_id": message.room_id,
                    "name": message.room_id,
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

        room_uuid = str(room["id"])
        msg_resp = httpx.post(
            messages_url,
            params={
                "on_conflict": "tenant_id,room_id,switch_message_id",
                "select": "id",
            },
            headers=_supabase_headers(
                settings, prefer="resolution=ignore-duplicates,return=representation"
            ),
            json={
                "tenant_id": tenant_id_str,
                "room_id": room_uuid,
                "switch_message_id": message.message_id,
                "switch_author_id": message.sender,
                "platform": "discord",
                "thread_id": message.thread_id,
                "author_name": redacted_sender_name,
                "body": redacted_text,
                "occurred_at": occurred_at.isoformat(),
            },
            timeout=10,
        )
        msg_resp.raise_for_status()
        inserted = msg_resp.json()
        if not isinstance(inserted, list):
            raise ValueError("Unexpected database response")
        return len(inserted) > 0
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(503, "Database request failed") from exc
