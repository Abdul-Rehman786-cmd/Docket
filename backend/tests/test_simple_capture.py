"""Mocked AI/storage tests with the real durable SQLite ingestion journal."""
import asyncio
import json
from datetime import datetime,timezone
import pytest
import httpx
from fastapi import HTTPException
from app.models.ai import LLMDecision
from app.models.messages import IncomingMessage
from app.repositories.message_receipts import MessageReceiptRepository
from app.services import ai_service
from app.services.extraction_service import capture_decision,storage_items,store_ai_memory
from test_useful_storage import config, message

@pytest.fixture
def anyio_backend(): return 'asyncio'

def meta(**changes):
    return dict(addressed=True,switch_message_id='capture-message',switch_author_id='new-sender',
        author_name='Sender label',timestamp_ms=1793872800000,occurred_at='2026-11-05T10:00:00+00:00',
        capture_timezone='UTC',origin='discord',durable=True,room_id='test-room',**changes)

@pytest.mark.parametrize('body,metadata,expected_due,wording',[
    ("@bot I'll finish the report by Friday.",meta(),None,'Friday'),
    ("<@12345> I’ll finish the report by Friday at 09:00.",meta(),'2026-11-06T09:00:00+00:00','Friday at 09:00'),
    ("@alias I will finish the report by 2026-11-06T09:00:00+05:00.",meta(),'2026-11-06T04:00:00+00:00','2026-11-06T09:00:00+05:00'),
])
def test_sender_owned_capture_preserves_deadline_without_identity_setup(body,metadata,expected_due,wording):
    raw=LLMDecision(action='store_only',commitments=[{'description':'Model paraphrase','owner_name':'Someone else','due_at':'2030-01-01T12:00:00Z'}])
    result=capture_decision(raw,body,metadata,config())
    assert result.commitments[0].description=='finish the report'
    assert result.commitments[0].owner_name=='Sender label' and result.commitments[0].due_at==expected_due
    items=storage_items(result,config(),source_metadata={**metadata,'commitment_capture':True,'commitment_deadline_text':wording})
    assert len(items['commitments'])==len(items['memories'])==1
    assert 'Assignee: new-sender' in items['memories'][0]['content']
    assert 'Deadline wording: '+wording in items['memories'][0]['content']
    assert 'Approval: pending' in items['memories'][0]['content'] and 'explicit sender statement' in items['memories'][0]['content']

@pytest.mark.anyio
@pytest.mark.parametrize('bot_classification',[None,True])
@pytest.mark.parametrize('body,addressed,creates',[
    ("@bot I'll finish the report by Friday.",True,True),
    ("<@12345> I’ll finish the report by Friday.",True,True),
    ("I'll finish the report by Friday.",False,False),
    ("I'll finish the report by Friday.",True,False), # Addressed follow-up is not a fresh mention.
    ("@bot I'll finish the report by Friday.",False,False),
    ('@bot Hello!',True,False),('@bot When is the report due?',True,False),
    ('@bot Ali will finish the report.',True,False),
    ('@bot I might finish the report.',True,False),
    ('@bot "I will finish the report."',True,False),
])
async def test_mocked_ingestion_simple_policy_without_mapping_or_bot_verification(monkeypatch,body,addressed,creates,bot_classification):
    settings=config(switch_reply_to_discord=True,automatic_commitments_enabled=False)
    worker=ai_service.AIService();saved=[];posted=[]
    async def ask(*args,**kwargs):
        return json.dumps({'action':'store_only','commitments':[{'description':'finish the report','owner_name':'Spoofed label'}]})
    async def store(settings,**kwargs):
        saved.append((kwargs,storage_items(kwargs['decision'],settings,source_metadata=kwargs['source_metadata'])))
        return dict(messages=1,commitments=1,decisions=0,memories=1)
    async def post(self,room_id,body,thread_id=None):posted.append(body)
    async def forbidden_prepare(*args,**kwargs):raise AssertionError('Creation must not call membership/lifecycle preparation')
    monkeypatch.setattr(ai_service,'ask_switch',ask);monkeypatch.setattr(ai_service,'store_ai_memory',store)
    monkeypatch.setattr(ai_service.SwitchAPI,'post_room_message',post)
    monkeypatch.setattr(ai_service.CommitmentChatService,'prepare_open',forbidden_prepare if creates else lambda *args:asyncio.sleep(0))
    incoming=message(settings,body,addressed=addressed,sender='unregistered-stable-sender',sender_is_bot=bot_classification)
    receipt=await worker.ingest(incoming,settings,MessageReceiptRepository()) # No adapter attestation.
    await worker.queue.join()
    result=worker.repository.recent(ai_service.response_scope(settings))[0]
    assert result.status=='answered' and bool(saved)==creates
    if creates:
        kwargs,items=saved[0]
        assert kwargs['source_metadata']['switch_author_id']=='unregistered-stable-sender'
        assert kwargs['source_metadata']['commitment_capture'] and kwargs['decision'].commitments[0].due_at is None
        assert 'Deadline wording: Friday' in items['memories'][0]['content']
        assert posted[-1]=='Task saved in Supabase. Search indexing is pending.'
        assert await worker.pending_confirmations.get('test-room:thread-1') is None
        replay=await worker.ingest(incoming,settings,MessageReceiptRepository())
        assert replay.duplicate and len(saved)==1
    else:assert not any('Task saved' in text for text in posted)
    assert (await worker.journal.get(receipt.message_ref))['state']=='done'
    await worker.close()

def test_missing_timezone_and_original_timestamp_do_not_invent_deadlines():
    decision=LLMDecision(action='store_only',commitments=[{'description':'finish report'}])
    for changes in ({'capture_timezone':None},{'timestamp_ms':None}):
        metadata=meta();metadata.update(changes)
        result=capture_decision(decision,"@bot I'll finish report by Friday at 09:00.",metadata,config())
        assert result.commitments[0].due_at is None

@pytest.mark.anyio
async def test_storage_failure_never_confirms_success_and_retry_reuses_extraction(monkeypatch):
    settings=config(switch_reply_to_discord=True);first=ai_service.AIService();posted=[];calls=[];writes=[]
    async def ask(*args,**kwargs):
        calls.append(True);return '{"action":"store_only","commitments":[{"description":"finish report"}]}'
    async def store(*args,**kwargs):
        writes.append(True)
        if len(writes)==1:raise HTTPException(503,'database_write_failed')
        return dict(messages=1,commitments=1,decisions=0,memories=1)
    async def post(self,room_id,body,thread_id=None):posted.append(body)
    monkeypatch.setattr(ai_service,'ask_switch',ask);monkeypatch.setattr(ai_service,'store_ai_memory',store)
    monkeypatch.setattr(ai_service.SwitchAPI,'post_room_message',post)
    incoming=message(settings,"@bot I'll finish report by Friday.",addressed=True)
    receipt=await first.ingest(incoming,settings,MessageReceiptRepository());await first.queue.join()
    assert (await first.journal.get(receipt.message_ref))['state']=='failed'
    assert not any('saved' in text.lower() for text in posted)
    await first.close();second=ai_service.AIService()
    await second.journal.retry(receipt.message_ref)
    await second._enqueue_durable(await second.journal.get(receipt.message_ref),settings);await second.queue.join()
    assert len(calls)==1 and posted[-1]=='Task saved in Supabase. Search indexing is pending.'
    await second.close()

@pytest.mark.anyio
async def test_capture_requires_new_atomic_overload_and_does_not_log_remote_error_body(caplog):
    settings=config(supabase_url='https://fixture.invalid',supabase_service_role_key='sb_secret_fixture_not_real')
    decision=LLMDecision(action='store_only',commitments=[{'description':'finish report'}])
    seen=[]
    def transport(request):
        seen.append(json.loads(request.content))
        return httpx.Response(404,json={'code':'PGRST202','message':'private remote diagnostic never log this'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        from app.repositories.record_repository import persist_extraction
        with pytest.raises(HTTPException):
            await persist_extraction(settings,tenant_id=settings.switch_tenant_id,switch_room_id='test-room',
                switch_message_id='capture-message',author_name='Label',body='Fixture source',occurred_at='2026-11-05T10:00:00Z',
                items=storage_items(decision,settings),source_metadata={**meta(),'commitment_capture':True},client=client)
    assert seen[0]['p_capture'] is True and seen[0]['p_source']['switch_author_id']=='new-sender'
    assert 'private remote diagnostic' not in caplog.text

@pytest.mark.anyio
@pytest.mark.parametrize('exposed',[True,False])
async def test_read_only_capture_overload_detection(exposed):
    from app.repositories.record_repository import storage_rpc_available
    settings=config(supabase_url='https://fixture.invalid',supabase_service_role_key='sb_secret_fixture_not_real')
    schema={'paths':{'/rpc/store_message_extraction':{'post':{'parameters':[{'name':'args','schema':{'properties':{'p_capture':{'type':'boolean'}} if exposed else {}}}]}}}}
    def handler(request):
        assert request.method=='GET'
        return httpx.Response(200,json=schema)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await storage_rpc_available(settings,client=client) is True
        assert await storage_rpc_available(settings,capture=True,client=client) is exposed
