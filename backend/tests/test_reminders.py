"""Fake domain repositories/sink + deterministic clock; no external chat or hosted writes."""
import asyncio
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import pytest
from app.core.config import Settings
from app.models.reminders import (ReminderWork,ReminderPolicy,ReminderHistory,ReminderDelivery,DispatchContext,
    NotificationReceipt,NotificationFailure)
from app.services.reminder_planner import plan_reminder,after_quiet_hours
from app.services.reminder_scheduler import ReminderScheduler
from app.services.notification_dispatcher import NotificationDispatcher,notification_fingerprint
from app.repositories.ingestion_jobs import IngestionJobs
from test_commitment_lifecycle import FakeRepository,NOW


@pytest.fixture
def anyio_backend():return 'asyncio'


class Clock:
    def __init__(self):self.at=NOW
    def __call__(self):return self.at
    def advance(self,**kwargs):self.at+=timedelta(**kwargs)


def sample():
    record=FakeRepository().record.model_copy(update={'approval_status':'approved','approval_at':NOW,
        'approval_by_switch_user_id':'admin','due_at':NOW+timedelta(minutes=20)})
    return ReminderWork(commitment=record,policy=ReminderPolicy(enabled=True,timezone='UTC'),
        room_enabled=True,assignee_is_member=True,switch_room_id='room',platform='discord',thread_id='source-thread')


class FakePreferencesRepository:
    def __init__(self,policy):self.policy=policy
    def change(self,**fields):self.policy=self.policy.model_copy(update={**fields,'version':self.policy.version+1})


class FakeCommitmentRepository:
    def __init__(self,record):self.record=record
    def change(self,**fields):self.record=self.record.model_copy(update={**fields,'version':self.record.version+1})


class FakeOutbox:
    def __init__(self,clock,work=None):
        self.clock=clock;self.template=work or sample()
        self.commitments=FakeCommitmentRepository(self.template.commitment)
        self.preferences=FakePreferencesRepository(self.template.policy)
        self.rows={};self.lock=asyncio.Lock();self.work_calls=0;self.last_chased=None;self.fail_finish=False

    def snapshot(self):
        record,policy=self.commitments.record,self.preferences.policy
        sent=[d for d in self.rows.values() if d.status=='sent']
        cycle=[d for d in self.rows.values() if d.intention.commitment_version==record.version and d.intention.policy_version==policy.version]
        successes=[d for d in cycle if d.status=='sent']
        overdue=[d for d in successes if d.intention.kind=='overdue']
        blocking=any(d.status in ('pending','sending','failed','ambiguous') and
            (d.status=='ambiguous' or (d.status=='failed' and d.next_attempt_at is None) or
            d.intention.kind!='upcoming' or record.due_at>self.clock() or d.dispatch_started_at is not None) for d in cycle)
        history=ReminderHistory(upcoming_sent=any(d.intention.kind=='upcoming' for d in successes),overdue_sent=len(overdue),
            last_sent_at=max((d.sent_at for d in sent),default=None),last_overdue_at=max((d.sent_at for d in overdue),default=None),
            last_room_sent_at=max((d.sent_at for d in sent),default=None),
            blocking_delivery=blocking)
        return self.template.model_copy(update={'commitment':record,'policy':policy,'history':history})

    def current(self,d):
        from app.services.reminder_planner import eligible
        w=self.snapshot();r=w.commitment
        return eligible(w) and d.intention.commitment_version==r.version and d.intention.policy_version==w.policy.version and (
            d.intention.recipient_id==r.owner_switch_user_id) and (d.intention.kind!='upcoming' or self.clock()<r.due_at)

    async def work(self,limit,after=None):
        self.work_calls+=1
        return [self.snapshot()],None

    async def persist(self,intention):
        async with self.lock:
            if intention.occurrence_key in self.rows:return False
            work=self.snapshot()
            if work.history.blocking_delivery:return False
            if intention.commitment_version!=work.commitment.version or intention.policy_version!=work.policy.version:return False
            for key,row in self.rows.items():
                if row.intention.kind=='upcoming' and row.status in ('pending','failed') and self.clock()>=work.commitment.due_at:
                    self.rows[key]=row.model_copy(update={'status':'superseded'})
            self.rows[intention.occurrence_key]=ReminderDelivery(id=uuid4(),intention=intention,status='pending',next_attempt_at=intention.scheduled_for)
            return True

    async def claim(self,limit):
        async with self.lock:
            for key,row in list(self.rows.items()):
                if row.status=='sending' and row.lease_until<=self.clock():
                    row=row.model_copy(update={'status':'ambiguous' if row.dispatch_started_at else 'pending',
                        'next_attempt_at':None if row.dispatch_started_at else self.clock()})
                if row.status in ('pending','failed') and not self.current(row):row=row.model_copy(update={'status':'superseded'})
                self.rows[key]=row
            found=[]
            for key,row in self.rows.items():
                if row.status in ('pending','failed') and row.next_attempt_at is not None and row.next_attempt_at<=self.clock() and row.attempts<3:
                    row=row.model_copy(update={'status':'sending','claim_token':uuid4(),'lease_until':self.clock()+timedelta(minutes=2)})
                    self.rows[key]=row;found.append(row)
                    if len(found)==limit:break
            return found

    def owned(self,delivery):
        row=self.rows[delivery.intention.occurrence_key]
        return row if row.claim_token==delivery.claim_token and row.status in ('sending','ambiguous') else None

    async def context(self,delivery):
        row=self.owned(delivery)
        return DispatchContext(delivery=row,work=self.snapshot()) if row else None

    async def begin(self,delivery,body):
        from app.services.notification_dispatcher import dispatch_when
        async with self.lock:
            row=self.owned(delivery)
            if not row or row.dispatch_started_at or row.lease_until<=self.clock():return False
            context=DispatchContext(delivery=row,work=self.snapshot())
            when=dispatch_when(context,self.clock())
            if when is None or when>self.clock():return False
            self.rows[delivery.intention.occurrence_key]=row.model_copy(update={'dispatch_started_at':self.clock(),'attempts':row.attempts+1})
            return True

    async def defer(self,delivery,until):
        row=self.owned(delivery)
        if row:self.rows[delivery.intention.occurrence_key]=row.model_copy(update={'status':'pending' if until else 'superseded',
            'next_attempt_at':until,'claim_token':None,'lease_until':None})

    async def finish(self,delivery,outcome,message_id=None,error=None):
        if self.fail_finish:raise RuntimeError('Simulated lost database acknowledgement')
        row=self.owned(delivery)
        if not row:return
        if outcome=='sent':
            row=row.model_copy(update={'status':'sent','sent_at':self.clock(),'switch_reply_message_id':message_id,
                'next_attempt_at':None,'claim_token':None,'lease_until':None})
            if self.current(row):self.last_chased=self.clock()
        else:
            row=row.model_copy(update={'status':'ambiguous' if outcome=='ambiguous' else 'failed',
                'next_attempt_at':self.clock()+timedelta(seconds=30*4**(row.attempts-1)) if outcome=='transient' and row.attempts<3 else None,
                'dispatch_started_at':None if outcome=='transient' else row.dispatch_started_at,'error_code':error,'claim_token':None,'lease_until':None})
        self.rows[delivery.intention.occurrence_key]=row


class FakeSink:
    def __init__(self):self.posts=[];self.failure=None
    async def send(self,room_id,body,thread_id):
        self.posts.append((room_id,body,thread_id))
        if self.failure:raise self.failure
        return NotificationReceipt(message_id='fixture-switch-reference')


def stack(clock,work=None):
    repo=FakeOutbox(clock,work);sink=FakeSink();journal=IngestionJobs()
    dispatcher=NotificationDispatcher(repo,sink,journal,Settings(_env_file=None),clock)
    return repo,ReminderScheduler(repo,clock),dispatcher,sink,journal


def test_upcoming_due_overdue_and_stable_occurrences():
    w=sample();a=plan_reminder(w,NOW);b=plan_reminder(w,NOW+timedelta(seconds=15))
    assert a.kind=='upcoming' and a.occurrence_key==b.occurrence_key
    assert plan_reminder(w,NOW-timedelta(hours=1)) is None
    due=plan_reminder(w,w.commitment.due_at)
    assert due.kind=='overdue' and due.ordinal==0
    late=plan_reminder(w,NOW+timedelta(days=5))
    assert late.occurrence_key==due.occurrence_key # missed slots coalesce to one


@pytest.mark.parametrize('changes',[
    {'task_status':'completed','completed_at':NOW},{'task_status':'cancelled'},
    {'approval_status':'pending','approval_at':None,'approval_by_switch_user_id':None},
    {'approval_status':'rejected'},{'due_at':None},{'owner_switch_user_id':None},
])
def test_ineligible_task_states(changes):
    w=sample();w=w.model_copy(update={'commitment':w.commitment.model_copy(update=changes)})
    assert plan_reminder(w,NOW) is None


@pytest.mark.parametrize('field,value',[('room_enabled',False),('assignee_is_member',False),('platform','slack')])
def test_scope_and_membership_eligibility(field,value):
    assert plan_reminder(sample().model_copy(update={field:value}),NOW) is None


def test_disabled_missing_timezone_and_cadence_limits():
    w=sample()
    for p in [ReminderPolicy(),ReminderPolicy(enabled=True),ReminderPolicy(enabled=True,timezone='Unknown/Zone')]:
        assert plan_reminder(w.model_copy(update={'policy':p}),NOW) is None
    w=w.model_copy(update={'commitment':w.commitment.model_copy(update={'due_at':NOW-timedelta(days=2)})})
    assert plan_reminder(w.model_copy(update={'history':ReminderHistory(overdue_sent=3)}),NOW) is None
    h=ReminderHistory(overdue_sent=1,last_overdue_at=NOW-timedelta(hours=23),last_sent_at=NOW-timedelta(hours=23))
    assert plan_reminder(w.model_copy(update={'history':h}),NOW) is None
    h=h.model_copy(update={'last_overdue_at':NOW-timedelta(hours=25),'last_sent_at':NOW-timedelta(hours=25)})
    assert plan_reminder(w.model_copy(update={'history':h}),NOW).ordinal==1


@pytest.mark.parametrize('instant,zone,start,end,expected',[
 ('2026-11-05T23:00:00Z','UTC',22,8,'2026-11-06T08:00:00Z'),
 ('2026-11-06T07:59:00Z','UTC',22,8,'2026-11-06T08:00:00Z'),
 ('2026-11-06T12:00:00Z','UTC',22,8,'2026-11-06T12:00:00Z'),
 ('2026-11-06T12:00:00Z','UTC',8,17,'2026-11-06T17:00:00Z'),
 ('2026-03-08T06:30:00Z','America/New_York',22,2,'2026-03-08T07:00:00Z'),
 ('2026-11-01T05:30:00Z','America/New_York',22,2,'2026-11-01T07:00:00Z'),
])
def test_quiet_hours_overnight_and_dst(instant,zone,start,end,expected):
    p=ReminderPolicy(timezone=zone,quiet_start=start,quiet_end=end)
    assert after_quiet_hours(datetime.fromisoformat(instant),p)==datetime.fromisoformat(expected)


@pytest.mark.anyio
async def test_repeated_ticks_claims_success_and_no_llm(monkeypatch):
    async def forbidden(*args,**kwargs):raise AssertionError('No model calls permitted')
    monkeypatch.setattr('app.services.ai_service.ask_switch',forbidden)
    clock=Clock();repo,scheduler,dispatcher,sink,journal=stack(clock)
    assert await scheduler.tick()==1 and await scheduler.tick()==0 and repo.last_chased is None
    assert len(repo.rows)==1
    assert await dispatcher.tick()==1 and await dispatcher.tick()==0
    row=next(iter(repo.rows.values()))
    assert row.status=='sent' and row.sent_at==NOW and repo.last_chased==NOW and row.attempts==1
    assert sink.posts[0][2]=='source-thread'
    assert await journal.is_reply(notification_fingerprint(*sink.posts[0][:2]))
    clock.advance(minutes=20)
    assert await scheduler.tick()==0 # minimum successful-send gap prevents immediate chase
    clock.advance(minutes=11)
    assert await scheduler.tick()==1
    assert await dispatcher.tick()==1 and len(sink.posts)==2
    assert await scheduler.tick()==0


@pytest.mark.anyio
async def test_quiet_deferral_does_not_count_sent_or_accumulate_backlog():
    clock=Clock();clock.at=NOW.replace(hour=23)
    w=sample();w=w.model_copy(update={'commitment':w.commitment.model_copy(update={'due_at':NOW-timedelta(days=5)})})
    repo,scheduler,dispatcher,sink,_=stack(clock,w)
    assert await scheduler.tick()==1
    for _ in range(5):await scheduler.tick();await dispatcher.tick()
    assert len(repo.rows)==1 and not sink.posts and repo.last_chased is None
    clock.at=NOW.replace(hour=8)+timedelta(days=1)
    assert await dispatcher.tick()==1 and len(sink.posts)==1
    assert await scheduler.tick()==0


@pytest.mark.anyio
@pytest.mark.parametrize('changes',[{'task_status':'completed','completed_at':NOW},{'task_status':'cancelled'},
    {'owner_switch_user_id':'other'},{'due_at':NOW+timedelta(days=3)}])
async def test_task_changes_supersede_before_dispatch(changes):
    clock=Clock();repo,scheduler,dispatcher,sink,_=stack(clock)
    await scheduler.tick();repo.commitments.change(**changes)
    assert await dispatcher.tick()==0 and not sink.posts
    assert next(iter(repo.rows.values())).status=='superseded'


@pytest.mark.anyio
async def test_policy_changes_supersede_and_overlapping_ticks_are_bounded():
    clock=Clock();repo,scheduler,dispatcher,sink,_=stack(clock)
    assert sum(await asyncio.gather(scheduler.tick(),scheduler.tick()))==1
    repo.preferences.change(enabled=False)
    await dispatcher.tick()
    assert not sink.posts and next(iter(repo.rows.values())).status=='superseded'


@pytest.mark.anyio
async def test_concurrent_claims_and_restart_recovery():
    clock=Clock();repo,scheduler,dispatcher,sink,journal=stack(clock)
    await scheduler.tick()
    claims=await asyncio.gather(repo.claim(1),repo.claim(1))
    assert sum(map(len,claims))==1
    token=next(r for group in claims for r in group).claim_token
    clock.advance(minutes=3)
    recovered=await repo.claim(1)
    assert len(recovered)==1 and recovered[0].claim_token!=token
    await repo.begin(recovered[0],'Possible send')
    clock.advance(minutes=3)
    assert await repo.claim(1)==[] and next(iter(repo.rows.values())).status=='ambiguous'
    restarted=NotificationDispatcher(repo,sink,journal,Settings(_env_file=None),clock)
    assert await restarted.tick()==0 and not sink.posts


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['transient','permanent','ambiguous'])
async def test_delivery_failures(kind):
    clock=Clock();repo,scheduler,dispatcher,sink,_=stack(clock)
    sink.failure=NotificationFailure(kind,'switch_connect_failed' if kind=='transient' else 'send_outcome_unknown')
    await scheduler.tick();await dispatcher.tick()
    row=next(iter(repo.rows.values()))
    assert row.sent_at is None and repo.last_chased is None
    assert row.status==('ambiguous' if kind=='ambiguous' else 'failed')
    assert await scheduler.tick()==0
    if kind=='transient':
        assert row.next_attempt_at==NOW+timedelta(seconds=30)
        clock.advance(seconds=30);await dispatcher.tick()
        clock.advance(seconds=120);await dispatcher.tick()
        row=next(iter(repo.rows.values()))
        assert row.attempts==3 and row.next_attempt_at is None and len(sink.posts)==3
    else:
        clock.advance(days=2);await dispatcher.tick()
        assert len(sink.posts)==1


@pytest.mark.anyio
async def test_acknowledged_send_with_failed_db_finish_is_not_blindly_resent():
    clock=Clock();repo,scheduler,dispatcher,sink,journal=stack(clock)
    await scheduler.tick();repo.fail_finish=True
    with pytest.raises(RuntimeError):await dispatcher.tick()
    assert len(sink.posts)==1
    clock.advance(minutes=3);repo.fail_finish=False
    restarted=NotificationDispatcher(repo,sink,journal,Settings(_env_file=None),clock)
    assert await restarted.tick()==0 and len(sink.posts)==1
    assert next(iter(repo.rows.values())).status=='ambiguous'


@pytest.mark.anyio
async def test_scheduler_skips_a_tick_while_previous_tick_is_waiting_for_storage():
    clock=Clock();repo,scheduler,_,_,_=stack(clock)
    entered=asyncio.Event();release=asyncio.Event();original=repo.work
    async def blocked(*args,**kwargs):
        entered.set();await release.wait();return await original(*args,**kwargs)
    repo.work=blocked
    first=asyncio.create_task(scheduler.tick())
    await asyncio.wait_for(entered.wait(),1)
    assert await scheduler.tick()==0
    release.set()
    assert await first==1 and repo.work_calls==1


@pytest.mark.anyio
async def test_other_task_success_defers_room_delivery_without_counting_an_attempt():
    from app.services.notification_dispatcher import dispatch_when
    clock=Clock();repo,scheduler,_,_,_=stack(clock)
    await scheduler.tick();d=(await repo.claim(1))[0]
    w=repo.snapshot();w=w.model_copy(update={'history':w.history.model_copy(update={'last_room_sent_at':NOW})})
    assert dispatch_when(DispatchContext(delivery=d,work=w),NOW)==NOW+timedelta(seconds=60)
    assert next(iter(repo.rows.values())).attempts==0
