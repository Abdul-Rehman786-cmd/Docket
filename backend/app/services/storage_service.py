import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.repositories import record_repository, sqlite_records
from app.services.env_service import write_env_variables

logger = logging.getLogger("team_memory")


def compute_cutoff_datetime(
    retention_value: int,
    retention_unit: str,
    cutoff_iso: str | None = None,
) -> datetime | None:
    """Calculate UTC cutoff datetime from timeline settings or explicit ISO string."""
    if cutoff_iso and cutoff_iso.strip():
        raw = cutoff_iso.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid cutoff timestamp: {cutoff_iso}") from exc

    unit = (retention_unit or "days").strip().lower()
    if unit == "indefinite":
        return None
    if retention_value <= 0:
        return datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    if unit == "minutes":
        return now - timedelta(minutes=retention_value)
    if unit == "hours":
        return now - timedelta(hours=retention_value)
    if unit == "days":
        return now - timedelta(days=retention_value)
    if unit == "weeks":
        return now - timedelta(weeks=retention_value)
    if unit == "months":
        return now - timedelta(days=retention_value * 30)
    if unit == "years":
        return now - timedelta(days=retention_value * 365)
    raise HTTPException(422, f"Unsupported retention unit: {retention_unit}")


def get_storage_status(settings: Settings | None = None) -> dict[str, Any]:
    """Return active storage configuration, SQLite disk usage, and quota metrics."""
    active = settings or get_settings()
    unit = (active.sqlite_max_space_unit or "MB").strip().upper()
    multiplier = 1024 * 1024 * 1024 if unit == "GB" else (1024 if unit == "KB" else 1024 * 1024)
    max_bytes = int(active.sqlite_max_space_value * multiplier)

    sqlite_stats = sqlite_records.get_sqlite_storage_stats(active)
    return {
        "active_engine": active.effective_database_engine,
        "retention_value": active.storage_retention_value,
        "retention_unit": active.storage_retention_unit,
        "sqlite_max_space_enabled": active.sqlite_max_space_enabled,
        "sqlite_max_space_value": active.sqlite_max_space_value,
        "sqlite_max_space_unit": unit,
        "sqlite_max_bytes": max_bytes,
        "sqlite": sqlite_stats,
    }


async def prune_data_by_timeline(
    *,
    engine: str = "active",
    retention_value: int = 90,
    retention_unit: str = "days",
    cutoff_iso: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Delete all data older than the chosen timeline in either Supabase or SQLite."""
    active = settings or get_settings()
    target_engine = active.effective_database_engine if engine == "active" else engine

    cutoff = compute_cutoff_datetime(retention_value, retention_unit, cutoff_iso)
    if cutoff is None:
        return {
            "status": "skipped",
            "reason": "indefinite_retention",
            "engine": target_engine,
            "deleted": {"memories": 0, "commitments": 0, "decisions": 0, "messages": 0, "documents": 0},
        }

    deleted = await record_repository.delete_records_older_than(
        cutoff,
        engine=target_engine,
        settings=active,
    )

    stats = sqlite_records.get_sqlite_storage_stats(active) if target_engine == "sqlite" else None
    return {
        "status": "ok",
        "engine": target_engine,
        "cutoff": cutoff.isoformat(),
        "deleted": deleted,
        "sqlite": stats,
    }


def configure_and_enforce_sqlite_space(
    *,
    max_space_value: float,
    max_space_unit: str = "MB",
    enabled: bool = True,
    persist_env: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Persist SQLite maximum space setting and immediately delete oldest records if exceeded."""
    unit = (max_space_unit or "MB").strip().upper()
    if unit not in ("KB", "MB", "GB"):
        raise HTTPException(422, f"Unsupported space unit: {max_space_unit}")
    if max_space_value <= 0:
        raise HTTPException(422, "Maximum space must be greater than 0")

    if persist_env and settings is None:
        write_env_variables(
            {
                "SQLITE_MAX_SPACE_ENABLED": "true" if enabled else "false",
                "SQLITE_MAX_SPACE_VALUE": str(max_space_value),
                "SQLITE_MAX_SPACE_UNIT": unit,
            }
        )

    active = settings or get_settings()
    multiplier = 1024 * 1024 * 1024 if unit == "GB" else (1024 if unit == "KB" else 1024 * 1024)
    max_bytes = int(max_space_value * multiplier)

    if not enabled:
        stats = sqlite_records.get_sqlite_storage_stats(active)
        return {
            "status": "ok",
            "enabled": False,
            "max_bytes": max_bytes,
            "result": {
                "enforced": False,
                "used_bytes": stats.get("used_bytes", 0),
                "file_bytes": stats.get("file_bytes", 0),
                "max_bytes": max_bytes,
            },
            "sqlite": stats,
        }

    if not active.sqlite_configured:
        raise HTTPException(
            503,
            "SQLite storage path is not configured. Please select a SQLite directory first.",
        )

    enforcement = sqlite_records.enforce_sqlite_max_space(max_bytes, active)
    stats = sqlite_records.get_sqlite_storage_stats(active)
    return {
        "status": "ok",
        "enabled": True,
        "max_bytes": max_bytes,
        "result": enforcement,
        "sqlite": stats,
    }


class StorageRetentionWorker:
    """Background worker that continuously prunes records older than configured timeline."""

    def __init__(self, on_change: Any = None):
        self.task: asyncio.Task | None = None
        self.on_change = on_change
        self._stop_event = asyncio.Event()

    def start(self):
        if self.task is None or self.task.done():
            self._stop_event.clear()
            self.task = asyncio.create_task(self.run())

    async def process_once(self, settings: Settings | None = None) -> dict[str, Any]:
        active = settings or get_settings()
        unit = (active.storage_retention_unit or "").strip().lower()
        if unit == "indefinite":
            return {"status": "skipped", "reason": "indefinite_retention"}

        engine = active.effective_database_engine
        db_ready = (
            active.sqlite_configured
            if engine == "sqlite"
            else active.supabase_configured
        )
        if not db_ready:
            return {"status": "skipped", "reason": "database_not_configured"}

        res = await prune_data_by_timeline(
            engine=engine,
            retention_value=active.storage_retention_value,
            retention_unit=active.storage_retention_unit,
            settings=active,
        )
        del_dict = res.get("deleted") or {}
        total_deleted = sum(int(v) for v in del_dict.values())
        if total_deleted > 0 and self.on_change:
            try:
                self.on_change({
                    "event": "records_pruned",
                    "engine": engine,
                    "deleted": del_dict,
                })
            except Exception as exc:
                logger.debug("Storage retention worker on_change broadcast warning: %s", exc)
        return res

    async def run(self):
        while not self._stop_event.is_set():
            active = get_settings()
            try:
                await self.process_once(active)
            except Exception as exc:
                logger.warning("Storage retention background prune warning: %s", exc)

            unit = (active.storage_retention_unit or "").strip().lower()
            if unit in ("minutes", "hours"):
                sleep_secs = 60
            elif unit == "indefinite":
                sleep_secs = 300
            else:
                sleep_secs = 300

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_secs)
            except (asyncio.TimeoutError, TimeoutError):
                pass

    async def close(self):
        self._stop_event.set()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


