"""Tests for meeting reminders and user-configured reminder duration from settings."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4
import pytest

from app.core.config import Settings
from app.models.reminders import (
    DispatchContext,
    NotificationFailure,
    NotificationReceipt,
    ReminderDelivery,
    ReminderHistory,
    ReminderIntention,
    ReminderPolicy,
    ReminderWork,
)
from app.models.commitments import CommitmentState
from app.services.reminder_planner import plan_reminder
from app.services.notification_dispatcher import reminder_template, reminder_notification
from app.services.reminder_settings_service import calculate_lead_minutes, configure_reminders
from app.repositories.sqlite_reminders import SqliteReminderRepository
from app.repositories.sqlite_db import get_sqlite_connection, init_sqlite_db
from app.services.commitment_chat_evidence import personal_commitment


NOW = datetime(2026, 11, 6, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_calculate_lead_minutes():
    assert calculate_lead_minutes(30, "minutes") == 30
    assert calculate_lead_minutes(1, "hours") == 60
    assert calculate_lead_minutes(2, "hours") == 120
    assert calculate_lead_minutes(1, "days") == 1440
    assert calculate_lead_minutes(0, "minutes") == 0


def test_meeting_phrasing_detection():
    # Meeting with topic and time
    msg1 = "@Codex meeting with frontend team at 2026-11-06T18:00:00Z"
    result1 = personal_commitment(msg1, {"addressed": True})
    assert result1 is not None
    assert "Meeting:" in result1[0]
    assert "2026-11-06T18:00:00Z" in result1[1]

    # Meeting schedule syntax
    msg2 = "<@12345> schedule a meeting for roadmap review at 2026-11-07T10:00:00Z"
    result2 = personal_commitment(msg2, {"addressed": True})
    assert result2 is not None
    assert "Meeting:" in result2[0]

    # Standard commitment still works
    msg3 = "@Codex I will review the pull request by 2026-11-06T17:00:00Z"
    result3 = personal_commitment(msg3, {"addressed": True})
    assert result3 is not None
    assert "review the pull request" in result3[0]

    # User's exact prompt with newline and PKT timezone
    msg4 = "@team-memory-client set a meeting for Nov 6 3:10 PM pkt\n 2026 with the title of discussing technologies"
    result4 = personal_commitment(msg4, {"addressed": True})
    assert result4 is not None
    assert result4[0] == "Meeting: discussing technologies"
    assert "Nov 6 3:10 PM pkt" in result4[1]

    from types import SimpleNamespace
    from app.services.commitment_chat_evidence import resolved_deadline
    mock_plan = SimpleNamespace(
        source=SimpleNamespace(occurred_at=NOW),
        policy=SimpleNamespace(timezone="Asia/Karachi"),
    )
    parsed_due = resolved_deadline(result4[1], mock_plan, None)
    assert parsed_due is not None
    # 3:10 PM PKT (UTC+5) is 10:10 UTC
    assert parsed_due.hour == 10
    assert parsed_due.minute == 10
    assert parsed_due.day == 6
    assert parsed_due.month == 11
    assert parsed_due.year == 2026

    # User's variation: "for the title discussing technologies 2026 Nov 6 3:10 pm pkt"
    msg5 = "@team-memory-client set a meeting for the title discussing technologies  2026  Nov 6 3:10 pm pkt"
    result5 = personal_commitment(msg5, {"addressed": True})
    assert result5 is not None
    assert result5[0] == "Meeting: discussing technologies"
    parsed_due5 = resolved_deadline(result5[1], mock_plan, None)
    assert parsed_due5.hour == 10 and parsed_due5.minute == 10 and parsed_due5.year == 2026

    # User's variation: without year or title (should default year to current year and topic to Team sync)
    msg6 = "@team-memory-client set a meeting for Nov 6 3:10 pm pkt"
    result6 = personal_commitment(msg6, {"addressed": True})
    assert result6 is not None
    assert result6[0] == "Meeting: Team sync"
    parsed_due6 = resolved_deadline(result6[1], mock_plan, None)
    assert parsed_due6.hour == 10 and parsed_due6.minute == 10 and parsed_due6.year == NOW.year


def test_meeting_reminder_planning_with_custom_user_duration():
    meeting_time = NOW + timedelta(minutes=45)
    comm = CommitmentState(
        id=uuid4(),
        tenant_id=uuid4(),
        room_id=uuid4(),
        message_id=uuid4(),
        extraction_key="comm-1",
        description="Meeting: Architecture Sync",
        due_at=meeting_time,
        approval_status="approved",
        approval_by_switch_user_id="admin",
        approval_at=NOW,
        task_status="not_started",
        owner_switch_user_id="user-1",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )

    # 1. User sets lead_minutes to 30. Meeting is in 45 min.
    # At NOW, 45 min remaining > 30 min lead -> should NOT trigger yet
    policy_30 = ReminderPolicy(enabled=True, lead_minutes=30, timezone="UTC")
    work_30 = ReminderWork(
        commitment=comm,
        policy=policy_30,
        room_enabled=True,
        assignee_is_member=True,
        switch_room_id="room-1",
        platform="discord",
    )
    intention = plan_reminder(work_30, NOW)
    assert intention is None

    # 2. Clock moves to NOW + 20 min (25 min before meeting).
    # Since 25 min <= 30 min lead -> intention planned!
    clock_20 = NOW + timedelta(minutes=20)
    intention = plan_reminder(work_30, clock_20)
    assert intention is not None
    assert intention.kind == "upcoming"
    assert intention.commitment_id == comm.id

    # 3. User changed duration in settings to 60 minutes!
    # At NOW (45 min before meeting), since 45 min <= 60 min lead -> intention planned immediately!
    policy_60 = ReminderPolicy(enabled=True, lead_minutes=60, timezone="UTC")
    work_60 = ReminderWork(
        commitment=comm,
        policy=policy_60,
        room_enabled=True,
        assignee_is_member=True,
        switch_room_id="room-1",
        platform="discord",
    )
    intention_60 = plan_reminder(work_60, NOW)
    assert intention_60 is not None
    assert intention_60.kind == "upcoming"


def test_meeting_reminder_template_and_room_dispatch():
    comm = CommitmentState(
        id=uuid4(),
        tenant_id=uuid4(),
        room_id=uuid4(),
        message_id=uuid4(),
        extraction_key="comm-1",
        description="Meeting: Weekly Standup",
        due_at=NOW + timedelta(minutes=15),
        approval_status="approved",
        approval_by_switch_user_id="admin",
        approval_at=NOW,
        task_status="not_started",
        owner_switch_user_id="user-1",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    policy = ReminderPolicy(enabled=True, lead_minutes=30, timezone="UTC", delivery_mode="source_thread")
    work = ReminderWork(
        commitment=comm,
        policy=policy,
        room_enabled=True,
        assignee_is_member=True,
        switch_room_id="room-sync",
        platform="discord",
        thread_id="thread-xyz",
    )
    intention = plan_reminder(work, NOW)
    assert intention is not None

    deliv = ReminderDelivery(
        id=uuid4(),
        intention=intention,
        status="sending",
    )
    ctx = DispatchContext(delivery=deliv, work=work)
    settings = Settings(_env_file=None, reminders_enabled=True)

    text = reminder_template(ctx, settings)
    assert "🔔 Reminder: Upcoming meeting: Meeting: Weekly Standup" in text
    assert "Deadline (UTC):" in text
    assert f"[source:{comm.message_id}]" in text
    assert f"[reminder:{intention.occurrence_key}]" in text

    # For meetings, notification routes directly to the room
    envelope = reminder_notification(ctx, settings)
    assert envelope.room_id == "room-sync"
    assert envelope.thread_id is None  # Meetings go to the room channel so all participants see it


@pytest.mark.anyio
async def test_sqlite_reminder_repository_lifecycle(tmp_path):
    db_file = tmp_path / "test_reminders.db"
    tenant_id = uuid4()
    room_id = uuid4()
    msg_id = uuid4()
    comm_id = uuid4()

    settings = Settings(
        _env_file=None,
        database_engine="sqlite",
        sqlite_db_path=str(db_file),
        switch_tenant_id=tenant_id,
        switch_room_ids="test-room",
        reminders_enabled=True,
        reminder_delivery_enabled=True,
        reminder_lead_minutes=30,
    )
    init_sqlite_db(settings)

    # Insert tenant, room, message, and meeting commitment
    with get_sqlite_connection(settings) as conn:
        with conn:
            conn.execute(
                "INSERT INTO tenants (id, name, created_at) VALUES (?, 'Test Tenant', DATETIME('now'))",
                (str(tenant_id),),
            )
            conn.execute(
                "INSERT INTO rooms (id, tenant_id, switch_room_id, name, created_at) VALUES (?, ?, 'test-room', 'Room 1', DATETIME('now'))",
                (str(room_id), str(tenant_id)),
            )
            conn.execute(
                """
                INSERT INTO messages (id, tenant_id, room_id, switch_message_id, switch_author_id,
                                      author_name, body, occurred_at, created_at)
                VALUES (?, ?, ?, 'msg-1', 'author-1', 'Alice', 'Meeting tomorrow', DATETIME('now'), DATETIME('now'))
                """,
                (str(msg_id), str(tenant_id), str(room_id)),
            )
            meeting_due = (NOW + timedelta(minutes=20)).isoformat()
            conn.execute(
                """
                INSERT INTO commitments (id, tenant_id, room_id, message_id, owner_name,
                                         description, due_at, approval_status, task_status, created_at)
                VALUES (?, ?, ?, ?, 'Alice', 'Meeting: Quarterly Planning', ?, 'approved', 'open', DATETIME('now'))
                """,
                (str(comm_id), str(tenant_id), str(room_id), str(msg_id), meeting_due),
            )

    repo = SqliteReminderRepository(settings, clock=lambda: NOW)
    work_items, cursor = await repo.work(10)
    assert len(work_items) == 1
    assert work_items[0].commitment.description == "Meeting: Quarterly Planning"
    assert work_items[0].policy.lead_minutes == 30

    # Plan reminder at NOW (20 min before meeting, within 30 min lead)
    intention = plan_reminder(work_items[0], NOW)
    assert intention is not None

    # Persist intention
    persisted = await repo.persist(intention)
    assert persisted is True
    # Duplicate persist rejected
    assert await repo.persist(intention) is False

    # Claim intention
    claimed = await repo.claim(10)
    assert len(claimed) == 1
    d = claimed[0]
    assert d.status == "sending"
    assert d.claim_token is not None

    # Context
    ctx = await repo.context(d)
    assert ctx is not None
    assert ctx.work.commitment.description == "Meeting: Quarterly Planning"

    # Begin dispatch
    begun = await repo.begin(d, "Test reminder body")
    assert begun is True

    # Finish dispatch
    await repo.finish(d, "sent", message_id="switch-event-123")

    # Verify work history updated
    work_after, _ = await repo.work(10)
    assert work_after[0].history.upcoming_sent is True


@pytest.mark.anyio
async def test_configure_reminders_service(tmp_path, monkeypatch):
    test_env = tmp_path / ".env"
    monkeypatch.setattr("app.services.env_service.ENV_PATH", test_env)

    result = await configure_reminders(
        enabled=True,
        lead_time=45,
        lead_unit="minutes",
        lead_minutes=45,
        delivery_enabled=True,
    )
    assert result["status"] == "ok"
    assert result["enabled"] is True
    assert result["lead_minutes"] == 45

    # Check env was written
    content = test_env.read_text(encoding="utf-8")
    assert "REMINDERS_ENABLED=true" in content
    assert "REMINDER_DELIVERY_ENABLED=true" in content
    assert "REMINDER_LEAD_MINUTES=45" in content
    assert "REMINDER_LEAD_TIME=45" in content
    assert "REMINDER_LEAD_UNIT=minutes" in content
