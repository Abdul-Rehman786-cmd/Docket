"""Bounded scheduler adapter. Plans/persists work; never sends or calls a model."""
import asyncio
from datetime import datetime,timezone
from app.services.reminder_planner import plan_reminder


class ReminderScheduler:
    def __init__(self,repository,clock=lambda:datetime.now(timezone.utc)):
        self.repository,self.clock=repository,clock
        self.lock=asyncio.Lock();self.cursor=None

    async def tick(self):
        if self.lock.locked():return 0
        async with self.lock:
            rows,cursor=await self.repository.work(25,self.cursor)
            count=0
            for work in rows:
                intention=plan_reminder(work,self.clock())
                if intention and await self.repository.persist(intention):count+=1
            self.cursor=cursor
            return count
