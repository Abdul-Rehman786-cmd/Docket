"""Mocked AI, fake lifecycle persistence; real local ingestion journal. No chat/cloud writes."""
import asyncio
import json
from copy import deepcopy
from datetime import datetime,timezone
from uuid import uuid4,uuid5,NAMESPACE_URL

import httpx
import pytest

from app.core.config import Settings
from app.core.security import trusted_adapter
from app.main import app
from app.models.commitment_chat import ChatPlan,ChatSource,ChatMember,ChatPolicy,TaskChangeProposal
from app.models.commitments import Actor,CommitmentError,Action,CompleteRequest
from app.models.messages import IncomingMessage
from app.repositories.message_receipts import MessageReceiptRepository
from app.services import ai_service
from app.services.commitment_chat_service import CommitmentChatService
from app.services.commitment_service import CommitmentService
from test_commitment_lifecycle import FakeRepository,NOW,WORKER


@pytest.fixture
def anyio_backend(): return 'asyncio'


class FakeChatRepository(FakeRepository):
    def __init__(self):
        super().__init__()
        self.roles['admin']='admin'
        self.policy=ChatPolicy(timezone='UTC',accept_pending_owner_completion=True)
        self.identities={'puppet-worker':'worker','puppet-admin':'admin','puppet-replacement':'replacement'}
        self.members=[ChatMember(subject_id='worker',label='Worker',external_author_id='platform-worker'),
            ChatMember(subject_id='replacement',label='Rehman',external_author_id='platform-replacement')]
        self.sources={}; self.extra=[]; self.fail_after_commit=False; self.prepared=0

    async def prepare_chat(self,tenant_id,meta,body):
        self.prepared+=1
        if tenant_id!=self.scope.tenant_id or meta['room_id']!='switch-room': return None
        subject=self.identities.get(meta.get('switch_author_id'))
        if subject not in self.roles: return None
        source=ChatSource(id=uuid5(NAMESPACE_URL,meta['switch_message_id']),scope=self.scope,
            switch_message_id=meta['switch_message_id'],switch_author_id=meta['switch_author_id'],
            external_author_id=meta.get('external_author_id'),actor=Actor(subject_id=subject),body=body,
            external_message_id=meta.get('external_message_id'),workspace_id=meta.get('workspace_id'),channel_id=meta.get('channel_id'),
            occurred_at=meta['occurred_at'],thread_id=meta.get('thread_id'))
        if source.id in self.sources and self.sources[source.id]!=source:
            raise CommitmentError('idempotency_conflict',409)
        self.sources[source.id]=source
        candidates=[r for r in [self.record,*self.extra] if r.approval_status!='rejected'
            and r.task_status not in ('completed','cancelled')
            and (r.owner_switch_user_id==subject or self.roles[subject]=='admin')]
        return ChatPlan(source=source,role=self.roles[subject],policy=self.policy,candidates=candidates,members=self.members)

    async def apply_chat_action(self,execution,commitment_id,action,request,at):
        source=execution.source
        if self.sources.get(source.id)!=source or self.identities.get(source.switch_author_id)!=source.actor.subject_id:
            raise CommitmentError('forbidden_action',403)
        if self.policy!=execution.policy: raise CommitmentError('forbidden_action',403)
        if any(e.source_evidence_id==source.id and e.idempotency_key!=request.idempotency_key for e in self.events):
            raise CommitmentError('idempotency_conflict',409)
        result=await super().apply_action(source.scope,source.actor,commitment_id,action,request,at,execution)
        if self.fail_after_commit:
            self.fail_after_commit=False
            raise CommitmentError('commitment_storage_unavailable',503)
        return result


def config(repo):
    return Settings(_env_file=None,automatic_commitments_enabled=True,switch_tenant_id=repo.scope.tenant_id,
        switch_room_ids='switch-room',switch_requester_agent_id='client',switch_requester_api_key='fixture-client-key',
        switch_ai_room_id='private-room',switch_ai_sender_id='ai-sender',switch_agent_id='ai',switch_ai_agent_name='codex-discord',
        switch_reply_to_discord=False)


def metadata(repo,sender='worker',**overrides):
    data=dict(room_id='switch-room',switch_message_id=str(uuid4()),switch_author_id='puppet-'+sender,
        external_author_id=None,occurred_at=NOW.isoformat(),timestamp_ms=int(NOW.timestamp()*1000),
        thread_id=None,durable=True,adapter_verified=True)
    return {**data,**overrides}


async def prepared(repo,body,sender='worker',**overrides):
    service=CommitmentChatService(repo,CommitmentService(repo,clock=lambda:NOW))
    meta=metadata(repo,sender,**overrides)
    plan=await service.prepare(config(repo),meta,body)
    return service,plan


def proposal(repo,plan,action,**fields):
    return TaskChangeProposal(action=action,commitment_id=repo.record.id,
        source_message_id=plan.source.switch_message_id,evidence=plan.source.body.strip(),clarification_required=False,**fields)


@pytest.mark.anyio
@pytest.mark.parametrize('action,body,fields,sender',[
    ('complete',"I've sent the report.",{},'worker'),
    ('cancel',"Cancel my report task; it's no longer needed.",{},'worker'),
    ('reassign','Rehman is taking over the report.',{'assignee_id':'replacement'},'admin'),
    ('reschedule','Move my report deadline to Monday.',{'due_at':'2026-11-09T12:00:00Z'},'worker'),
])
async def test_clear_updates_change_state_audit_memory_once(action,body,fields,sender):
    repo=FakeChatRepository(); original=deepcopy(repo.source)
    service,plan=await prepared(repo,body,sender)
    change=proposal(repo,plan,action,**fields)
    result=await service.execute(plan,[change],'fixture-ref')
    assert result.status=='applied' and repo.record.version==2 and len(repo.events)==1
    event=repo.events[0]
    assert event.actor_id==sender and event.backend_actor=='switch-ingestion-worker'
    assert event.evidence==body and event.source_evidence_id==plan.source.id and event.outcome=='applied'
    assert event.before['version']==1 and event.after['version']==2
    assert repo.source==original and repo.memory['embedding'] is None
    if action=='complete':
        assert repo.record.task_status=='completed' and repo.record.completed_at==NOW
        assert repo.record.approval_status=='pending' and repo.record.approval_at is None
        assert repo.record.automated_acceptance_at==NOW and 'automated owner' in repo.memory['content']
        assert not repo.memory['active']
    if action=='cancel': assert repo.record.task_status=='cancelled' and not repo.memory['active']
    if action=='reassign': assert repo.record.owner_switch_user_id=='replacement'
    if action=='reschedule': assert repo.record.due_at==datetime(2026,11,9,12,tzinfo=timezone.utc)
    replay=await service.execute(plan,[change],'fixture-ref')
    assert replay.event_id==result.event_id and len(repo.events)==1 and repo.record.version==2


@pytest.mark.anyio
@pytest.mark.parametrize('body',[
    'I might finish tomorrow.','Has Ali finished?', '"I have sent the report."',
    '> I have sent the report.', 'If I have sent the report.', 'I have not sent the report.',
    "I've sent the report, but maybe not.", 'Someone said I have sent the report.',
])
async def test_questions_tentative_quoted_and_hypothetical_never_mutate(body):
    repo=FakeChatRepository(); service,plan=await prepared(repo,body)
    original=repo.record
    result=await service.execute(plan,[proposal(repo,plan,'complete')],'ref')
    assert result.status=='ambiguous' and repo.record==original and not repo.events


@pytest.mark.anyio
async def test_ambiguity_authority_identity_and_handoffs():
    repo=FakeChatRepository(); repo.extra=[repo.record.model_copy(update={'id':uuid4(),'extraction_key':'two'})]
    service,plan=await prepared(repo,'Done')
    assert (await service.execute(plan,[proposal(repo,plan,'complete')],'ref')).status=='ambiguous'
    repo.extra=[]
    service,plan=await prepared(repo,'Rehman is taking over the report.')
    assert (await service.execute(plan,[proposal(repo,plan,'reassign',assignee_id='replacement')],'ref')).status=='unauthorized'
    repo.members.append(ChatMember(subject_id='worker',label='Rehman'))
    service,plan=await prepared(repo,'Rehman is taking over the report.','admin')
    assert (await service.execute(plan,[proposal(repo,plan,'reassign',assignee_id='replacement')],'ref')).status=='ambiguous'
    service,plan=await prepared(repo,"I've sent the report.",'replacement')
    assert (await service.execute(plan,[proposal(repo,plan,'complete')],'ref')).status=='ambiguous'
    service,plan=await prepared(repo,"I've sent the report.",'unknown')
    assert plan is None
    assert (await service.execute(None,[],'ref')).status=='unauthorized'
    assert not repo.events and repo.record.version==1


@pytest.mark.anyio
async def test_deadline_needs_resolved_date_time_and_timezone():
    repo=FakeChatRepository(); repo.policy=repo.policy.model_copy(update={'timezone':None})
    service,plan=await prepared(repo,'Move my report deadline to Monday.')
    p=proposal(repo,plan,'reschedule',due_at='2026-11-09T12:00:00Z')
    assert (await service.execute(plan,[p],'ref')).status=='ambiguous'
    repo.policy=repo.policy.model_copy(update={'timezone':'UTC'}); repo.record=repo.record.model_copy(update={'due_at':None})
    service,plan=await prepared(repo,'Move my report deadline to Monday.')
    assert (await service.execute(plan,[proposal(repo,plan,'reschedule',due_at=p.due_at)],'ref')).status=='ambiguous'
    service,plan=await prepared(repo,'Move my report deadline to 2026-12-01T17:00:00+05:00.')
    result=await service.execute(plan,[proposal(repo,plan,'reschedule',due_at='2026-12-01T12:00:00Z')],'ref')
    assert result.status=='applied' and repo.record.due_at.hour==12


@pytest.mark.anyio
async def test_pending_policy_and_public_rules_remain_separate():
    repo=FakeChatRepository(); repo.policy=repo.policy.model_copy(update={'accept_pending_owner_completion':False})
    service,plan=await prepared(repo,"I've sent the report.")
    assert (await service.execute(plan,[proposal(repo,plan,'complete')],'ref')).status=='conflict'
    with pytest.raises(CommitmentError,match='invalid_transition'):
        await CommitmentService(repo).perform(repo.scope,WORKER,repo.record.id,Action.complete,
            CompleteRequest(idempotency_key=uuid4(),expected_version=1))
    repo.policy=repo.policy.model_copy(update={'accept_pending_owner_completion':True})
    service,plan=await prepared(repo,"I've sent the report.",'admin')
    assert (await service.execute(plan,[proposal(repo,plan,'complete')],'ref')).status=='conflict'
    assert not repo.events


@pytest.mark.anyio
async def test_stale_source_revocation_and_atomic_failure():
    repo=FakeChatRepository(); service,plan=await prepared(repo,"I've sent the report.")
    p=proposal(repo,plan,'complete')
    repo.record=repo.record.model_copy(update={'version':2})
    assert (await service.execute(plan,[p],'ref')).status=='conflict'
    repo.record=repo.record.model_copy(update={'version':1}); repo.identities.clear()
    assert (await service.execute(plan,[p],'ref')).status=='unauthorized'
    repo.identities['puppet-worker']='worker'; repo.fail=True
    before=deepcopy((repo.record,repo.events,repo.receipts,repo.memory))
    with pytest.raises(RuntimeError): await service.execute(plan,[p],'ref')
    assert (repo.record,repo.events,repo.receipts,repo.memory)==before


@pytest.mark.anyio
async def test_evidence_source_and_model_ids_must_match_backend_candidates():
    repo=FakeChatRepository(); service,plan=await prepared(repo,"I've sent the report.")
    p=proposal(repo,plan,'complete')
    for change in [{'commitment_id':uuid4()},{'source_message_id':'invented'},
                   {'evidence':'sent the report'},{'clarification_required':True}]:
        assert (await service.execute(plan,[p.model_copy(update=change)],'ref')).status=='ambiguous'
    assert not repo.events


@pytest.mark.anyio
async def test_authenticated_adapter_boundary_and_env_routes_fail_closed(monkeypatch):
    from app.core.config import get_settings
    token='fixture-adapter-credential-long-enough-123'
    monkeypatch.setenv('SWITCH_ADAPTER_TOKEN',token); get_settings.cache_clear()
    settings=get_settings()
    assert trusted_adapter(token,settings) and not trusted_adapter('test-ingest-token',settings)
    assert not trusted_adapter('test-ingest-token',settings.model_copy(update={'switch_adapter_token':settings.switch_ingest_token}))
    captured=[]
    class Stub:
        async def ingest(self,message,settings,receipts,**kwargs):
            from app.services.message_service import receive_message
            captured.append(kwargs['adapter_verified'])
            return receive_message(message,settings,receipts)
    monkeypatch.setattr(app.state,'ai_service',Stub())
    message=IncomingMessage(tenant_id=settings.switch_tenant_id,room_id='test-room',message_id='fixture',sender='sender',body='Done')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        path='/messages'; headers={'X-Ingest-Token':'test-ingest-token'}
        assert (await client.post(path,headers={**headers,'X-Adapter-Token':token},json=message.model_dump(mode='json'))).status_code==200
        assert (await client.post(path,headers=headers,json=message.model_dump(mode='json'))).status_code==200
        assert (await client.post(path,headers={'X-Adapter-Token':token},json=message.model_dump(mode='json'))).status_code==401
        public=f'/api/v1/tenants/{uuid4()}/rooms/{uuid4()}/commitments/{uuid4()}/complete'
        assert (await client.post(public,headers={**headers,'X-Adapter-Token':token},
            json={'expected_version':1,'idempotency_key':str(uuid4())})).status_code==503
    assert captured==[True,False]


@pytest.mark.anyio
async def test_worker_recovery_replays_saved_proposal_without_duplicate_task_or_audit(monkeypatch):
    repo=FakeChatRepository(); settings=config(repo)
    chat=CommitmentChatService(repo,CommitmentService(repo,clock=lambda:NOW))
    # This test isolates the legacy mapped transaction/recovery contract. The open
    # adapter and genuine mention boundary is exercised in test_open_commitments.
    monkeypatch.setattr(chat,'prepare_open',chat.prepare)
    monkeypatch.setattr(ai_service,'explicit_agent_mention',lambda *args:True)
    monkeypatch.setattr(ai_service,'CommitmentChatService',lambda storage:chat)
    calls=[]
    async def mocked_ai(settings,question,**kwargs):
        calls.append(kwargs)
        plan=ChatPlan.model_validate(kwargs['task_context'])
        p=proposal(repo,plan,'complete')
        return json.dumps({'action':'none','task_changes':[p.model_dump(mode='json')]})
    async def forbidden_store(*args,**kwargs): raise AssertionError('Lifecycle updates cannot create commitments')
    monkeypatch.setattr(ai_service,'ask_switch',mocked_ai)
    monkeypatch.setattr(ai_service,'store_ai_memory',forbidden_store)
    first=ai_service.AIService(); repo.fail_after_commit=True
    message=IncomingMessage(tenant_id=repo.scope.tenant_id,room_id='switch-room',message_id='recover-completion',
        sender='puppet-worker',body="I've sent the report.",timestamp_ms=int(NOW.timestamp()*1000),
        external_message_id='platform-source',workspace_id='platform-workspace',channel_id='platform-channel')
    receipt=await first.ingest(message,settings,MessageReceiptRepository(),adapter_verified=True)
    await first.queue.join()
    job=await first.journal.get(receipt.message_ref)
    assert job['state']=='failed' and job['decision'] and job['chat_plan']
    assert repo.record.task_status=='completed' and len(repo.events)==1 and not repo.memory['active']
    stored_source=next(iter(repo.sources.values()))
    assert (stored_source.external_message_id,stored_source.workspace_id,stored_source.channel_id)==(
        'platform-source','platform-workspace','platform-channel')
    event_id=repo.events[0].id
    await first.close()
    restarted=ai_service.AIService()
    await restarted.journal.retry(receipt.message_ref)
    await restarted._enqueue_durable(await restarted.journal.get(receipt.message_ref),settings)
    await restarted.queue.join()
    recovered=await restarted.journal.get(receipt.message_ref)
    assert recovered['state']=='done' and recovered['chat_plan'] is None
    assert json.loads(recovered['chat_outcome'])['status']=='applied'
    assert len(calls)==1 and len(repo.events)==1 and repo.events[0].id==event_id
    assert repo.record.version==2
    replay=await restarted.ingest(message,settings,MessageReceiptRepository(),adapter_verified=True)
    assert replay.duplicate and restarted.pending==0
    await restarted.close()


@pytest.mark.anyio
async def test_update_ambiguity_finishes_once_and_blocks_duplicate_extraction(monkeypatch):
    repo=FakeChatRepository(); settings=config(repo)
    chat=CommitmentChatService(repo,CommitmentService(repo,clock=lambda:NOW))
    monkeypatch.setattr(ai_service,'CommitmentChatService',lambda storage:chat)
    async def mocked_ai(*args,**kwargs):
        # Deliberately mistaken extraction rather than a lifecycle proposal.
        return '{"action":"store_only","commitments":[{"description":"Report completed"}]}'
    posted=[]
    async def no_store(*args,**kwargs): raise AssertionError('Duplicate extraction')
    async def post(*args,**kwargs): posted.append(kwargs)
    monkeypatch.setattr(ai_service,'ask_switch',mocked_ai)
    monkeypatch.setattr(ai_service,'store_ai_memory',no_store)
    monkeypatch.setattr(ai_service.SwitchAPI,'post_room_message',post)
    service=ai_service.AIService()
    message=IncomingMessage(tenant_id=repo.scope.tenant_id,room_id='switch-room',message_id='ambiguous-once',
        sender='puppet-worker',body='Done',timestamp_ms=int(NOW.timestamp()*1000))
    receipt=await service.ingest(message,settings.model_copy(update={'switch_reply_to_discord':True}),
        MessageReceiptRepository(),adapter_verified=True)
    await service.queue.join()
    job=await service.journal.get(receipt.message_ref)
    assert job['state']=='done' and job['attempts']==1 and json.loads(job['chat_outcome'])['status']=='unauthorized'
    assert not posted and not repo.events and repo.record.version==1
    assert await service.journal.recoverable(ai_service.ingestion_scope(settings),settings.allowed_switch_rooms)==[]
    await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize('zone,source_time,deadline,expression,proposed,expected',[
    ('Asia/Karachi','2026-11-05T23:30:00Z','2026-11-06T07:00:00Z','Monday','2026-11-09T07:00:00Z','applied'),
    ('America/New_York','2026-10-30T12:00:00Z','2026-10-30T05:30:00Z','Sunday','2026-11-01T05:30:00Z','ambiguous'),
    ('America/New_York','2026-03-06T12:00:00Z','2026-03-06T07:30:00Z','Sunday','2026-03-08T07:30:00Z','ambiguous'),
    ('UTC','2026-11-09T12:00:00Z','2026-11-09T12:00:00Z','Monday','2026-11-16T12:00:00Z','ambiguous'),
    ('UTC','2026-11-05T12:00:00Z',None,'Monday at 09:00','2026-11-09T09:00:00Z','applied'),
    ('UTC','2026-11-05T12:00:00Z',None,'someday','2026-11-09T12:00:00Z','ambiguous'),
])
async def test_relative_dates_use_original_instant_and_confirmed_timezone(zone,source_time,deadline,expression,proposed,expected):
    repo=FakeChatRepository(); repo.policy=repo.policy.model_copy(update={'timezone':zone})
    repo.record=repo.record.model_copy(update={'due_at':datetime.fromisoformat(deadline) if deadline else None})
    service,plan=await prepared(repo,f'Move my report deadline to {expression}.',occurred_at=source_time)
    result=await service.execute(plan,[proposal(repo,plan,'reschedule',due_at=proposed)],'ref')
    assert result.status==expected
    assert bool(repo.events)==(expected=='applied')


@pytest.mark.anyio
async def test_two_chat_preflights_cannot_overwrite_concurrent_change():
    repo=FakeChatRepository()
    chat,one=await prepared(repo,"I've sent the report.")
    _,two=await prepared(repo,'Cancel my report task.')
    original=repo.context; gate=asyncio.Event(); count=0
    async def barrier(*args,**kwargs):
        nonlocal count
        value=await original(*args,**kwargs)
        if not repo.lock.locked():
            count+=1
            if count==2: gate.set()
            await gate.wait()
        return value
    repo.context=barrier
    results=await asyncio.wait_for(asyncio.gather(chat.execute(one,[proposal(repo,one,'complete')],'one'),
        chat.execute(two,[proposal(repo,two,'cancel')],'two')),2)
    assert sorted(r.status for r in results)==['applied','conflict']
    assert repo.record.version==2 and len(repo.events)==1 and not repo.memory['active']


@pytest.mark.anyio
async def test_untrusted_adapter_and_unknown_timestamp_never_prepare_candidates():
    repo=FakeChatRepository(); chat=CommitmentChatService(repo)
    for overrides in [{'adapter_verified':False},{'timestamp_ms':None},{'durable':False}]:
        assert await chat.prepare(config(repo),metadata(repo,**overrides),'Done') is None
    assert repo.prepared==0


@pytest.mark.anyio
async def test_admin_can_complete_approved_task_but_not_pending_as_an_owner():
    repo=FakeChatRepository()
    repo.record=repo.record.model_copy(update={'approval_status':'approved','approval_at':NOW,'approval_by_switch_user_id':'admin'})
    chat,plan=await prepared(repo,"I've sent the report.",'admin')
    result=await chat.execute(plan,[proposal(repo,plan,'complete')],'ref')
    assert result.status=='applied' and repo.record.automated_acceptance_at is None


@pytest.mark.anyio
async def test_confirmed_identity_does_not_come_from_display_name_or_guessed_scope():
    repo=FakeChatRepository(); chat=CommitmentChatService(repo)
    meta=metadata(repo,'unknown'); meta['author_name']='Worker'
    assert await chat.prepare(config(repo),meta,"I've sent the report.") is None
    assert await chat.prepare(config(repo),metadata(repo,room_id='other-room'),"I've sent the report.") is None
    assert not repo.events


@pytest.mark.anyio
async def test_deadline_clearing_and_stable_mention_handoff():
    repo=FakeChatRepository(); chat,plan=await prepared(repo,'Clear my report deadline.')
    assert (await chat.execute(plan,[proposal(repo,plan,'reschedule',due_at=None)],'ref')).status=='applied'
    assert repo.record.due_at is None and 'unscheduled' in repo.memory['content']
    chat,plan=await prepared(repo,'<@platform-replacement> is taking over the report.','admin')
    assert (await chat.execute(plan,[proposal(repo,plan,'reassign',assignee_id='replacement')],'ref')).status=='applied'
    assert repo.record.owner_switch_user_id=='replacement'


def test_lifecycle_extraction_guard_preserves_questions_and_blocks_new_tasks():
    from app.services.commitment_chat_evidence import protect_extraction
    from app.models.ai import LLMDecision
    query=LLMDecision(action='query_only',search_query='report completed')
    assert protect_extraction(query,'Has Ali finished?',True)==query
    mixed=LLMDecision(action='store_and_query',search_query='budget',commitments=[{'description':'Report finished'}])
    guarded=protect_extraction(mixed,"I've delivered the report. What about budget?",True)
    assert guarded.action=='query_only' and not guarded.commitments
    tentative=LLMDecision(action='store_only',commitments=[{'description':'Finish tomorrow'}])
    assert protect_extraction(tentative,'I might finish tomorrow.',True).action=='none'


@pytest.mark.anyio
async def test_no_partial_execution_of_multiple_proposals():
    repo=FakeChatRepository(); chat,plan=await prepared(repo,"I've sent the report.")
    p=proposal(repo,plan,'complete')
    assert (await chat.execute(plan,[p,p],'ref')).status=='ambiguous'
    assert not repo.events


@pytest.mark.anyio
async def test_chat_transport_carries_backend_actor_source_and_original_sender(monkeypatch):
    from app.models.commitment_chat import ChatExecution
    from app.repositories.commitment_repository import SupabaseCommitmentRepository
    repo=FakeChatRepository(); chat,plan=await prepared(repo,"I've sent the report.")
    await chat.execute(plan,[proposal(repo,plan,'complete')],'ref')
    result=next(iter(repo.receipts.values()))[1]
    request=CompleteRequest(idempotency_key=result.event.idempotency_key,expected_version=1)
    execution=ChatExecution(source=plan.source,policy=plan.policy)
    settings=config(repo).model_copy(update={'supabase_url':'https://fixture.invalid','supabase_service_role_key':'fixture-key'})
    calls=[]
    async def transport(incoming):
        calls.append((incoming.url.path,json.loads(incoming.content)))
        return httpx.Response(200,json=plan.model_dump(mode='json') if 'prepare' in incoming.url.path else result.model_dump(mode='json'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        adapter=SupabaseCommitmentRepository(settings,client)
        meta=metadata(repo,switch_message_id=plan.source.switch_message_id)
        assert (await adapter.prepare_chat(repo.scope.tenant_id,meta,plan.source.body)).source.actor==WORKER
        assert (await adapter.apply_chat_action(execution,repo.record.id,Action.complete,request,NOW)).event==result.event
    assert calls[0][1]['p_source']['switch_author_id']=='puppet-worker'
    write=calls[1][1]
    assert calls[1][0].endswith('/rpc/apply_commitment_chat_action')
    assert write['p_actor_id']=='worker' and write['p_backend_actor']=='switch-ingestion-worker'
    assert write['p_chat_source_id']==str(plan.source.id) and write['p_expected_version']==1


@pytest.mark.anyio
async def test_disabling_feature_blocks_saved_proposal_during_recovery(monkeypatch):
    repo=FakeChatRepository(); settings=config(repo)
    chat=CommitmentChatService(repo,CommitmentService(repo,clock=lambda:NOW))
    monkeypatch.setattr(chat,'prepare_open',chat.prepare)
    monkeypatch.setattr(ai_service,'explicit_agent_mention',lambda *args:True)
    monkeypatch.setattr(ai_service,'CommitmentChatService',lambda storage:chat)
    async def mocked_ai(settings,question,**kwargs):
        plan=ChatPlan.model_validate(kwargs['task_context'])
        return json.dumps({'action':'none','task_changes':[proposal(repo,plan,'complete').model_dump(mode='json')]})
    monkeypatch.setattr(ai_service,'ask_switch',mocked_ai)
    repo.fail=True
    first=ai_service.AIService()
    message=IncomingMessage(tenant_id=repo.scope.tenant_id,room_id='switch-room',message_id='disable-after-failure',
        sender='puppet-worker',body="I've sent the report.",timestamp_ms=int(NOW.timestamp()*1000))
    receipt=await first.ingest(message,settings,MessageReceiptRepository(),adapter_verified=True)
    await first.queue.join(); await first.close()
    assert not repo.events
    restarted=ai_service.AIService()
    await restarted.journal.retry(receipt.message_ref)
    await restarted._enqueue_durable(await restarted.journal.get(receipt.message_ref),
        settings.model_copy(update={'automatic_commitments_enabled':False}))
    await restarted.queue.join()
    job=await restarted.journal.get(receipt.message_ref)
    assert job['state']=='done' and json.loads(job['chat_outcome'])['status']=='unauthorized' and not repo.events
    await restarted.close()
