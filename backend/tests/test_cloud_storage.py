"""Offline HTTP/authentication and minimum-schema proposal tests; no live credentials."""

import asyncio
import base64
import json

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.supabase_auth import backend_headers, backend_key_type
from app.repositories.record_repository import check_database_connection, storage_rpc_available
from app.schema_upgrade import build_storage_upgrade_sql
from app import verify_cloud_storage
from app.core.message_logging import redact_source_identifier,message_text_for_log
from app.services.ai_service import AIService
from app.models.messages import IncomingMessage
from app.repositories.message_receipts import MessageReceiptRepository


def fake_jwt(role):
    # Deliberately unsigned synthetic token; never a real API credential.
    payload=base64.urlsafe_b64encode(json.dumps({"role":role}).encode()).decode().rstrip('=')
    return "fixture."+payload+".unsigned"


def test_new_secret_uses_apikey_without_bearer_jwt():
    key="sb_secret_fixture_not_a_real_key"
    assert backend_key_type(key)=="secret"
    assert backend_headers(key)=={"apikey":key}


def test_legacy_service_role_preserves_bearer_authentication():
    key=fake_jwt("service_role")
    assert backend_key_type(key)=="legacy_service_role"
    assert backend_headers(key)=={"apikey":key,"Authorization":"Bearer "+key}


@pytest.mark.parametrize("key",["sb_publishable_fixture_not_real",fake_jwt("anon"),fake_jwt("authenticated"),"bad.invalid.jwt"])
def test_public_or_invalid_credentials_are_not_backend_credentials(key):
    with pytest.raises(HTTPException) as error:
        backend_headers(key)
    assert error.value.status_code==503
    assert key not in str(error.value.detail)


def test_anon_variable_is_not_a_backend_secret_alias(monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY","fixture-public-key")
    assert Settings(_env_file=None).supabase_service_role_key==""


@pytest.mark.parametrize("rpc_present",[True,False])
def test_cloud_connection_and_schema_are_separate_checks(rpc_present):
    async def run():
        config=Settings(_env_file=None).model_copy(update={"supabase_url":"https://fixture.invalid",
            "supabase_service_role_key":"sb_secret_fixture_not_a_real_key"})
        seen=[]

        def handle(request):
            seen.append(request)
            assert "authorization" not in request.headers
            assert request.headers["apikey"]==config.supabase_service_role_key
            if request.url.path.endswith("/tenants"):
                return httpx.Response(200,json=[])
            return httpx.Response(200,json={"paths":{"/rpc/store_message_extraction":{}} if rpc_present else {}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert await check_database_connection(config,client=client)=={"status":"connected"}
            assert await storage_rpc_available(config,client=client) is rpc_present
        assert all(request.method=="GET" for request in seen)
    asyncio.run(run())


def test_storage_only_proposal_does_not_require_embeddings_or_touch_tables():
    sql=build_storage_upgrade_sql()
    assert "CREATE OR REPLACE FUNCTION public.store_message_extraction" in sql
    assert "REVOKE ALL" in sql and "TO service_role" in sql
    for forbidden in ("ALTER TABLE","DROP TABLE","CREATE TABLE","embedding_status","claim_memory_embeddings","CASCADE"):
        assert forbidden not in sql
    assert "approval_status" not in sql and "task_status" not in sql


def test_cloud_fixture_refuses_to_create_data_before_storage_rpc_exists(monkeypatch):
    async def preflight(settings):
        return {"cloud_status":"connected","storage_rpc_available":False}

    monkeypatch.setattr(verify_cloud_storage,"check",preflight)
    result=asyncio.run(verify_cloud_storage.verify(Settings(_env_file=None)))
    assert result["fixture_state"]=="blocked_missing_storage_rpc"
    assert result["fixture_rows_created"]==0


def test_source_uuids_and_long_platform_ids_survive_but_credentials_do_not():
    config=Settings(_env_file=None)
    for identifier in ("00000000-0000-0000-0000-000000000099","switch-participant-"+"a"*40):
        assert redact_source_identifier(identifier,config)==identifier
        assert json.loads(message_text_for_log(identifier,config))!=identifier
    assert redact_source_identifier(config.switch_ingest_token.get_secret_value(),config)=="[REDACTED]"
    assert redact_source_identifier("sb_secret_fixture_not_real",config)=="[REDACTED]"
    assert redact_source_identifier("https://fixture.invalid/private",config)=="[REDACTED]"


def test_redaction_configuration_failure_is_retryable_before_admission(monkeypatch):
    def unavailable(path):
        raise OSError("fixture redaction unavailable")

    monkeypatch.setattr("app.core.message_logging.dotenv_values",unavailable)
    async def run():
        config=Settings(_env_file=None)
        receipts=MessageReceiptRepository()
        service=AIService()
        try:
            incoming=IncomingMessage(tenant_id=config.switch_tenant_id,room_id="test-room",
                message_id="fixture-message",sender="fixture-human",body="A useful fixture")
            with pytest.raises(HTTPException) as error:
                await service.ingest(incoming,config,receipts)
            assert error.value.status_code==503
            assert receipts.recent(config.switch_tenant_id,config.allowed_switch_rooms)==[]
            assert service.pending==0
        finally:
            await service.close()
    asyncio.run(run())
