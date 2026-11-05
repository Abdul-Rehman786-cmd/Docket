import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.repositories.embedding_repository import claim_embeddings, search_embeddings
from app.schema_upgrade import build_upgrade_sql


def test_hosted_proposal_has_only_nondestructive_changes_and_version_guards():
    sql = build_upgrade_sql()
    assert "DROP TABLE" not in sql and "TRUNCATE" not in sql and "DELETE FROM" not in sql
    assert "CASCADE" not in sql
    assert "ADD COLUMN IF NOT EXISTS content_hash TEXT" in sql
    assert "CREATE OR REPLACE FUNCTION public.store_message_extraction" in sql
    assert "CREATE OR REPLACE FUNCTION public.finish_memory_embedding" in sql
    assert "m.content_hash=p_content_hash AND m.embedding_claim_token=p_claim_token" in sql
    assert sql.startswith("BEGIN;") and sql.endswith("COMMIT;")


def test_vector_search_sends_trusted_scope_and_keeps_source_refs():
    async def run():
        settings=Settings(_env_file=None).model_copy(update={"supabase_url":"https://fixture.invalid","supabase_service_role_key":"fixture-key"})
        calls=[]
        room=str(UUID(int=10))
        source=str(UUID(int=20))

        def handle(request):
            calls.append(request)
            if request.method=="GET":
                return httpx.Response(200,json=[{"id":room,"tenant_id":str(settings.switch_tenant_id),"enabled":True}])
            payload=json.loads(request.content)
            assert payload["p_tenant_id"]==str(settings.switch_tenant_id)
            assert payload["p_room_id"]==room and payload["p_limit"]==5
            return httpx.Response(200,json=[{"id":str(UUID(int=30)),"message_id":source,"content":"Fixture fact","similarity":.8}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            rows=await search_embeddings(settings,[1.0]+[0.0]*383,switch_room_id="test-room",client=client)
            assert len(calls)==2 and rows[0]["message_id"]==source
            assert rows[0]["retrieval_mode"]=="semantic" and rows[0]["room_id"]==room
            with pytest.raises(HTTPException) as error:
                await search_embeddings(settings,[1.0]+[0.0]*383,switch_room_id="unauthorized",client=client)
            assert error.value.status_code==403 and len(calls)==2
    asyncio.run(run())


def test_claim_response_outside_trusted_scope_is_rejected():
    async def run():
        settings=Settings(_env_file=None).model_copy(update={"supabase_url":"https://fixture.invalid","supabase_service_role_key":"fixture-key"})

        def handle(request):
            if request.method=="GET":
                return httpx.Response(200,json=[{"id":str(UUID(int=10)),"tenant_id":str(settings.switch_tenant_id),"enabled":True}])
            return httpx.Response(200,json=[{"id":"malicious","tenant_id":str(UUID(int=999)),"room_id":str(UUID(int=10))}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(HTTPException,match="invalid_embedding_scope"):
                await claim_embeddings(settings,client=client)
    asyncio.run(run())
