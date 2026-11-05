"""Mocked PostgREST transport; does not exercise hosted Supabase."""
import json
from uuid import uuid4

import httpx
import pytest

from app.core.config import get_settings
from app.models.commitments import Action, ActionContext, CommitmentError, ApproveRequest, Filters
from app.repositories.commitment_repository import SupabaseCommitmentRepository
from test_commitment_lifecycle import FakeRepository, REVIEWER, NOW, CommitmentService


@pytest.fixture
def anyio_backend(): return 'asyncio'


def settings(monkeypatch):
    monkeypatch.setenv('SUPABASE_URL','https://fixture.supabase.invalid')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY','synthetic-service-credential')
    get_settings.cache_clear()
    return get_settings()


@pytest.mark.anyio
async def test_adapter_sends_one_atomic_write_with_trusted_scope(monkeypatch):
    fake=FakeRepository(); request=ApproveRequest(idempotency_key=uuid4(),expected_version=1)
    result=await CommitmentService(fake,clock=lambda:NOW).perform(fake.scope,REVIEWER,fake.record.id,Action.approve,request)
    requests=[]
    async def transport(incoming):
        requests.append(incoming)
        assert incoming.url.query==b''
        assert incoming.headers['apikey']=='synthetic-service-credential'
        return httpx.Response(200,json=result.model_dump(mode='json'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        repo=SupabaseCommitmentRepository(settings(monkeypatch),client)
        actual=await repo.apply_action(fake.scope,REVIEWER,fake.record.id,Action.approve,request,NOW)
    assert actual==result and len(requests)==1
    assert requests[0].url.path=='/rest/v1/rpc/apply_commitment_action'
    payload=json.loads(requests[0].content)
    assert payload==dict(p_tenant_id=str(fake.scope.tenant_id),p_room_id=str(fake.scope.room_id),p_actor_id='reviewer',
        p_commitment_id=str(fake.record.id),p_action='approve',p_expected_version=1,
        p_idempotency_key=str(request.idempotency_key),p_payload={'reason':None})


@pytest.mark.anyio
@pytest.mark.parametrize('response,expected,status',[
    ({'message':'stale_version','code':'PT409'},'stale_version',409),
    ({'message':'idempotency_conflict','code':'PT409'},'idempotency_conflict',409),
    ({'message':'sensitive remote diagnostic','code':'PGRST202'},'commitment_storage_unavailable',503),
    ({'message':'stale_version','code':'unexpected'},'commitment_storage_unavailable',503),
])
async def test_remote_errors_are_fixed_categories(monkeypatch,response,expected,status):
    fake=FakeRepository()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(400,json=response))) as client:
        repo=SupabaseCommitmentRepository(settings(monkeypatch),client)
        with pytest.raises(CommitmentError) as raised: await repo.context(fake.scope,REVIEWER,fake.record.id)
    assert raised.value.code==expected and raised.value.status==status
    assert 'sensitive' not in str(raised.value)


@pytest.mark.anyio
async def test_read_contract_and_mis_scoped_response_fail_closed(monkeypatch):
    fake=FakeRepository(); context=await fake.context(fake.scope,REVIEWER,fake.record.id)
    calls=[]
    async def transport(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200,json=context.model_dump(mode='json'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        repo=SupabaseCommitmentRepository(settings(monkeypatch),client)
        assert (await repo.context(fake.scope,REVIEWER,fake.record.id)).role=='approver'
        context=context.model_copy(update={'commitment':fake.record.model_copy(update={'room_id':uuid4()})})
        with pytest.raises(CommitmentError,match='invalid_storage_result'):
            await repo.context(fake.scope,REVIEWER,fake.record.id)
    assert calls[0]['p_actor_id']=='reviewer' and calls[0]['p_mode']=='context'


@pytest.mark.anyio
async def test_unconfigured_storage_fails_without_network():
    fake=FakeRepository()
    repo=SupabaseCommitmentRepository(get_settings())
    with pytest.raises(CommitmentError,match='commitment_storage_unavailable'):
        await repo.context(fake.scope,REVIEWER,fake.record.id)
