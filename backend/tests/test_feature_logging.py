"""Feature trace privacy, actual persisted outcomes and disabled-state visibility."""
import logging
from datetime import timedelta
from uuid import uuid4
import pytest
from app.core.config import get_settings
from app.core.feature_logging import feature_event,startup_events
from app.services.commitment_service import CommitmentService
from app.models.commitments import Actor,ApproveRequest,Action,CommitmentError
from app.services.reminder_scheduler import ReminderScheduler
from app.services.notification_dispatcher import NotificationDispatcher
from app.models.reminders import NotificationFailure
from test_commitment_lifecycle import FakeRepository,NOW,REVIEWER
from test_reminders import Clock,stack


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.fixture(autouse=True)
def capture_features(caplog,monkeypatch):
    monkeypatch.setattr(logging.getLogger('team_memory'),'propagate',True)
    caplog.set_level(logging.INFO,logger='team_memory.features')


def test_feature_trace_drops_secrets_contents_and_unknown_diagnostics(caplog):
    secret='synthetic-sensitive-credential'
    feature_event('commitment','requested',reference=secret,task=secret,reason=secret,action=secret,
                  body=secret,prompt=secret,token=secret,count=2,version=1)
    text=caplog.text
    assert '[feature]' in text and 'component=commitment state=requested' in text
    assert secret not in text and 'reason=other' in text and 'action=unknown' in text
    assert 'count=2' in text and 'body=' not in text and 'token=' not in text and 'prompt=' not in text
    feature_event('commitment','failed',reference='source\nFORGED LOG',reason='failed\nFORGED LOG')
    assert 'FORGED LOG' not in caplog.text


def test_trace_can_be_disabled_without_turning_on_features(monkeypatch,caplog):
    monkeypatch.setenv('FEATURE_LOGS_ENABLED','false');get_settings.cache_clear()
    feature_event('commitment','applied',count=1)
    assert '[feature]' not in caplog.text
    assert not get_settings().reminders_enabled and not get_settings().automatic_commitments_enabled


def test_startup_reports_disabled_features_and_authentication_limits(caplog):
    s=get_settings().model_copy(update={'reminders_enabled':False,'automatic_commitments_enabled':False,
                                      'local_embeddings_enabled':False})
    startup_events(s,False)
    assert 'component=reminder_planner state=disabled reason=flag_disabled' in caplog.text
    assert 'component=reminder_dispatch state=disabled reason=flag_disabled' in caplog.text
    assert 'component=commitment state=blocked reason=human_auth_unavailable' in caplog.text
    assert 'component=ai state=blocked reason=ai_not_configured' in caplog.text


@pytest.mark.anyio
async def test_commitment_logs_applied_only_after_success_and_replay_separately(caplog):
    repo=FakeRepository();service=CommitmentService(repo,clock=lambda:NOW)
    request=ApproveRequest(idempotency_key=uuid4(),expected_version=1,reason='Private review reason')
    await service.perform(repo.scope,REVIEWER,repo.record.id,Action.approve,request)
    assert 'state=applied action=approve' in caplog.text and 'version=2' in caplog.text
    assert 'Private review reason' not in caplog.text and repo.record.approval_status=='approved'
    caplog.clear()
    await service.perform(repo.scope,REVIEWER,repo.record.id,Action.approve,request)
    assert 'state=replayed action=approve' in caplog.text and 'state=applied' not in caplog.text


@pytest.mark.anyio
async def test_failed_commitment_never_logs_success(caplog):
    repo=FakeRepository();repo.fail=True;service=CommitmentService(repo,clock=lambda:NOW)
    with pytest.raises(RuntimeError):
        await service.perform(repo.scope,REVIEWER,repo.record.id,Action.approve,ApproveRequest(idempotency_key=uuid4(),expected_version=1))
    assert 'component=commitment state=failed' in caplog.text and 'state=applied' not in caplog.text
    assert repo.record.version==1


@pytest.mark.anyio
async def test_authorization_failures_are_visible_without_identity_data(caplog):
    repo=FakeRepository();service=CommitmentService(repo)
    with pytest.raises(CommitmentError):
        await service.perform(repo.scope,Actor(subject_id='worker'),repo.record.id,Action.approve,
            ApproveRequest(idempotency_key=uuid4(),expected_version=1))
    assert 'state=blocked action=approve reason=forbidden_action' in caplog.text
    assert 'subject_id' not in caplog.text


@pytest.mark.anyio
async def test_reminder_trace_separates_persistence_and_saved_switch_ack(caplog):
    clock=Clock();repo,scheduler,dispatcher,sink,_=stack(clock)
    await scheduler.tick()
    assert 'component=reminder_planner state=persisted' in caplog.text
    assert 'state=acknowledged' not in caplog.text
    caplog.clear();await dispatcher.tick()
    assert 'component=reminder_dispatch state=send_started' in caplog.text
    assert 'component=reminder_dispatch state=acknowledged' in caplog.text
    assert 'fixture-switch-reference' not in caplog.text and 'Deliver report' not in caplog.text


@pytest.mark.anyio
async def test_unsaved_acknowledgement_never_logs_delivery_success(caplog):
    clock=Clock();repo,scheduler,dispatcher,sink,_=stack(clock)
    await scheduler.tick();repo.fail_finish=True;caplog.clear()
    with pytest.raises(RuntimeError):await dispatcher.tick()
    assert len(sink.posts)==1
    assert 'component=reminder_dispatch state=failed' in caplog.text
    assert 'state=acknowledged' not in caplog.text


@pytest.mark.anyio
async def test_ambiguous_delivery_trace_never_claims_success(caplog):
    clock=Clock();repo,scheduler,dispatcher,sink,_=stack(clock)
    sink.failure=NotificationFailure('ambiguous','send_outcome_unknown')
    await scheduler.tick();caplog.clear();await dispatcher.tick()
    assert 'component=reminder_dispatch state=ambiguous' in caplog.text
    assert 'state=acknowledged' not in caplog.text


@pytest.mark.anyio
async def test_quiet_hours_deferral_is_visible_without_counting_a_send(caplog):
    clock=Clock();repo,scheduler,dispatcher,sink,_=stack(clock)
    repo.commitments.record=repo.commitments.record.model_copy(update={'due_at':NOW-timedelta(days=2)})
    await scheduler.tick();caplog.clear();clock.at=NOW.replace(hour=23)
    await dispatcher.tick()
    assert 'component=reminder_dispatch state=deferred reason=quiet_hours_or_cadence' in caplog.text
    assert not sink.posts and repo.last_chased is None and next(iter(repo.rows.values())).attempts==0
