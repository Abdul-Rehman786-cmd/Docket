"""Mocked Switch/PostgREST only. No real external delivery or hosted mutation."""
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4
import httpx
import pytest
from app.core.config import Settings
from app.models.reminders import NotificationFailure,DispatchContext
from app.models.commitments import CommitmentError
from app.services.switch_api import SwitchAPI
from app.services.switch_notifications import SwitchNotificationSink
from app.services.notification_dispatcher import dispatch_when,reminder_template
from app.repositories.reminder_repository import SupabaseReminderRepository
from app.commitment_deployment_check import inspect
from test_reminders import Clock,stack,NOW


@pytest.fixture
def anyio_backend():return 'asyncio'


def settings():
    return Settings(_env_file=None,reminders_enabled=True,reminder_delivery_enabled=True,
        switch_bridge_url='https://switch.invalid',switch_requester_agent_id='fixture-client',
        switch_requester_api_key='fixture-client-secret',switch_agent_id='fixture-ai',switch_ai_room_id='private-ai',
        switch_room_ids='room')


@pytest.mark.anyio
@pytest.mark.parametrize('status,body,kind',[
    (200,{'result':{'event_id':'fixture-event'}},None),
    (200,{'result':{'status':'ok'}},'ambiguous'),
    (429,{},'transient'),(403,{},'permanent'),(404,{},'permanent'),(503,{},'ambiguous'),(302,{},'ambiguous'),
])
async def test_switch_one_request_and_explicit_failure_classes(monkeypatch,status,body,kind):
    calls=[]
    async def transport(request):
        calls.append(request)
        assert request.url.path=='/agents/fixture-client/ops/post_message'
        assert request.headers['Authorization']=='Bearer fixture-client-secret'
        return httpx.Response(status,json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        @asynccontextmanager
        async def http(self):yield client
        monkeypatch.setattr(SwitchAPI,'http',http)
        sink=SwitchNotificationSink(settings())
        if kind:
            with pytest.raises(NotificationFailure) as raised:await sink.send('room','Fixture reminder','thread')
            assert raised.value.kind==kind
        else:assert (await sink.send('room','Fixture reminder','thread')).message_id=='fixture-event'
    assert len(calls)==1 # No unsafe direct-message fallback after a possible accepted post.


@pytest.mark.anyio
@pytest.mark.parametrize('error,kind',[(httpx.ConnectError('untrusted diagnostic'),'transient'),
    (httpx.ReadTimeout('untrusted diagnostic'),'ambiguous'),(httpx.WriteError('untrusted diagnostic'),'ambiguous')])
async def test_transport_errors_never_expose_remote_contents(monkeypatch,error,kind):
    async def transport(request):raise error
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        @asynccontextmanager
        async def http(self):yield client
        monkeypatch.setattr(SwitchAPI,'http',http)
        with pytest.raises(NotificationFailure) as raised:await SwitchNotificationSink(settings()).send('room','Fixture')
    assert raised.value.kind==kind and 'untrusted' not in str(raised.value)


@pytest.mark.anyio
async def test_delivery_flags_and_scope_fail_closed():
    for s,room in [(settings().model_copy(update={'reminder_delivery_enabled':False}),'room'),(settings(),'other-room'),
        (settings(),'private-ai'),(settings().model_copy(update={'switch_requester_agent_id':'fixture-ai'}),'room')]:
        with pytest.raises(NotificationFailure) as raised:await SwitchNotificationSink(s).send(room,'Fixture')
        assert raised.value.kind=='permanent'


@pytest.mark.anyio
async def test_context_scope_and_policy_checks_before_delivery():
    clock=Clock();repo,scheduler,_,_,_=stack(clock)
    await scheduler.tick();delivery=(await repo.claim(1))[0];work=repo.snapshot()
    ctx=DispatchContext(delivery=delivery,work=work)
    assert dispatch_when(ctx,NOW)==NOW
    bad=work.model_copy(update={'commitment':work.commitment.model_copy(update={'tenant_id':uuid4()})})
    assert dispatch_when(ctx.model_copy(update={'work':bad}),NOW) is None
    bad=work.model_copy(update={'commitment':work.commitment.model_copy(update={'room_id':uuid4()})})
    assert dispatch_when(ctx.model_copy(update={'work':bad}),NOW) is None
    repo.preferences.change(timezone=None)
    assert dispatch_when(ctx.model_copy(update={'work':repo.snapshot()}),NOW) is None


@pytest.mark.anyio
async def test_templates_are_source_backed_and_do_not_fabricate_mentions():
    clock=Clock();repo,scheduler,_,_,_=stack(clock)
    repo.commitments.record=repo.commitments.record.model_copy(update={'description':'Ask @everyone and <@123> to deliver report'})
    await scheduler.tick();d=(await repo.claim(1))[0]
    text=reminder_template(DispatchContext(delivery=d,work=repo.snapshot()),settings())
    assert '@' not in text and str(repo.commitments.record.message_id) in text
    assert '[reminder:' in text and 'Deadline (UTC)' in text


@pytest.mark.anyio
async def test_repository_claim_scope_and_lost_acknowledgement(monkeypatch):
    clock=Clock();fake,scheduler,_,_,_=stack(clock)
    await scheduler.tick();d=(await fake.claim(1))[0]
    s=settings().model_copy(update={'switch_tenant_id':d.intention.tenant_id,'supabase_url':'https://fixture.invalid','supabase_service_role_key':'fixture-key'})
    calls=[]
    async def transport(request):
        import json
        payload=json.loads(request.content);calls.append(payload)
        return httpx.Response(200,json=[d.model_dump(mode='json')] if 'claim_reminder' in request.url.path else False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        repo=SupabaseReminderRepository(s,client)
        assert (await repo.claim(1))[0].id==d.id
        with pytest.raises(CommitmentError,match='reminder_acknowledgement_not_saved'):
            await repo.finish(d,'sent',message_id='fixture-message')
    assert calls[0]['p_tenant_id']==str(d.intention.tenant_id) and calls[0]['p_switch_room_ids']==['room']


@pytest.mark.anyio
async def test_hosted_probe_is_read_only_and_returns_no_records():
    s=settings().model_copy(update={'supabase_url':'https://fixture.invalid','supabase_service_role_key':'fixture-key'})
    calls=[]
    async def transport(request):
        calls.append(request)
        assert request.method=='GET' and request.url.query==b''
        return httpx.Response(200,json={'paths':{'/rpc/store_message_extraction':{}},'definitions':{}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:report=await inspect(s,client)
    assert report['status']=='metadata_available' and report['functions']['store_message_extraction']
    assert not report['functions']['read_commitment_lifecycle'] and len(calls)==1
    assert 'fixture-key' not in str(report)
