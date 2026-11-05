"""Opt-in real Supabase + local-model fixture. Requires approved hosted schema.

No Switch/Codex/Discord requests. Creates a unique disposable scope and cleans
only that scope. Never run before deployment approval or with absent credentials.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.core.embedding_model import RECIPE, validate_vector
from app.models.ai import LLMDecision
from app.repositories.record_repository import _rest_url, _supabase_headers
from app.services.embedding_service import EmbeddingWorker
from app.services.extraction_service import store_ai_memory
from app.services.retrieval_service import query_team_memory


async def verify():
    original = Settings()
    if not original.supabase_configured:
        raise RuntimeError("database_not_configured")
    tenant, room = uuid4(), uuid4()
    marker = "TM-real-embedding-" + str(uuid4())
    settings = original.model_copy(update={"switch_tenant_id": tenant, "switch_room_ids": marker,
                                           "local_embeddings_enabled": True})
    created = False
    evidence = {}
    print(json.dumps({"fixture":marker,"tenant_id":str(tenant),"room_id":str(room),"state":"starting"}))
    async with httpx.AsyncClient(trust_env=False,timeout=20) as client:
        async def request(method, table, **kwargs):
            response = await client.request(method,_rest_url(settings,table),headers=_supabase_headers(settings),**kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

        try:
            await request("POST","tenants",json={"id":str(tenant),"name":marker})
            created = True
            await request("POST","rooms",json={"id":str(room),"tenant_id":str(tenant),"switch_room_id":marker,"name":marker})
            counts = await store_ai_memory(settings,tenant_id=tenant,switch_room_id=marker,
                switch_message_id=marker,author_name="Disposable fixture",body="The backend API uses FastAPI.",
                occurred_at=datetime.now(timezone.utc),decision=LLMDecision(action="store_only",
                memories=[{"content":"The backend API uses FastAPI."}]),client=client)
            # Same source replay must not create another source or memory.
            replay = await store_ai_memory(settings,tenant_id=tenant,switch_room_id=marker,
                switch_message_id=marker,author_name="Disposable fixture",body="The backend API uses FastAPI.",
                occurred_at=datetime.now(timezone.utc),decision=LLMDecision(action="store_only",
                memories=[{"content":"The backend API uses FastAPI."}]),client=client)
            assert replay == counts == {"messages":1,"commitments":0,"decisions":0,"memories":1}
            assert await EmbeddingWorker().process_once(settings) == 1
            rows = await request("GET","memories",params={"tenant_id":f"eq.{tenant}","room_id":f"eq.{room}",
                "select":"id,message_id,embedding,embedding_status,embedding_recipe,content_hash,embedding_content_hash"})
            assert len(rows)==1
            memory=rows[0]
            assert memory["embedding_status"]=="completed" and memory["embedding_recipe"]==RECIPE
            assert memory["content_hash"]==memory["embedding_content_hash"]
            vector = json.loads(memory["embedding"]) if isinstance(memory["embedding"],str) else memory["embedding"]
            validate_vector(vector)
            result = await query_team_memory(settings,search_query="Which framework powers our backend API?",switch_room_id=marker)
            assert result["retrieval"][0]["mode"]=="semantic"
            assert result["memories"][0]["message_id"]==memory["message_id"]
            evidence = {"fixture":marker,"stored_counts":counts,"real_vector_dimensions":len(vector),
                "stored_norm":sum(x*x for x in vector)**.5,"retrieval_mode":"semantic",
                "paraphrase_similarity":result["memories"][0]["similarity"],"source_reference_verified":True}
        finally:
            if created:
                for table in ("memories","commitments","decisions","messages"):
                    await request("DELETE",table,params={"tenant_id":f"eq.{tenant}"})
                await request("DELETE","rooms",params={"id":f"eq.{room}","tenant_id":f"eq.{tenant}"})
                await request("DELETE","tenants",params={"id":f"eq.{tenant}"})
                assert await request("GET","tenants",params={"id":f"eq.{tenant}","select":"id"}) == []
                evidence["fixture_cleanup_verified"] = True
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-approved-fixture",action="store_true",required=True)
    parser.parse_args()
    try:
        print(json.dumps(asyncio.run(verify())))
    except Exception:
        print('{"error":"live_fixture_failed_or_cleanup_incomplete"}')
        raise SystemExit(1) from None
