"""Notification dispatch boundary. Fixed templates; no model or broad retry fallback."""
import asyncio
import hashlib
import json
from datetime import datetime,timedelta,timezone
from app.models.reminders import NotificationFailure,NotificationEnvelope
from app.services.reminder_planner import eligible,after_quiet_hours,utc
from app.core.message_logging import message_text_for_log


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
    label='Upcoming' if context.delivery.intention.kind=='upcoming' else 'Due / overdue'
    return (f'{label} task: {description}\nDeadline (UTC): {utc(task.due_at).isoformat()}\n'
            f'[source:{task.message_id}] [reminder:{context.delivery.intention.occurrence_key}]')


def reminder_notification(context,settings):
    return NotificationEnvelope(room_id=context.work.switch_room_id,body=reminder_template(context,settings),
        thread_id=context.work.thread_id if context.work.policy.delivery_mode=='source_thread' else None)


class NotificationDispatcher:
    def __init__(self,repository,sink,journal,settings,clock=lambda:datetime.now(timezone.utc),
                 readiness=dispatch_when,render=reminder_notification):
        self.repository,self.sink,self.journal,self.settings,self.clock=repository,sink,journal,settings,clock
        self.lock=asyncio.Lock()
        self.readiness,self.render=readiness,render

    async def tick(self):
        if self.lock.locked():return 0
        async with self.lock:
            count=0
            for _ in range(4):
                claimed=await self.repository.claim(1)
                if not claimed:break
                delivery=claimed[0]
                context=await self.repository.context(delivery)
                if context is None:continue
                now=utc(self.clock());when=self.readiness(context,now)
                if when is None or when>now:
                    await self.repository.defer(delivery,when);continue
                notification=self.render(context,self.settings)
                body=notification.body
                await self.journal.remember_reply(notification_fingerprint(notification.room_id,body))
                # Final database recheck and durable send-start marker, then release transaction.
                if not await self.repository.begin(delivery,body):continue
                try:
                    receipt=await self.sink.send(notification.room_id,body,notification.thread_id)
                except NotificationFailure as error:
                    await self.repository.finish(delivery,error.kind,error=error.category)
                except asyncio.CancelledError:raise # Expired started claims become ambiguous, never blind retries.
                except Exception:
                    await self.repository.finish(delivery,'ambiguous',error='send_outcome_unknown')
                else:
                    await self.repository.finish(delivery,'sent',message_id=receipt.message_id)
                    count+=1
            return count
