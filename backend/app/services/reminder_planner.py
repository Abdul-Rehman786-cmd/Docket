"""Pure reminder planning. No database, Switch, LLM or wall clock calls."""
from datetime import datetime,timedelta,timezone
from uuid import NAMESPACE_URL,uuid5
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from app.models.reminders import ReminderWork,ReminderIntention,ReminderPolicy
from app.services.commitment_service import reminder_state


def utc(at):
    if at.tzinfo is None or at.utcoffset() is None: raise ValueError('aware_clock_required')
    return at.astimezone(timezone.utc)


def policy_zone(policy):
    if not policy.timezone:return None
    try:return timezone.utc if policy.timezone=='UTC' else ZoneInfo(policy.timezone)
    except (ZoneInfoNotFoundError,ValueError):return None


def after_quiet_hours(at,policy):
    """Scan real instants through gaps/folds; never construct a nonexistent local time."""
    at=utc(at); zone=policy_zone(policy)
    if zone is None:return None
    if policy.quiet_start==policy.quiet_end:return at # Explicit equal hours disables quiet time.
    def quiet(value):
        hour=value.astimezone(zone).hour
        return (hour>=policy.quiet_start or hour<policy.quiet_end) if policy.quiet_start>policy.quiet_end else policy.quiet_start<=hour<policy.quiet_end
    if not quiet(at):return at
    cursor=at.replace(second=0,microsecond=0)+timedelta(minutes=1)
    for _ in range(1800): # Bounded >24h, including a long DST day.
        if not quiet(cursor):return cursor
        cursor+=timedelta(minutes=1)
    return None


def eligible(work):
    return (work.room_enabled and work.assignee_is_member and work.platform=='discord' and
            work.policy.enabled and policy_zone(work.policy) is not None and reminder_state(work.commitment).eligible)


def plan_reminder(work:ReminderWork,now:datetime)->ReminderIntention|None:
    now=utc(now)
    if not eligible(work) or work.history.blocking_delivery:return None
    task,prefs,history=work.commitment,work.policy,work.history
    due=utc(task.due_at)
    if now<due:
        if history.upcoming_sent:return None
        kind,ordinal,base='upcoming',0,due-timedelta(minutes=prefs.lead_minutes)
    else:
        if history.overdue_sent>=prefs.max_followups:return None
        kind,ordinal='overdue',history.overdue_sent
        base=max(due,utc(history.last_overdue_at)+timedelta(minutes=prefs.cadence_minutes)) if history.last_overdue_at else due
    if history.last_sent_at:
        base=max(base,utc(history.last_sent_at)+timedelta(minutes=prefs.minimum_gap_minutes))
    if history.last_room_sent_at:
        base=max(base,utc(history.last_room_sent_at)+timedelta(seconds=prefs.room_gap_seconds))
    if now<base:return None
    scheduled=after_quiet_hours(now,prefs)
    if scheduled is None or (kind=='upcoming' and scheduled>=due):return None
    # Identity uses the logical cycle/ordinal, never the time of this scheduler tick.
    key=uuid5(NAMESPACE_URL,f'reminder:{task.tenant_id}:{task.room_id}:{task.id}:{task.version}:{prefs.version}:{kind}:{ordinal}')
    return ReminderIntention(occurrence_key=key,tenant_id=task.tenant_id,room_id=task.room_id,
        commitment_id=task.id,recipient_id=task.owner_switch_user_id,commitment_version=task.version,
        policy_version=prefs.version,kind=kind,ordinal=ordinal,occurrence_base=base,scheduled_for=scheduled)
