"""Read-only document scope resolution. Never creates or repairs room mappings."""

from uuid import UUID

import httpx
from fastapi import HTTPException

from app.core.http_clients import database_client
from app.models.documents import DocumentScope
from app.repositories.record_repository import _rest_url, _supabase_headers


@database_client
async def resolve_document_scope(settings, switch_room_id=None, *, client):
    if settings.switch_tenant_id is None or not settings.allowed_switch_rooms:
        raise HTTPException(503,"document_scope_not_configured")
    selected=settings.allowed_switch_rooms
    if switch_room_id is not None:
        if switch_room_id not in selected:
            raise HTTPException(403,"document_scope_denied")
        selected={switch_room_id}
    room_ids=[]
    try:
        for external_id in sorted(selected):
            response=await client.get(_rest_url(settings,"rooms"),headers=_supabase_headers(settings),params={
                "switch_room_id":f"eq.{external_id}","select":"id,tenant_id,enabled","limit":"1"},timeout=10)
            response.raise_for_status()
            rows=response.json()
            if not isinstance(rows,list):
                raise ValueError()
            if not rows or str(rows[0].get("tenant_id"))!=str(settings.switch_tenant_id) or rows[0].get("enabled") is not True:
                raise HTTPException(403,"document_scope_denied")
            room_ids.append(str(UUID(rows[0]["id"])))
    except (httpx.HTTPError,ValueError,TypeError,KeyError,AttributeError):
        raise HTTPException(503,"document_scope_unavailable") from None
    return DocumentScope(str(settings.switch_tenant_id),tuple(room_ids))
