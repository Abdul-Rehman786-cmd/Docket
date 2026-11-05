"""Mocked AI and atomic fake storage. No hosted DB, model or external messages."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL

import pytest
from pydantic import ValidationError
from app.core.chat_participation import explicit_agent_mention, participant_id
from app.models.commitment_chat import ChatSource, ChatPlan, ChatPolicy, TaskChangeProposal
from app.models.commitments import Actor, ActionResult, AuditEvent, CommitmentError
from app.models.messages import IncomingMessage
from app.repositories.message_receipts import MessageReceiptRepository
from app.services import ai_service
from app.services.commitment_chat_service import CommitmentChatService
from app.services.commitment_service import CommitmentService, reminder_state, snapshot, summary_text
from test_commitment_lifecycle import FakeRepository, NOW
from test_commitment_chat import config, metadata

@pytest.fixture
def anyio_backend(): return 'asyncio'

class OpenRepository(FakeRepository):
    def __init__(self):
        super().__init__()
        self.exists=False; self.sources={}; self.fail_after_commit=False
        self.policy=ChatPolicy(mode='open_mentions',timezone='UTC')
        self.pending_reminders=['pending']; self.creations=0

    async def prepare_chat(self,tenant_id,meta,body):
        if tenant_id!=self.scope.tenant_id or meta['room_id']!='switch-room': return None
        assert meta['policy_mode']=='open_mentions'
        actor=Actor(subject_id=participant_id(tenant_id,self.scope.room_id,meta['switch_author_id']))
        self.roles[actor.subject_id]='member'
        source=ChatSource(id=uuid5(NAMESPACE_URL,meta['switch_message_id']),scope=self.scope,
            switch_message_id=meta['switch_message_id'],switch_author_id=meta['switch_author_id'],actor=actor,
            body=body,occurred_at=meta['occurred_at'],thread_id=meta.get('thread_id'),
            registered_agent_name=meta.get('registered_agent_name'))
        if source.id in self.sources and self.sources[source.id]!=source: raise CommitmentError('idempotency_conflict',409)
        self.sources[source.id]=source
        return ChatPlan(source=source,role='member',policy=self.policy,members=[],
            candidates=[self.record] if self.exists and self.record.owner_switch_user_id==actor.subject_id and
                self.record.task_status not in ('completed','cancelled') else [])

    async def create_chat(self,execution,key,description,due_at,at):
        async with self.lock:
            source=execution.source
            content=(description,due_at,source.id)
            previous=self.receipts.get((source.actor.subject_id,key))
            if previous:
                if previous[0]!=content: raise CommitmentError('idempotency_conflict',409)
                return previous[1]
            record=self.record.model_copy(update={'description':description,'due_at':due_at,
                'owner_switch_user_id':source.actor.subject_id,'owner_name':None,
                'owner_accepted_at':at,'owner_acceptance_source_id':source.id})
            event=AuditEvent(id=uuid4(),sequence=1,tenant_id=self.scope.tenant_id,room_id=self.scope.room_id,
                commitment_id=record.id,actor_id=source.actor.subject_id,action='create',occurred_at=at,
                idempotency_key=key,expected_version=1,changed=True,before={},after=snapshot(record),
                backend_actor=execution.backend_actor,source_evidence_id=source.id,evidence=source.body)
            result=ActionResult(commitment=record,event=event,changed=True,reminder=reminder_state(record))
            if self.fail: raise RuntimeError('Simulated transaction failure')
            self.record=record; self.exists=True; self.creations+=1; self.events.append(event)
            self.memory={'content':summary_text(description,owner_id=record.owner_switch_user_id,due_at=due_at,
                owner_accepted_at=at),'active':True,'embedding':None}
            self.receipts[(source.actor.subject_id,key)]=(content,result)
            if self.fail_after_commit:
                self.fail_after_commit=False
                raise CommitmentError('commitment_storage_unavailable',503)
            return result

    async def apply_chat_action(self,execution,commitment_id,action,request,at):
        if self.sources.get(execution.source.id)!=execution.source: raise CommitmentError('forbidden_action',403)
        result=await self.apply_action(execution.source.scope,execution.source.actor,commitment_id,action,request,at,execution)
        self.pending_reminders=[]
        return result

def meta(repo, **changes):
    return metadata(repo,registered_agent_id='client',registered_agent_name='registered-helper',
        sender_is_bot=False,addressed=True,**changes)

def service(repo): return CommitmentChatService(repo,CommitmentService(repo,clock=lambda:NOW))

def proposal(plan,action,**changes):
    return TaskChangeProposal(action=action,source_message_id=plan.source.switch_message_id,
        evidence=plan.source.body,clarification_required=False,**changes)

async def create(repo):
    chat=service(repo)
    plan=await chat.prepare_open(config(repo),meta(repo),"@registered-helper I'll finish report by Friday at 09:00.")
    p=proposal(plan,'create',description='finish report',due_at='2026-11-06T09:00:00Z')
    return chat,plan,p,await chat.execute(plan,[p],'ref')

@pytest.mark.anyio
async def test_unknown_sender_creates_accepted_owned_task_and_replay_is_atomic():
    repo=OpenRepository(); original=repo.source.copy()
    chat,plan,p,result=await create(repo)
    assert result.status=='applied' and repo.creations==1 and len(repo.events)==1
    assert repo.record.owner_switch_user_id==plan.source.actor.subject_id
    assert repo.record.approval_status=='pending' and repo.record.approval_at is None
    assert repo.record.owner_accepted_at==NOW and reminder_state(repo.record).eligible
    assert 'explicit owner statement' in repo.memory['content'] and repo.memory['embedding'] is None
    assert repo.source==original and repo.events[0].evidence==plan.source.body
    assert (await chat.execute(plan,[p],'ref')).event_id==result.event_id
    assert repo.creations==1 and len(repo.events)==1

@pytest.mark.anyio
@pytest.mark.parametrize('action,body,fields,status',[
    ('start','I started my report.',{},'in_progress'),
    ('complete','I finished my report.',{},'completed'),
    ('cancel','Cancel my report task.',{},'cancelled'),
    ('reschedule','Move my report deadline to Monday.',{'due_at':'2026-11-09T09:00:00Z'},'not_started'),
    ('reschedule','Clear my report deadline.',{'due_at':None},'not_started'),
])
async def test_mentioned_owner_actions_synchronize_history_memory_and_reminders(action,body,fields,status):
    repo=OpenRepository(); await create(repo); chat=service(repo)
    plan=await chat.prepare_open(config(repo),meta(repo),'@registered-helper '+body)
    p=proposal(plan,action,commitment_id=repo.record.id,**fields)
    result=await chat.execute(plan,[p],'ref')
    assert result.status=='applied' and repo.record.task_status==status and repo.record.version==2
    assert len(repo.events)==2 and repo.events[-1].before['version']==1
    assert repo.memory['embedding'] is None and not repo.pending_reminders
    if status in ('completed','cancelled'): assert not reminder_state(repo.record).eligible and not repo.memory['active']
    if status=='completed': assert repo.record.completed_at==NOW

@pytest.mark.anyio
@pytest.mark.parametrize('changes,body',[
    ({},'I finished my report.'),
    ({},'@registered-helper-evil I finished my report.'),
    ({},'@someone I finished my report.'),
    ({'adapter_verified':False},'@registered-helper I finished my report.'),
    ({'registered_agent_id':'attacker'},'@registered-helper I finished my report.'),
    ({'sender_is_bot':None},'@registered-helper I finished my report.'),
    ({'sender_is_bot':True},'@registered-helper I finished my report.'),
    ({'mentioned_agent_ids':['someone']},'@registered-helper I finished my report.'),
    ({},'`@registered-helper I finished my report.`'),
])
async def test_mention_and_sender_spoofing_fail_closed(changes,body):
    repo=OpenRepository(); await create(repo); old=repo.record
    metadata=meta(repo); metadata.update(changes)
    assert await service(repo).prepare_open(config(repo),metadata,body) is None
    assert repo.record==old and len(repo.events)==1

@pytest.mark.anyio
async def test_other_sender_model_owner_spoof_and_ambiguous_task():
    repo=OpenRepository(); await create(repo); chat=service(repo)
    m=meta(repo); m['switch_author_id']='different-puppet'
    plan=await chat.prepare_open(config(repo),m,'@registered-helper I finished my report.')
    assert not plan.candidates
    assert (await chat.execute(plan,[proposal(plan,'complete',commitment_id=repo.record.id)],'ref')).status=='ambiguous'
    with pytest.raises(ValidationError): proposal(plan,'create',description='report',owner_id=repo.record.owner_switch_user_id)
    plan=await chat.prepare_open(config(repo),meta(repo),'@registered-helper Done')
    plan=plan.model_copy(update={'candidates':[repo.record,repo.record.model_copy(update={'id':uuid4()})]})
    assert (await chat.execute(plan,[proposal(plan,'complete',commitment_id=repo.record.id)],'ref')).status=='ambiguous'
    assert repo.record.version==1

@pytest.mark.anyio
@pytest.mark.parametrize('body',[
    "I'll finish report by Friday.","I'll finish report by someday.","I might finish report.",
    'Has Ali finished my report?', '"I finished my report."', 'Rehman is taking over my report.',
])
async def test_ambiguous_deadlines_and_unsupported_handoffs_leave_state_unchanged(body):
    repo=OpenRepository(); chat=service(repo)
    plan=await chat.prepare_open(config(repo),meta(repo),'@registered-helper '+body)
    if plan is None:
        assert not repo.exists
        return
    p=proposal(plan,'create',description='finish report')
    assert (await chat.execute(plan,[p],'ref')).status=='ambiguous' and not repo.exists

@pytest.mark.anyio
async def test_own_tasks_read_is_scoped_and_no_changes():
    repo=OpenRepository(); await create(repo); chat=service(repo)
    plan=await chat.prepare_open(config(repo),meta(repo),'@registered-helper What are my tasks?')
    result=await chat.execute(plan,[proposal(plan,'list')],'ref')
    assert result.status=='no_change' and 'finish report' in result.reply and len(repo.events)==1
    assert participant_id(repo.scope.tenant_id,uuid4(),'sender')!=participant_id(repo.scope.tenant_id,repo.scope.room_id,'sender')
    assert participant_id(repo.scope.tenant_id,repo.scope.room_id,'sender','discord')!=participant_id(repo.scope.tenant_id,repo.scope.room_id,'sender','slack')

@pytest.mark.anyio
async def test_legacy_internal_creation_replays_after_lost_ack():
    # The chat worker now uses atomic extraction for simple creation; preserve the
    # old lifecycle creation contract for other internal integrations.
    repo=OpenRepository();chat=service(repo)
    plan=await chat.prepare_open(config(repo),meta(repo),"@registered-helper I'll finish report.")
    change=proposal(plan,'create',description='finish report');repo.fail_after_commit=True
    with pytest.raises(CommitmentError,match='commitment_storage_unavailable'):
        await chat.execute(plan,[change],'ref')
    assert repo.creations==1 and len(repo.events)==1
    replay=await service(repo).execute(plan,[change],'ref')
    assert replay.status=='applied' and repo.creations==1 and len(repo.events)==1

@pytest.mark.anyio
async def test_creation_rollback_concurrent_replays_and_stale_owner_update():
    repo=OpenRepository(); chat=service(repo)
    plan=await chat.prepare_open(config(repo),meta(repo),"@registered-helper I'll finish report.")
    p=proposal(plan,'create',description='finish report')
    repo.fail=True
    with pytest.raises(RuntimeError): await chat.execute(plan,[p],'ref')
    assert not repo.exists and not repo.events and not repo.receipts
    repo.fail=False
    one,two=await asyncio.gather(chat.execute(plan,[p],'ref'),chat.execute(plan,[p],'ref'))
    assert one.event_id==two.event_id and repo.creations==1 and len(repo.events)==1
    first=await chat.prepare_open(config(repo),meta(repo),'@registered-helper I started my report.')
    second=await chat.prepare_open(config(repo),meta(repo),'@registered-helper Cancel my report task.')
    assert (await chat.execute(first,[proposal(first,'start',commitment_id=repo.record.id)],'ref')).status=='applied'
    assert (await chat.execute(second,[proposal(second,'cancel',commitment_id=repo.record.id)],'ref')).status=='conflict'
    assert repo.record.task_status=='in_progress' and len(repo.events)==2

@pytest.mark.anyio
async def test_unmentioned_and_spoofed_extraction_preserves_facts_without_creating_tasks(monkeypatch):
    repo=OpenRepository(); settings=config(repo); calls=[]
    monkeypatch.setattr(ai_service,'CommitmentChatService',lambda storage:service(repo))
    async def mocked_ai(*args,**kwargs):
        return json.dumps({'action':'store_only','commitments':[{'description':'finish report','owner_name':'Victim'}],
            'memories':[{'content':'The release is Friday'}]})
    async def store(settings,**kwargs):
        decision=kwargs['decision']; assert not decision.commitments
        assert decision.memories[0].content=='The release is Friday'
        calls.append(decision)
        return {'messages':1,'commitments':0,'decisions':0,'memories':1}
    monkeypatch.setattr(ai_service,'ask_switch',mocked_ai); monkeypatch.setattr(ai_service,'store_ai_memory',store)
    worker=ai_service.AIService()
    message=IncomingMessage(tenant_id=repo.scope.tenant_id,room_id='switch-room',message_id='unmentioned-promise',
        sender='new-puppet',body="I'll finish report. The release is Friday.",timestamp_ms=int(NOW.timestamp()*1000),
        registered_agent_id='client',registered_agent_name='registered-helper',sender_is_bot=False)
    await worker.ingest(message,settings,MessageReceiptRepository(),adapter_verified=True)
    await worker.queue.join()
    assert len(calls)==1 and not repo.exists and not repo.events
    await worker.close()

@pytest.mark.anyio
async def test_allowed_room_tenant_and_backend_candidate_validation():
    repo=OpenRepository(); chat=service(repo); body="@registered-helper I'll finish report."
    m=meta(repo);m['room_id']='different-room'
    assert await chat.prepare_open(config(repo),m,body) is None
    assert await chat.prepare_open(config(repo).model_copy(update={'switch_tenant_id':uuid4()}),meta(repo),body) is None
    # The backend refuses a repository result carrying another owner's candidate.
    await create(repo)
    original=repo.prepare_chat
    async def invalid(*args):
        plan=await original(*args)
        return plan.model_copy(update={'candidates':[repo.record.model_copy(update={'owner_switch_user_id':'spoofed-owner'})]})
    repo.prepare_chat=invalid
    with pytest.raises(CommitmentError,match='invalid_storage_result'):
        await chat.prepare_open(config(repo),meta(repo),'@registered-helper What are my tasks?')

def test_planner_accepts_explicit_owner_evidence_without_manager_approval():
    from test_reminders import sample
    from app.services.reminder_planner import plan_reminder
    work=sample()
    record=work.commitment.model_copy(update={'approval_status':'pending','approval_at':None,
        'approval_by_switch_user_id':None,'owner_accepted_at':NOW,'owner_acceptance_source_id':uuid4()})
    work=work.model_copy(update={'commitment':record})
    assert plan_reminder(work,NOW) is not None
    for changes in ({'owner_accepted_at':None,'owner_acceptance_source_id':None},
        {'task_status':'completed','completed_at':NOW},{'task_status':'cancelled'},
        {'approval_status':'rejected','approval_at':NOW,'approval_by_switch_user_id':'reviewer'}):
        assert plan_reminder(work.model_copy(update={'commitment':record.model_copy(update=changes)}),NOW) is None

@pytest.mark.anyio
@pytest.mark.parametrize('trusted_sender',[True,False])
async def test_mocked_task_list_cannot_fall_through_to_room_wide_rag(monkeypatch,trusted_sender):
    repo=OpenRepository(); await create(repo); settings=config(repo)
    monkeypatch.setattr(ai_service,'CommitmentChatService',lambda storage:service(repo))
    async def mocked_ai(*args,**kwargs):
        return '{"action":"query_only","search_query":"all tasks in the room"}'
    async def forbidden_query(*args,**kwargs): raise AssertionError('Owner read must not use room-wide RAG')
    monkeypatch.setattr(ai_service,'ask_switch',mocked_ai)
    monkeypatch.setattr(ai_service,'query_team_memory',forbidden_query)
    worker=ai_service.AIService()
    message=IncomingMessage(tenant_id=repo.scope.tenant_id,room_id='switch-room',message_id='owned-list',
        sender='puppet-worker',body='@registered-helper What are my tasks?',timestamp_ms=int(NOW.timestamp()*1000),
        registered_agent_id='client',registered_agent_name='registered-helper',addressed=True,
        sender_is_bot=False if trusted_sender else None)
    receipt=await worker.ingest(message,settings,MessageReceiptRepository(),adapter_verified=True)
    await worker.queue.join(); job=await worker.journal.get(receipt.message_ref)
    assert job['state']=='done'
    outcome=json.loads(job['chat_outcome'])
    assert outcome['status']==('no_change' if trusted_sender else 'unauthorized')
    if trusted_sender: assert 'finish report' in outcome['reply']
    assert len(repo.events)==1
    await worker.close()
