"""Notification dispatch boundary. Fixed templates; no model or broad retry fallback."""
import asyncio
import hashlib
import json
import re
from datetime import datetime,timedelta,timezone
from app.models.reminders import NotificationFailure,NotificationEnvelope
from app.services.reminder_planner import eligible,after_quiet_hours,utc
from app.core.message_logging import message_text_for_log
from app.core.feature_logging import feature_event


def notification_fingerprint(room_id,body):
    # Compatible with AIService's existing persisted echo guard.
    return hashlib.sha256(json.dumps([room_id,body.strip()]).encode()).hexdigest()


def dispatch_when(context,now):
    work,delivery=context.work,context.delivery
    record,policy,history=work.commitment,work.policy,work.history
    intent=delivery.intention;now=utc(now)
    if (record.tenant_id,record.room_id,record.id)!=(intent.tenant_id,intent.room_id,intent.commitment_id):return None
    if (not eligible(work) or record.version!=intent.commitment_version or policy.version!=intent.policy_version
        or record.owner_switch_user_id!=intent.recipient_id):return None
    if intent.kind=='upcoming' and (now>=record.due_at or history.upcoming_sent):return None
    if intent.kind=='overdue' and (history.overdue_sent!=intent.ordinal or history.overdue_sent>=policy.max_followups):return None
    earliest=max(now,intent.occurrence_base)
    if history.last_sent_at:earliest=max(earliest,history.last_sent_at+timedelta(minutes=policy.minimum_gap_minutes))
    if history.last_room_sent_at:earliest=max(earliest,history.last_room_sent_at+timedelta(seconds=policy.room_gap_seconds))
    if intent.kind=='overdue' and history.last_overdue_at:earliest=max(earliest,history.last_overdue_at+timedelta(minutes=policy.cadence_minutes))
    when=after_quiet_hours(earliest,policy)
    if intent.kind=='upcoming' and when and when>=record.due_at:return None
    return when


def reminder_template(context,settings):
    task=context.work.commitment
    description=json.loads(message_text_for_log(task.description,settings,max_length=500))
    # Never forward mass/user mentions embedded in source text; no fabricated pings.
    description=' '.join(description.replace('@','＠').replace('<','‹').replace('>','›').split())
    is_meeting = bool(re.search(r'\bmeeting\b', task.description, re.I))
    if is_meeting:
        label='Upcoming' if context.delivery.intention.kind=='upcoming' else 'Due / overdue'
        return (f'🔔 Reminder: {label} meeting: {description}\nDeadline (UTC): {utc(task.due_at).isoformat()}\n'
                f'[source:{task.message_id}] [reminder:{context.delivery.intention.occurrence_key}]')
    label='Upcoming' if context.delivery.intention.kind=='upcoming' else 'Due / overdue'
    return (f'{label} task: {description}\nDeadline (UTC): {utc(task.due_at).isoformat()}\n'
            f'[source:{task.message_id}] [reminder:{context.delivery.intention.occurrence_key}]')


def reminder_notification(context,settings):
    is_meeting = bool(re.search(r'\bmeeting\b', context.work.commitment.description, re.I))
    thread = None if is_meeting else (context.work.thread_id if context.work.policy.delivery_mode=='source_thread' else None)
    return NotificationEnvelope(room_id=context.work.switch_room_id,body=reminder_template(context,settings),
        thread_id=thread)


class NotificationDispatcher:
    def __init__(self,repository,sink,journal,settings,clock=lambda:datetime.now(timezone.utc),
                 readiness=dispatch_when,render=reminder_notification):
        self.repository,self.sink,self.journal,self.settings,self.clock=repository,sink,journal,settings,clock
        self.lock=asyncio.Lock()
        self.readiness,self.render=readiness,render

    async def tick(self):
        if self.lock.locked():
            feature_event('reminder_dispatch','skipped',reason='tick_already_running')
            return 0
        async with self.lock:
            count=0
            for _ in range(4):
                claimed=await self.repository.claim(1)
                if not claimed:break
                delivery=claimed[0]
                feature_event('reminder_dispatch','claimed',reference=delivery.id,task=delivery.intention.commitment_id,attempts=delivery.attempts)
                context=await self.repository.context(delivery)
                if context is None:
                    feature_event('reminder_dispatch','skipped',reference=delivery.id,reason='current_state_recheck')
                    continue
                now=utc(self.clock());when=self.readiness(context,now)
                if when is None or when>now:
                    accepted=await self.repository.defer(delivery,when)
                    feature_event('reminder_dispatch',('deferred' if when else 'superseded') if accepted is True else 'skipped',
                                  reference=delivery.id,reason='quiet_hours_or_cadence' if when else 'current_state_recheck',
                                  wait_seconds=max(0,int((when-now).total_seconds())) if when else 0)
                    continue
                notification=self.render(context,self.settings)
                body=notification.body
                await self.journal.remember_reply(notification_fingerprint(notification.room_id,body))
                # Final database recheck and durable send-start marker, then release transaction.
                if not await self.repository.begin(delivery,body):
                    feature_event('reminder_dispatch','skipped',reference=delivery.id,reason='current_state_recheck')
                    continue
                feature_event('reminder_dispatch','send_started',reference=delivery.id,attempts=delivery.attempts+1)
                try:
                    receipt=await self.sink.send(notification.room_id,body,notification.thread_id)
                except NotificationFailure as error:
                    await self._save_result(delivery,error.kind,error=error.category)
                    state='ambiguous' if error.kind=='ambiguous' else ('retry_scheduled' if error.kind=='transient' and delivery.attempts+1<3 else 'failed')
                    feature_event('reminder_dispatch',state,reference=delivery.id,reason=error.category,attempts=delivery.attempts+1)
                except asyncio.CancelledError:
                    feature_event('reminder_dispatch','ambiguous',reference=delivery.id,reason='interrupted')
                    raise # Expired started claims become ambiguous, never blind retries.
                except Exception:
                    await self._save_result(delivery,'ambiguous',error='send_outcome_unknown')
                    feature_event('reminder_dispatch','ambiguous',reference=delivery.id,reason='send_outcome_unknown')
                else:
                    await self._save_result(delivery,'sent',message_id=receipt.message_id)
                    count+=1
                    feature_event('reminder_dispatch','acknowledged',reference=delivery.id,attempts=delivery.attempts+1)
            feature_event('reminder_dispatch','succeeded' if count else 'idle',acknowledged=count)
            return count

    async def _save_result(self,delivery,outcome,**kwargs):
        try:
            await self.repository.finish(delivery,outcome,**kwargs)
        except Exception:
            feature_event('reminder_dispatch','failed',reference=delivery.id,reason='storage_unavailable')
            raise
