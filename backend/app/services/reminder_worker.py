"""Single-process application adapter. Separate bounded planning/dispatch loops."""
import asyncio
import logging
from app.core.config import get_settings
from app.repositories.reminder_repository import SupabaseReminderRepository
from app.repositories.ingestion_jobs import IngestionJobs
from app.services.reminder_scheduler import ReminderScheduler
from app.services.notification_dispatcher import NotificationDispatcher
from app.services.switch_notifications import SwitchNotificationSink


class ReminderWorker:
    def __init__(self):self.tasks=[]

    def start(self):
        self.tasks=[asyncio.create_task(self.run(False)),asyncio.create_task(self.run(True))]

    async def run(self,dispatch):
        adapter=None
        while True:
            settings=get_settings()
            try:
                if settings.reminders_enabled and settings.supabase_configured and (not dispatch or settings.reminder_delivery_enabled):
                    repository=SupabaseReminderRepository(settings)
                    if adapter is None:
                        adapter=NotificationDispatcher(repository,SwitchNotificationSink(settings),IngestionJobs(),settings) if dispatch else ReminderScheduler(repository)
                    adapter.repository=repository
                    if dispatch:adapter.settings=settings;adapter.sink=SwitchNotificationSink(settings)
                    await adapter.tick()
            except asyncio.CancelledError:raise
            except Exception:logging.getLogger('team_memory').warning('Reminder worker unavailable stage=%s','dispatch' if dispatch else 'planning')
            await asyncio.sleep(15)

    async def close(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
