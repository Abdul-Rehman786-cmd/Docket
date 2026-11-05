"""Scoped backend-only embedding job and vector-search RPC access."""

import httpx
from uuid import UUID
from fastapi import HTTPException

from app.core.http_clients import database_client
from app.repositories.record_repository import _authorized_room, _rest_url, _supabase_headers


async def authorized_room_ids(settings, client, switch_room_id=None):
    if settings.switch_tenant_id is None or not settings.allowed_switch_rooms:
        raise HTTPException(503, "Message scope is not configured")
    rooms = settings.allowed_switch_rooms
    if switch_room_id:
        if switch_room_id not in rooms:
            raise HTTPException(403, "Room is not authorized")
        rooms = frozenset({switch_room_id})
    return [await _authorized_room(settings, settings.switch_tenant_id, room, client=client)
            for room in sorted(rooms)]


async def _rpc(settings, name, payload, client):
    try:
        response = await client.post(_rest_url(settings, "rpc/" + name),
                                     headers=_supabase_headers(settings), json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "embedding_database_unavailable") from None


@database_client
async def claim_embeddings(settings, *, client):
    rooms = await authorized_room_ids(settings, client)
    rows = await _rpc(settings, "claim_memory_embeddings", {
        "p_tenant_id": str(settings.switch_tenant_id), "p_room_ids": rooms, "p_limit": 8,
    }, client)
    if not isinstance(rows, list) or len(rows)>8:
        raise HTTPException(503, "invalid_embedding_acknowledgement")
    for row in rows:
        if row.get("tenant_id") != str(settings.switch_tenant_id) or row.get("room_id") not in rooms:
            raise HTTPException(503, "invalid_embedding_scope")
    return rows


@database_client
async def finish_embedding(settings, job, *, vector=None, error=None, client):
    rooms = await authorized_room_ids(settings, client)
    if job["tenant_id"] != str(settings.switch_tenant_id) or job["room_id"] not in rooms:
        raise HTTPException(403, "Room is not authorized")
    result = await _rpc(settings, "finish_memory_embedding", {
        "p_memory_id": job["id"], "p_tenant_id": job["tenant_id"], "p_room_id": job["room_id"],
        "p_claim_token": job["claim_token"], "p_content_hash": job["content_hash"],
        "p_embedding": vector, "p_error_code": error,
    }, client)
    if type(result) is not bool:
        raise HTTPException(503, "invalid_embedding_acknowledgement")
    return result


@database_client
async def search_embeddings(settings, vector, *, switch_room_id=None, client):
    rooms = await authorized_room_ids(settings, client, switch_room_id)
    matches = []
    for room in rooms:
        rows = await _rpc(settings, "search_memories", {
            "p_tenant_id": str(settings.switch_tenant_id), "p_room_id": room,
            "p_embedding": vector, "p_limit": 5,
        }, client)
        if not isinstance(rows, list):
            raise HTTPException(503, "invalid_embedding_acknowledgement")
        matches.extend({**row, "room_id": room, "retrieval_mode": "semantic"} for row in rows)
    return sorted(matches, key=lambda r: r["similarity"], reverse=True)[:5]


@database_client
async def source_context(settings, matches, *, client):
    rooms = await authorized_room_ids(settings, client)
    if any(row.get("room_id") not in rooms for row in matches):
        raise HTTPException(403, "Room is not authorized")
    message_ids = sorted({str(UUID(row["message_id"])) for row in matches})
    specs = {
        "commitments": "id,message_id,room_id,description,owner_name,due_at,approval_status,task_status",
        "decisions": "id,message_id,room_id,topic,summary,approval_status,decision_status",
        "messages": "id,room_id,switch_message_id,author_name,occurred_at",
    }
    result = {}
    try:
        for table, fields in specs.items():
            response = await client.get(_rest_url(settings, table), headers=_supabase_headers(settings), params={
                "tenant_id":f"eq.{settings.switch_tenant_id}", "room_id":f"in.({','.join(rooms)})",
                "id" if table=="messages" else "message_id":f"in.({','.join(message_ids)})",
                "select":fields, "limit":"50",
            },timeout=10)
            response.raise_for_status()
            result[table] = response.json()
            if not isinstance(result[table],list):
                raise ValueError("Invalid context")
        return result
    except (httpx.HTTPError,ValueError):
        raise HTTPException(503,"source_context_unavailable") from None
