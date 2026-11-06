"""Service for managing reminder settings and room reminder policies."""
import logging
from typing import Literal

from app.core.config import get_settings
from app.services.env_service import write_env_variables
from app.repositories.sqlite_db import get_sqlite_connection

logger = logging.getLogger("uvicorn.error")


def calculate_lead_minutes(lead_time: int, lead_unit: str) -> int:
    unit = (lead_unit or "minutes").lower()
    val = max(0, lead_time)
    if unit == "hours":
        mins = val * 60
    elif unit == "days":
        mins = val * 1440
    else:
        mins = val
    return min(10080, mins)


async def configure_reminders(
    enabled: bool,
    lead_time: int,
    lead_unit: Literal["minutes", "hours", "days"] = "minutes",
    lead_minutes: int | None = None,
    delivery_enabled: bool = True,
) -> dict:
    computed_minutes = lead_minutes if lead_minutes is not None else calculate_lead_minutes(lead_time, lead_unit)
    computed_minutes = min(10080, max(0, computed_minutes))

    # 1. Update .env variables
    env_updates = {
        "REMINDERS_ENABLED": "true" if enabled else "false",
        "REMINDER_DELIVERY_ENABLED": "true" if (enabled and delivery_enabled) else "false",
        "REMINDER_LEAD_TIME": str(lead_time),
        "REMINDER_LEAD_UNIT": lead_unit,
        "REMINDER_LEAD_MINUTES": str(computed_minutes),
    }
    write_env_variables(env_updates)
    settings = get_settings()

    # 2. Update SQLite room policies if SQLite configured
    if settings.sqlite_configured:
        try:
            with get_sqlite_connection(settings) as conn:
                with conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT tenant_id, id FROM rooms")
                    rooms = cursor.fetchall()
                    tz = settings.commitment_timezone or "UTC"
                    for r in rooms:
                        cursor.execute(
                            """
                            INSERT INTO commitment_chat_policies (
                                tenant_id, room_id, reminders_enabled, reminder_lead_minutes, timezone
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(tenant_id, room_id) DO UPDATE SET
                                reminders_enabled = excluded.reminders_enabled,
                                reminder_lead_minutes = excluded.reminder_lead_minutes,
                                timezone = excluded.timezone,
                                reminder_policy_version = reminder_policy_version + 1
                            """,
                            (r["tenant_id"], r["id"], 1 if enabled else 0, computed_minutes, tz),
                        )
            logger.info("Updated SQLite reminder policies enabled=%s lead_minutes=%d", enabled, computed_minutes)
        except Exception as exc:
            logger.warning("Could not update SQLite reminder policies: %s", exc)

    # 3. Update Supabase room policies if Supabase configured
    if settings.supabase_configured and settings.switch_tenant_id and settings.allowed_switch_rooms:
        from app.repositories.record_repository import configure_supabase_reminder_policy
        try:
            await configure_supabase_reminder_policy(settings, enabled, computed_minutes)
        except Exception as exc:
            logger.warning("Could not update Supabase reminder policies: %s", exc)

    return {
        "status": "ok",
        "enabled": enabled,
        "lead_time": lead_time,
        "lead_unit": lead_unit,
        "lead_minutes": computed_minutes,
        "delivery_enabled": enabled and delivery_enabled,
    }

