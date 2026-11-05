"""Document repository contract and backend-only RPC adapter.

call(scope, operation, data) is the only persistence boundary. Import atomically
commits document/version/chunks/idempotency; finish atomically publishes a fully
embedded latest version. No message jobs or commitment records are accessed.
"""

from typing import Protocol

import httpx
from fastapi import HTTPException

from app.core.http_clients import database_client
from app.models.documents import DocumentScope
from app.repositories.record_repository import _rest_url, _supabase_headers


class DocumentRepository(Protocol):
    async def call(self, scope: DocumentScope, operation: str, data: dict): ...


class SupabaseDocumentRepository:
    @database_client
    async def call(self, scope, operation, data, *, client):
        try:
            settings = self.settings()
            response = await client.post(_rest_url(settings, "rpc/document_operation"),
                headers=_supabase_headers(settings), json={
                    "p_tenant_id": scope.tenant_id, "p_room_ids": list(scope.room_ids),
                    "p_operation": operation, "p_data": data,
                }, timeout=20)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            try:
                body = error.response.json()
                code = body.get("code") if isinstance(body, dict) else None
            except ValueError:
                code = None
            status = {"P0002": 404, "40001": 409, "42501": 403, "22023": 422}.get(code, 503)
            reason = {404: "document_not_found",409: "document_version_conflict",
                      403: "document_scope_denied",422: "document_invalid_import"}.get(status,"document_database_unavailable")
            raise HTTPException(status, reason) from None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "document_database_unavailable") from None

    @staticmethod
    def settings():
        from app.core.config import get_settings
        return get_settings()
