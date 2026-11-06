"""Document repository contract and backend-only RPC adapter.

call(scope, operation, data) is the only persistence boundary. Import atomically
commits document/version/chunks/idempotency; finish atomically publishes a fully
embedded latest version. No message jobs or commitment records are accessed.
"""


import httpx
from fastapi import HTTPException

from app.core.http_clients import database_client
from app.repositories.record_repository import _rest_url, _supabase_headers


class SupabaseDocumentRepository:
    @database_client
    async def call(self, scope, operation, data, *, client):
        try:
            settings = self.settings()
            # Reuse the RPC's enabled-tenant/room authorization before the small
            # metadata-only catalog read. No source text, vectors or original bytes.
            catalog = operation == 'catalog'
            response = await client.post(_rest_url(settings, "rpc/document_operation"),
                headers=_supabase_headers(settings), json={
                    "p_tenant_id": scope.tenant_id, "p_room_ids": list(scope.room_ids),
                    "p_operation": 'documents' if catalog else operation,
                    "p_data": {'limit': 1, 'offset': 0} if catalog else data,
                }, timeout=20)
            response.raise_for_status()
            if catalog:
                params = {
                    'select': 'id,title,published_version_id,room:rooms!inner(enabled),published:document_versions!documents_published_version_fk!inner(id,filename,status)',
                    'tenant_id': 'eq.'+scope.tenant_id, 'room_id': 'in.('+','.join(scope.room_ids)+')',
                    'active': 'eq.true', 'room.enabled': 'eq.true', 'published.status': 'eq.completed', 'order': 'id.asc',
                    'limit': min(50, max(1, int(data.get('limit', 5)))),
                    'offset': min(10000, max(0, int(data.get('offset', 0))))}
                for field, column in (('filename', 'published.filename'), ('title', 'title')):
                    if data.get(field):
                        params[column] = 'eq.'+data[field]
                response = await client.get(_rest_url(settings, 'documents'), headers=_supabase_headers(settings), params=params, timeout=20)
                response.raise_for_status()
                return [dict(id=row['id'], title=row['title'], published_version_id=row['published_version_id'],
                             filename=row['published']['filename']) for row in response.json()]
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
