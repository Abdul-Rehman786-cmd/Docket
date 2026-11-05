"""Bounded scheduler adapter. Plans/persists work; never sends or calls a model."""
import asyncio
from datetime import datetime,timezone
from app.services.reminder_planner import plan_reminder
from app.core.feature_logging import feature_event


class ReminderScheduler:
    def __init__(self,repository,clock=lambda:datetime.now(timezone.utc)):
        self.repository,self.clock=repository,clock
        self.lock=asyncio.Lock();self.cursor=None

    async def tick(self):
        if self.lock.locked():
            feature_event('reminder_planner','skipped',reason='tick_already_running')
            return 0
        async with self.lock:
            rows,cursor=await self.repository.work(25,self.cursor)
            count=0
            for work in rows:
                intention=plan_reminder(work,self.clock())
                if intention and await self.repository.persist(intention):
                    count+=1
                    feature_event('reminder_planner','persisted',reference=intention.occurrence_key,task=intention.commitment_id,
                                  version=intention.commitment_version,policy_version=intention.policy_version)
                elif intention:
                    feature_event('reminder_planner','skipped',reference=intention.occurrence_key,reason='current_state_recheck')
                else:
                    feature_event('reminder_planner','skipped',task=work.commitment.id,reason='no_eligible_intention')
            self.cursor=cursor
            feature_event('reminder_planner','succeeded' if count else 'idle',scanned=len(rows),persisted=count)
            return count
