"""Opt-in real FastAPI -> hosted Supabase fixture, with a simulated extraction.

No Discord/Switch messages or embedding work. The HTTP handlers, parser, durable
journal, service, repository and cloud writes are real. Run only after RPC approval.
"""

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic import SecretStr

from app.core.config import BACKEND_DIR, Settings
from app.core.supabase_auth import backend_key_type
from app.main import app
from app.repositories.message_receipts import MessageReceiptRepository
from app.repositories.record_repository import (
    _rest_url, _supabase_headers, check_database_connection, storage_rpc_available,
)
from app.services.ai_service import response_scope


async def check(settings):
    connected = await check_database_connection(settings)
    return {"cloud_status":connected["status"],"backend_key_type":backend_key_type(settings.supabase_service_role_key),
            "storage_rpc_available":await storage_rpc_available(settings),
            "capture_overload_exposed":await storage_rpc_available(settings,capture=True)}


async def verify(settings):
    preflight = await check(settings)
    if not preflight["storage_rpc_available"]:
        return {**preflight,"fixture_state":"blocked_missing_storage_rpc","fixture_rows_created":0}
    if not preflight.get('capture_overload_exposed'):
        return {**preflight,'fixture_state':'blocked_missing_capture_overload','fixture_rows_created':0}
    tenant, room = uuid4(), uuid4()
    marker = "TM-cloud-fixture-"+str(uuid4())
    fixture = settings.model_copy(update={
        "switch_tenant_id":tenant,"switch_room_ids":marker,"switch_reply_to_discord":False,
        "local_embeddings_enabled":False,"log_discord_messages":False,
        "switch_agent_id":"fixture-ai","switch_requester_agent_id":"fixture-client",
        "switch_requester_api_key":SecretStr("fixture-not-a-real-credential"),
        "switch_ai_room_id":"fixture-private-room","switch_ai_sender_id":"fixture-ai-sender",
    })
    folder = BACKEND_DIR / ".local" / "cloud-fixtures" / str(tenant)
    folder.mkdir(parents=True,exist_ok=False)
    journal = folder / "ingestion.sqlite3"
    created = False
    evidence = {"fixture":marker,"tenant_id":str(tenant),"room_id":str(room),
                "extraction":"simulated","fastapi_and_cloud":"real"}
    print(json.dumps({**evidence,"fixture_state":"starting"}))
    async with httpx.AsyncClient(trust_env=False,timeout=20) as database:
        async def request(method,table,**kwargs):
            response=await database.request(method,_rest_url(fixture,table),headers=_supabase_headers(fixture),**kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

        async def rows(table):
            return await request("GET",table,params={"tenant_id":f"eq.{tenant}","room_id":f"eq.{room}","select":"*"})

        async def extraction(*args,**kwargs):
            return json.dumps({"action":"store_only",
                "commitments":[{"description":"send the fixture report"}]})

        try:
            await request("POST","tenants",json={"id":str(tenant),"name":marker})
            created=True
            await request("POST","rooms",json={"id":str(room),"tenant_id":str(tenant),"switch_room_id":marker,"name":marker})
            with ExitStack() as patches:
                for module in ("app.main","app.core.security","app.controllers.messages","app.controllers.ai",
                               "app.services.ai_service","app.services.embedding_service","app.repositories.record_repository"):
                    patches.enter_context(patch(module+".get_settings",return_value=fixture))
                patches.enter_context(patch("app.repositories.ingestion_jobs.JOURNAL_PATH",journal))
                patches.enter_context(patch("app.services.ai_service.ask_switch",extraction))
                app.state.message_receipts=MessageReceiptRepository()
                async with app.router.lifespan_context(app):
                    timestamp_ms=int(datetime.now(timezone.utc).timestamp()*1000)
                    payload={"tenant_id":str(tenant),"room_id":marker,"message_id":marker,
                        "sender":"fixture-human","sender_name":"Disposable fixture user",
                        "body":"@fixture-agent I'll send the fixture report by Friday.","addressed":True,
                        "timestamp_ms":timestamp_ms,"thread_id":"fixture-thread",
                        "workspace_id":"fixture-workspace","channel_id":"fixture-channel",
                        "external_message_id":"fixture-platform-message","external_author_id":"fixture-platform-author"}
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                base_url="http://127.0.0.1:8000",trust_env=False) as http:
                        header={"X-Ingest-Token":fixture.switch_ingest_token.get_secret_value()}
                        first=await http.post("/messages",headers=header,json=payload)
                        assert first.status_code==200
                        await asyncio.wait_for(app.state.ai_service.queue.join(),45)
                        result=app.state.ai_service.repository.recent(response_scope(fixture))[0]
                        assert result.status=="answered", "fixture_processing_failed"
                        expected={"messages":1,"commitments":1,"decisions":0,"memories":1}
                        assert result.stored_counts==expected
                        repeated=await http.post("/messages",headers=header,json=payload)
                        assert repeated.status_code==200 and repeated.json()["duplicate"]
                        await asyncio.wait_for(app.state.ai_service.queue.join(),45)
                        job=await app.state.ai_service.journal.get(first.json()["message_ref"])
                        assert job["state"]=="done" and job["attempts"]==1
            records={table:await rows(table) for table in expected}
            counts={table:len(data) for table,data in records.items()}
            assert counts==expected
            source=records["messages"][0]
            assert source["switch_message_id"]==marker and source["switch_author_id"]=="fixture-human"
            assert source["thread_id"]==payload["thread_id"] and source["platform"]=="discord"
            for field in ("workspace_id","channel_id","external_message_id","external_author_id"):
                assert source[field]==payload[field]
            assert int(datetime.fromisoformat(source["occurred_at"].replace("Z","+00:00")).timestamp()*1000)==timestamp_ms
            assert source["extraction_status"]=="completed" and source["extraction_completed_at"]
            for table in ("commitments","decisions","memories"):
                assert all(row["message_id"]==source["id"] for row in records[table])
            commitment=records["commitments"][0]
            assert commitment["approval_status"]=="pending" and commitment["task_status"]=="not_started"
            assert commitment['owner_switch_user_id']=='fixture-human' and commitment["due_at"] is None
            assert 'Deadline wording: Friday' in records['memories'][0]['content']
            evidence.update(stored_counts=counts,duplicate_counts_unchanged=True,source_links_verified=True,
                            source_metadata_verified=True,human_approval_defaults_verified=True)
        finally:
            if created:
                for table in ("memories","commitments","decisions","messages"):
                    await request("DELETE",table,params={"tenant_id":f"eq.{tenant}","room_id":f"eq.{room}"})
                remaining={table:len(await rows(table)) for table in ("memories","commitments","decisions","messages")}
                assert not any(remaining.values())
                evidence["remaining_fixture_record_counts"]=remaining
                await request("DELETE","rooms",params={"id":f"eq.{room}","tenant_id":f"eq.{tenant}"})
                await request("DELETE","tenants",params={"id":f"eq.{tenant}"})
                assert await request("GET","tenants",params={"id":f"eq.{tenant}","select":"id"})==[]
                evidence["cleanup_verified"]=True
            # Exact fixture files only; never touch the application's real journal.
            if folder.resolve().parent == (BACKEND_DIR/".local/cloud-fixtures").resolve():
                journal.unlink(missing_ok=True)
                journal.with_name(journal.name+"-journal").unlink(missing_ok=True)
                folder.rmdir()
    return evidence


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check",action="store_true")
    mode.add_argument("--run-approved-fixture",action="store_true")
    args=parser.parse_args()
    try:
        settings=Settings()
        print(json.dumps(asyncio.run(check(settings) if args.check else verify(settings))))
    except Exception:
        print('{"fixture_state":"failed_or_cleanup_incomplete"}')
        raise SystemExit(1) from None
