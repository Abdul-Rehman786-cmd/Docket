"""Read-only hosted metadata probe. Never applies SQL or prints credentials/records."""
import asyncio
import json
import logging
import httpx
from app.core.config import get_settings
from app.core.supabase_auth import backend_headers

FUNCTIONS=('store_message_extraction','claim_memory_embeddings','finish_memory_embedding','read_commitment_lifecycle',
           'apply_commitment_action','prepare_commitment_chat','apply_commitment_chat_action',
           'read_reminder_work','persist_reminder_intention','claim_reminder_deliveries','begin_reminder_dispatch','finish_reminder_delivery')
COLUMNS={'commitments':('version','cancelled_at','automated_acceptance_at','automated_acceptance_source_id'),
         'memories':('content_hash','embedding_content_hash','embedding_status','embedding_claim_token','embedding_lease_until'),
         'commitment_action_events':('source_evidence_id','backend_actor','outcome'),
         'commitment_chat_policies':('enabled','timezone','accept_pending_owner_completion','reminders_enabled','reminder_policy_version'),
         'commitment_chat_identities':('switch_author_id','subject_id'),
         'reminder_deliveries':('occurrence_key','commitment_version','policy_version','dispatch_started_at')}


async def inspect(settings,client):
    report={'mode':'read_only_postgrest_metadata','configuration':{
        'database_configured':settings.supabase_configured,'automatic_commitments_enabled':settings.automatic_commitments_enabled,
        'adapter_credential_configured':bool(settings.switch_adapter_token.get_secret_value()),
        'non_ai_identity_configured':bool(settings.switch_requester_agent_id and settings.switch_requester_api_key.get_secret_value()),
        'reminders_enabled':settings.reminders_enabled,'reminder_delivery_enabled':settings.reminder_delivery_enabled,
        'tenant_configured':settings.switch_tenant_id is not None,'configured_room_count':len(settings.allowed_switch_rooms)},
        'client_role_permissions':'not_verifiable_via_service_role_openapi',
        'function_definitions_and_constraints':'require_read_only_catalog_review'}
    if not settings.supabase_configured: return {**report,'status':'not_configured'}
    try:
        response=await client.get(settings.supabase_url.rstrip('/')+'/rest/v1/',
            headers={**backend_headers(settings.supabase_secret_key or settings.supabase_service_role_key),'Accept':'application/openapi+json'})
        if response.status_code!=200: return {**report,'status':'metadata_request_rejected','http_status':response.status_code}
        schema=response.json(); paths=schema.get('paths',{}); definitions=schema.get('definitions',{})
        report.update(status='metadata_available',functions={name:'/rpc/'+name in paths for name in FUNCTIONS},
            columns={table:{col:col in definitions.get(table,{}).get('properties',{}) for col in cols} for table,cols in COLUMNS.items()})
        return report
    except Exception:
        return {**report,'status':'metadata_unavailable'}  # No remote URL/body/exception contents.


async def main():
    logging.getLogger('httpx').setLevel(logging.CRITICAL)
    async with httpx.AsyncClient(timeout=10,trust_env=False,follow_redirects=False) as client:
        print(json.dumps(await inspect(get_settings(),client),indent=2))

if __name__=='__main__': asyncio.run(main())
