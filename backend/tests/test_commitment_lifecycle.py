"""Domain tests use an atomic in-memory fake, not hosted persistence/authentication."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.core.commitment_identity import resolve_commitment_actor
from app.controllers.commitments import service as service_dependency
from app.main import app
from app.models.commitments import (
    Action, ActionContext, ActionResult, Actor, AuditEvent, CommitmentError, CommitmentState,
    ApproveRequest, RejectRequest, StartRequest, CompleteRequest, CancelRequest,
    ReassignRequest, RescheduleRequest, Filters, Scope,
)
from app.services.commitment_service import CommitmentService, decide, reminder_state, request_content, snapshot, summary_text

NOW = datetime(2026,11,5,12,tzinfo=timezone.utc)
MODELS = dict(approve=ApproveRequest,reject=RejectRequest,start=StartRequest,complete=CompleteRequest,
              cancel=CancelRequest,reassign=ReassignRequest,reschedule=RescheduleRequest)
REVIEWER, WORKER = Actor(subject_id='reviewer'), Actor(subject_id='worker')


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class FakeRepository:
    def __init__(self):
        self.scope=Scope(tenant_id=uuid4(),room_id=uuid4())
        self.record=CommitmentState(id=uuid4(),tenant_id=self.scope.tenant_id,room_id=self.scope.room_id,
            message_id=uuid4(),extraction_key='commitment-one',description='Deliver report',
            owner_name='Original name',owner_switch_user_id='worker',due_at=NOW,created_at=NOW,updated_at=NOW)
        self.roles={'reviewer':'approver','worker':'member','replacement':'member'}
        self.receipts={}; self.events=[]; self.memory={'content':'Old summary','embedding':'old-vector','active':True}
        self.source={'body':'Original evidence','message_id':self.record.message_id,'link':'source-link'}
        self.lock=asyncio.Lock(); self.fail=False

    def check(self,scope,actor,commitment_id=None):
        if scope != self.scope or actor.subject_id not in self.roles or (commitment_id is not None and commitment_id != self.record.id):
            raise CommitmentError('not_found',404)

    async def context(self,scope,actor,commitment_id,request=None):
        self.check(scope,actor,commitment_id)
        previous=self.receipts.get((actor.subject_id,request.idempotency_key)) if request else None
        return ActionContext(commitment=self.record,role=self.roles[actor.subject_id],
            assignee_valid=getattr(request,'assignee_id',None) in self.roles,
            previous_request=previous[0] if previous else None,previous_result=previous[1] if previous else None)

    async def list(self,scope,actor,filters):
        self.check(scope,actor)
        r=self.record
        match=(not filters.approval_status or filters.approval_status==r.approval_status) and (
            not filters.task_status or filters.task_status==r.task_status) and (
            not filters.assignee_id or filters.assignee_id==r.owner_switch_user_id)
        return ([r] if match and filters.offset==0 else []),None

    async def history(self,scope,actor,commitment_id,limit,offset):
        self.check(scope,actor,commitment_id)
        return self.events[offset:offset+limit],offset+limit if len(self.events)>offset+limit else None

    async def apply_action(self,scope,actor,commitment_id,action,request,at,chat=None):
        async with self.lock:
            context=await self.context(scope,actor,commitment_id,request)
            decision=decide(context,actor,action,request,at,chat)
            if isinstance(decision,ActionResult): return decision
            record,changed=decision
            event=AuditEvent(id=uuid4(),sequence=len(self.events)+1,tenant_id=scope.tenant_id,room_id=scope.room_id,
                commitment_id=record.id,actor_id=actor.subject_id,action=action,occurred_at=at,
                idempotency_key=request.idempotency_key,expected_version=request.expected_version,changed=changed,
                before=snapshot(self.record),after=snapshot(record),reason=request.reason,
                backend_actor=chat.backend_actor if chat else None,source_evidence_id=chat.source.id if chat else None,
                evidence=chat.source.body if chat else None,outcome='applied' if changed else 'no_change')
            memory=deepcopy(self.memory)
            content=summary_text(record.description,record.owner_name,record.owner_switch_user_id,record.due_at,
                                 record.approval_status,record.task_status,record.automated_acceptance_at)
            if content!=memory['content']: memory.update(content=content,embedding=None,embedding_status='pending')
            memory['active']=record.approval_status!='rejected' and record.task_status not in ('completed','cancelled')
            result=ActionResult(commitment=record,reminder=reminder_state(record),event=event,changed=changed)
            if self.fail: raise RuntimeError('Simulated transaction failure')
            self.record,self.memory=record,memory
            self.events.append(event)
            self.receipts[(actor.subject_id,request.idempotency_key)]=(request_content(record.id,action,request,chat),result)
            return result


@pytest.fixture
def module():
    repo=FakeRepository()
    return repo,CommitmentService(repo,clock=lambda:NOW)


async def act(module,action,actor=REVIEWER,**fields):
    repo,service=module
    version=fields.pop('expected_version',repo.record.version)
    request=MODELS[action](idempotency_key=fields.pop('idempotency_key',uuid4()),expected_version=version,**fields)
    return await service.perform(repo.scope,actor,repo.record.id,Action(action),request)


@pytest.mark.anyio
async def test_approve_start_complete_audit_and_evidence(module):
    repo,service=module; original=deepcopy(repo.source)
    assert not (await service.get(repo.scope,WORKER,repo.record.id)).reminder.eligible
    approved=await act(module,'approve')
    assert approved.commitment.approval_at==NOW and approved.commitment.approval_by_switch_user_id=='reviewer'
    assert approved.reminder.eligible
    await act(module,'start',WORKER)
    done=await act(module,'complete',WORKER)
    assert done.commitment.completed_at==NOW and done.commitment.task_status=='completed'
    assert done.reminder.reason=='completed' and not repo.memory['active'] and repo.memory['embedding'] is None
    assert 'Task: completed' in repo.memory['content'] and 'Approval: approved' in repo.memory['content']
    assert repo.source==original
    history=await service.history(repo.scope,WORKER,repo.record.id)
    assert [e.action.value for e in history.items]==['approve','start','complete']
    assert done.event.before['task_status']=='in_progress' and done.event.after['completed_at'] is not None
    assert done.event.actor_id=='worker' and done.event.after['version']==4
    repeat=await act(module,'complete',WORKER)
    assert not repeat.changed and repeat.commitment.completed_at==NOW


@pytest.mark.anyio
@pytest.mark.parametrize('action',['start','complete'])
async def test_pending_and_rejected_cannot_be_worked(module,action):
    with pytest.raises(CommitmentError,match='invalid_transition'): await act(module,action,WORKER)
    await act(module,'reject',reason='Not agreed')
    with pytest.raises(CommitmentError,match='invalid_transition'): await act(module,action,WORKER)
    with pytest.raises(CommitmentError,match='invalid_transition'): await act(module,'approve')
    assert not module[0].memory['active'] and reminder_state(module[0].record).reason=='rejected'


@pytest.mark.anyio
async def test_reassignment_and_rescheduling(module):
    repo,_=module
    result=await act(module,'reschedule',due_at='2026-12-01T17:00:00+05:00')
    assert result.commitment.due_at==datetime(2026,12,1,12,tzinfo=timezone.utc)
    await act(module,'reassign',assignee_id='replacement')
    assert repo.record.owner_switch_user_id=='replacement' and repo.record.owner_name is None
    await act(module,'approve')
    with pytest.raises(CommitmentError,match='forbidden_action'): await act(module,'complete',WORKER)
    await act(module,'reschedule',due_at=None)
    assert repo.record.due_at is None and reminder_state(repo.record).reason=='no_deadline'
    assert 'unscheduled' in repo.memory['content'] and 'Assignee: replacement' in repo.memory['content']
    await act(module,'complete',Actor(subject_id='replacement'))


@pytest.mark.anyio
@pytest.mark.parametrize('terminal',['complete','cancel'])
async def test_terminal_records_cannot_change(module,terminal):
    if terminal=='complete': await act(module,'approve')
    result=await act(module,terminal,WORKER if terminal=='complete' else REVIEWER,reason='Finished')
    assert result.commitment.completed_at==NOW if terminal=='complete' else result.commitment.cancelled_at==NOW
    assert not result.reminder.eligible
    for action,fields in [('reassign',{'assignee_id':'replacement'}),('reschedule',{'due_at':None}),('approve',{}),('reject',{})]:
        with pytest.raises(CommitmentError,match='invalid_transition'): await act(module,action,**fields)


@pytest.mark.anyio
async def test_permission_scope_and_assignees(module):
    repo,service=module
    for action,fields in [('approve',{}),('reject',{}),('cancel',{}),('reassign',{'assignee_id':'replacement'}),('reschedule',{'due_at':None})]:
        with pytest.raises(CommitmentError,match='forbidden_action'): await act(module,action,WORKER,**fields)
    for scope in [Scope(tenant_id=uuid4(),room_id=repo.scope.room_id),Scope(tenant_id=repo.scope.tenant_id,room_id=uuid4())]:
        with pytest.raises(CommitmentError,match='not_found'): await service.get(scope,WORKER,repo.record.id)
    with pytest.raises(CommitmentError,match='not_found'): await service.history(repo.scope,Actor(subject_id='outsider'),repo.record.id)
    with pytest.raises(CommitmentError,match='invalid_assignee'): await act(module,'reassign',assignee_id='DisplayName')
    await act(module,'approve')
    with pytest.raises(CommitmentError,match='forbidden_action'): await act(module,'complete',REVIEWER)


@pytest.mark.anyio
async def test_idempotency_stale_concurrency_and_current_permissions(module):
    repo,service=module; key=uuid4()
    result=await act(module,'approve',idempotency_key=key)
    replay=await act(module,'approve',idempotency_key=key,expected_version=1)
    assert replay==result and len(repo.events)==1
    with pytest.raises(CommitmentError,match='idempotency_conflict'):
        await act(module,'approve',idempotency_key=key,expected_version=1,reason='Different')
    with pytest.raises(CommitmentError,match='stale_version'): await act(module,'approve',expected_version=1)
    noop=await act(module,'approve')
    assert not noop.changed and repo.record.version==2
    outcomes=await asyncio.gather(act(module,'reschedule',expected_version=2,due_at=None),
        act(module,'reassign',expected_version=2,assignee_id='replacement'),return_exceptions=True)
    assert sum(isinstance(x,ActionResult) for x in outcomes)==1
    assert any(isinstance(x,CommitmentError) and x.code=='stale_version' for x in outcomes)
    repo.roles.pop('reviewer')
    with pytest.raises(CommitmentError,match='not_found'):
        await act(module,'approve',idempotency_key=key,expected_version=1)


@pytest.mark.anyio
async def test_transaction_failure_preserves_all_state(module):
    repo,_=module; before=deepcopy((repo.record,repo.memory,repo.source,repo.events,repo.receipts)); repo.fail=True
    with pytest.raises(RuntimeError): await act(module,'cancel')
    assert (repo.record,repo.memory,repo.source,repo.events,repo.receipts)==before


@pytest.mark.anyio
async def test_racing_preflights_are_revalidated_inside_transaction(module):
    repo,_=module
    original_context=repo.context
    gate=asyncio.Event()
    arrivals=0
    async def racing_context(*args,**kwargs):
        nonlocal arrivals
        context=await original_context(*args,**kwargs)
        if not repo.lock.locked():
            arrivals+=1
            if arrivals==2: gate.set()
            await gate.wait()
        return context
    repo.context=racing_context
    results=await asyncio.wait_for(asyncio.gather(
        act(module,'approve',expected_version=1),act(module,'cancel',expected_version=1),
        return_exceptions=True),timeout=2)
    assert arrivals==2
    assert sum(isinstance(item,ActionResult) for item in results)==1
    assert sum(isinstance(item,CommitmentError) and item.code=='stale_version' for item in results)==1
    assert len(repo.events)==len(repo.receipts)==1 and repo.record.version==2


def test_models_reject_naive_omitted_deadlines_and_untrusted_actor():
    base=dict(idempotency_key=uuid4(),expected_version=1)
    for fields in [{},{'due_at':'2026-12-01T12:00:00'},{'due_at':1234}]:
        with pytest.raises(ValidationError): RescheduleRequest(**base,**fields)
    assert RescheduleRequest(**base,due_at=None).due_at is None
    for fields in [{'actor_id':'admin'},{'role':'admin'},{'tenant_id':str(uuid4())}]:
        with pytest.raises(ValidationError): ApproveRequest(**base,**fields)
    with pytest.raises(ValidationError): Filters(limit=101)
    with pytest.raises(ValidationError): Filters(due_from=NOW,due_to='2020-01-01T00:00:00Z')


@pytest.mark.anyio
async def test_http_auth_boundary_and_authenticated_contract(module):
    repo,service=module
    path=f'/api/v1/tenants/{repo.scope.tenant_id}/rooms/{repo.scope.room_id}/commitments'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get(path,headers={'Authorization':'Bearer test-ingest-token','X-Actor-Id':'reviewer'})
        assert response.status_code==503
        app.dependency_overrides[resolve_commitment_actor]=lambda: REVIEWER
        app.dependency_overrides[service_dependency]=lambda: service
        try:
            body=dict(idempotency_key=str(uuid4()),expected_version=1)
            response=await client.post(path+f'/{repo.record.id}/approve',json=body)
            assert response.status_code==200 and response.json()['commitment']['version']==2
            assert response.headers['cache-control']=='no-store'
            assert (await client.get(path+'?limit=101')).status_code==422
            assert (await client.get(path+'?approval_status=approved')).json()['items']
            assert (await client.get(path+f'/{repo.record.id}/history')).json()['items'][0]['actor_id']=='reviewer'
            assert (await client.post(path+f'/{repo.record.id}/cancel',json={**body,'actor_id':'worker'})).status_code==422
            assert (await client.post(path+f'/{repo.record.id}/cancel',json=body)).status_code==409
            assert (await client.post(path+f'/{repo.record.id}/reschedule',json={**body,'expected_version':2})).status_code==422
            app.dependency_overrides[resolve_commitment_actor]=lambda: Actor(subject_id='outsider')
            assert (await client.get(path)).status_code==404
        finally: app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_reason_uses_existing_credential_redaction(module,monkeypatch):
    monkeypatch.setenv('ADMIN_API_TOKEN','synthetic-fixture-secret')
    from app.core.config import get_settings
    get_settings.cache_clear()
    result=await act(module,'reject',reason='Reason synthetic-fixture-secret')
    assert 'synthetic-fixture-secret' not in result.model_dump_json()
    assert 'REDACTED' in result.event.reason.upper()


def test_extraction_summary_keeps_pending_status_and_identity():
    from app.core.config import get_settings
    from app.models.ai import LLMDecision
    from app.services.extraction_service import storage_items
    decision=LLMDecision(action='store_only',commitments=[{'description':'Deliver report','owner_name':'Worker',
        'due_at':'2026-12-01T17:00:00+05:00'}])
    items=storage_items(decision,get_settings())
    assert items['commitments'][0]['extraction_key']==items['memories'][0]['extraction_key']
    assert 'Approval: pending\nTask: not_started' in items['memories'][0]['content']
    assert '2026-12-01T12:00:00.000000Z' in items['memories'][0]['content']
    assert 'owner_switch_user_id' not in items['commitments'][0]


@pytest.mark.anyio
async def test_http_action_routes_use_specific_models_and_actor(module):
    repo,service=module
    path=f'/api/v1/tenants/{repo.scope.tenant_id}/rooms/{repo.scope.room_id}/commitments/{repo.record.id}'
    actor=REVIEWER
    app.dependency_overrides[resolve_commitment_actor]=lambda: actor
    app.dependency_overrides[service_dependency]=lambda: service
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            for action,fields in [('reschedule',{'due_at':None}),('reassign',{'assignee_id':'replacement'}),('approve',{}),('start',{}),('complete',{})]:
                if action in ('start','complete'): actor=Actor(subject_id='replacement')
                response=await client.post(path+'/'+action,json=dict(idempotency_key=str(uuid4()),
                    expected_version=repo.record.version,**fields))
                assert response.status_code==200
                assert response.json()['event']['action']==action
            assert (await client.patch(path,json={'task_status':'not_started'})).status_code==405
    finally: app.dependency_overrides.clear()
