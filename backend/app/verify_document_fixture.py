"""Opt-in post-approval hosted document fixture, actual local model and FastAPI routes.

No schema changes or messaging. Requires the reviewed document migration already
installed. Generated tenant/room isolation and exact-scope cleanup are mandatory.
"""

import argparse
import asyncio
import json
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
import httpx

from app.controllers.documents import router, document_repository
from app.core.config import Settings
from app.core.embedding_model import validate_vector, get_embedder, RECIPE
from app.models.documents import DocumentScope
from app.repositories.document_repository import SupabaseDocumentRepository
from app.repositories.record_repository import _rest_url, _supabase_headers
from app.services.document_worker import DocumentWorker
from app.services.document_chunker import digest


async def verify():
    original = Settings()
    if not original.supabase_configured or not original.admin_api_token:
        raise RuntimeError("fixture_configuration_missing")
    tenant, room = str(uuid4()), str(uuid4())
    marker = "TM-document-fixture-"+str(uuid4())
    settings = original.model_copy(update={"switch_tenant_id":tenant,"switch_room_ids":marker,
        "document_ingestion_enabled":True,"local_embeddings_enabled":True})
    repo = SupabaseDocumentRepository()
    repo.settings = lambda:settings
    scope = DocumentScope(tenant,(room,))
    created = False
    worker = DocumentWorker(repo,settings_provider=lambda:settings)
    evidence = dict(fixture=marker,tenant_id=tenant,room_id=room)
    print(json.dumps({**evidence,"state":"preflight"}))
    async with httpx.AsyncClient(trust_env=False,timeout=20) as database:
        async def request(method,table,**kwargs):
            response=await database.request(method,_rest_url(settings,table),headers=_supabase_headers(settings),**kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

        filters={"tenant_id":f"eq.{tenant}","room_id":f"eq.{room}"}
        async def rows(table):
            return await request("GET",table,params={**filters,"select":"*"})

        # All preflights precede fixture creation. Empty scope cannot write anything.
        for table in ("documents","document_versions","document_chunks","document_imports"):
            await request("GET",table,params={"select":"tenant_id","limit":"0"})
        preflight=await database.post(_rest_url(settings,"rpc/document_operation"),headers=_supabase_headers(settings),
            json=dict(p_tenant_id=tenant,p_room_ids=[],p_operation="import_status",p_data={}))
        assert preflight.status_code==403 and preflight.json().get("code")=="42501"
        try:
            await request("POST","tenants",json={"id":tenant,"name":marker})
            created=True
            await request("POST","rooms",json={"id":room,"tenant_id":tenant,"switch_room_id":marker,"name":marker})
            api=FastAPI()
            api.include_router(router)

            api.dependency_overrides[document_repository]=lambda:repo
            header={"X-Admin-Token":original.admin_api_token}
            # Only backend configuration is substituted. Real auth and room-scope
            # dependencies execute; no route authorization dependency is bypassed.
            with patch("app.controllers.documents.get_settings",return_value=settings):
              async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url="http://fixture.invalid",
                                         headers=header,params={"room_id":marker},trust_env=False) as http:
                async def published(expected):
                    async with asyncio.timeout(60):
                        while True:
                            detail=await http.get(root)
                            assert detail.status_code==200
                            if detail.json()["published_version_id"]==expected:
                                return
                            await asyncio.sleep(0.5)

                params=dict(import_id=str(uuid4()),source_key=marker,filename="design.md",title="Fixture API design",expected_version=0)
                source=("# API design\n\nThe backend web API uses FastAPI.\nFixture fact: "+marker+" keeps architecture notes.").encode()
                denied=await http.get("/api/v1/documents",headers={"X-Admin-Token":"fixture-invalid"})
                assert denied.status_code==401
                denied=await http.get("/api/v1/documents",params={"room_id":"fixture-unauthorized-room"})
                assert denied.status_code==403
                first=await http.post("/api/v1/documents/imports",params=params,content=source)
                assert first.status_code==202
                initial=first.json()
                document_id,version_id=initial["document_id"],initial["version_id"]
                root="/api/v1/documents/"+document_id
                versions=await rows("document_versions")
                assert len(versions)==1 and versions[0]["source_text"]==source.decode()
                assert versions[0]["content_hash"]==digest(source.decode())
                worker.start()
                await published(version_id)
                await worker.close()
                chunks=await rows("document_chunks")
                assert chunks and all(c["status"]=="completed" for c in chunks)
                for chunk in chunks:
                    vector=json.loads(chunk["embedding"]) if isinstance(chunk["embedding"],str) else chunk["embedding"]
                    validate_vector(vector)
                    assert chunk["model_recipe"]==RECIPE and chunk["embedding_content_hash"]==chunk["content_hash"]
                    assert chunk["text"]==source.decode()[chunk["start_char"]:chunk["end_char"]]
                    assert len(get_embedder().tokenizer.encode(chunk["text"]).ids)<=512
                    assert chunk["start_line"]==source.decode()[:chunk["start_char"]].count("\n")+1
                    assert chunk["end_line"]==source.decode()[:chunk["end_char"]-1].count("\n")+1
                query=dict(query="Which framework powers the backend API?",document_id=document_id)
                search=await http.post("/api/v1/documents/search",json=query)
                assert search.status_code==200
                hit=search.json()[0]
                assert hit["version_id"]==version_id and hit["source_type"]=="document"
                assert hit["citation"]==f"document:{document_id}:{version_id}:{hit['chunk_id']}"
                supporting=await http.get(root+"/versions/"+version_id+"/chunks/"+hit["chunk_id"])
                assert supporting.status_code==200 and supporting.json()["text"]==hit["text"]
                assert hit["text"] in source.decode()
                unchanged=await http.post("/api/v1/documents/imports",params={**params,"import_id":str(uuid4()),"expected_version":1},content=source)
                assert unchanged.status_code==202 and unchanged.json()["duplicate"]
                assert unchanged.json()["version_id"]==version_id and len(await rows("document_versions"))==1
                assert len(await rows("document_chunks"))==len(chunks)
                update=await http.post("/api/v1/documents/imports",params={**params,"import_id":str(uuid4()),"expected_version":1},
                                       content=source+b"\n\nThe current backend runtime is Python.")
                assert update.status_code==202 and update.json()["version_number"]==2
                before=await http.post("/api/v1/documents/search",json=query)
                assert all(h["version_id"]==version_id for h in before.json())
                worker=DocumentWorker(repo,settings_provider=lambda:settings)
                worker.start()
                await published(update.json()["version_id"])
                await worker.close()
                after=await http.post("/api/v1/documents/search",json=query)
                assert after.json() and all(h["version_id"]==update.json()["version_id"] for h in after.json())
                # Controlled failure affects only this generated fixture. Previous
                # publication must survive both failure and obsolete completion.
                failed=await http.post("/api/v1/documents/imports",params={**params,"import_id":str(uuid4()),"expected_version":2},
                                       content=source+b"\n\nControlled failure fixture version.")
                assert failed.status_code==202
                job=(await repo.call(scope,"claim",{}))[0]
                completion=dict(id=job["id"],claim_token=job["claim_token"],content_hash=job["content_hash"],model_recipe=RECIPE)
                assert await repo.call(scope,"finish",dict(completion,vector=None,error="inference_failed"))
                preserved=await http.post("/api/v1/documents/search",json=query)
                assert preserved.json() and all(h["version_id"]==update.json()["version_id"] for h in preserved.json())
                assert any(c["error"]=="inference_failed" for c in await rows("document_chunks"))
                newest=await http.post("/api/v1/documents/imports",params={**params,"import_id":str(uuid4()),"expected_version":3},
                                       content=source+b"\n\nNewest successfully published fixture version.")
                assert newest.status_code==202
                assert not await repo.call(scope,"finish",dict(completion,vector=vector,error=None))
                worker.start()
                await published(newest.json()["version_id"])
                await worker.close()
                # Cross-tenant database access is rejected before reading/ranking.
                try:
                    await repo.call(DocumentScope(str(uuid4()),(room,)),"document",dict(document_id=document_id))
                    raise AssertionError("cross_tenant_access_accepted")
                except HTTPException as error:
                    assert error.status_code==403
                versions=await rows("document_versions")
                chunks=await rows("document_chunks")
                assert len(versions)==4 and len(chunks)==4
                assert sum(c["status"]=="completed" for c in chunks)==3
                archived=await http.post(root+"/archive",params={"expected_version":4})
                assert archived.status_code==200 and archived.json()["active"] is False
                assert (await http.post("/api/v1/documents/search",json=query)).json()==[]
                assert len(await rows("document_versions"))==4
                evidence.update(import_publication_retrieval_citation_verified=True,real_vector_dimensions=384,
                    documents=1,versions=4,chunks=4,completed_vectors=3,unchanged_deduplication_verified=True,
                    automatic_publication_verified=True,safe_replacement_verified=True,failure_preserved_publication=True,
                    worker_restart_recovery_verified=True,
                    stale_worker_rejected=True,archive_verified=True,unauthorized_and_cross_tenant_rejected=True,
                    paraphrase_similarity=hit["similarity"])
        finally:
            await worker.close()
            if created:
                await request("PATCH","documents",params=filters,json={"published_version_id":None})
                for table in ("document_imports","document_chunks","document_versions","documents"):
                    await request("DELETE",table,params=filters)
                    assert await rows(table)==[]
                await request("DELETE","rooms",params={"id":f"eq.{room}","tenant_id":f"eq.{tenant}"})
                await request("DELETE","tenants",params={"id":f"eq.{tenant}"})
                assert await request("GET","tenants",params={"id":f"eq.{tenant}","select":"id"})==[]
                evidence["exact_scope_cleanup_verified"]=True
    return evidence


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-approved-fixture",action="store_true",required=True)
    parser.parse_args()
    try:
        print(json.dumps(asyncio.run(verify())))
    except Exception:
        print('{"error":"document_fixture_failed_or_cleanup_incomplete"}')
        raise SystemExit(1) from None
