"""Staging repository for user confirmation before saving data to database."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from uuid import UUID

from app.core.config import BACKEND_DIR
from app.models.ai import LLMDecision

DB_PATH = BACKEND_DIR / ".local" / "ingestion.sqlite3"


class PendingConfirmationRepository:
    def __init__(self, path: Path | None = None):
        self.path = path or DB_PATH

    async def _execute(self, operation):
        def run():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=5) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA secure_delete=ON")
                db.execute("""CREATE TABLE IF NOT EXISTS pending_confirmations (
                    draft_key TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    thread_id TEXT,
                    switch_message_id TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    body TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    source_metadata_json TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )""")
                return operation(db)
        return await asyncio.to_thread(run)

    async def stage(
        self,
        *,
        draft_key: str,
        tenant_id: UUID | str,
        room_id: str,
        thread_id: str | None,
        switch_message_id: str,
        author_name: str,
        body: str,
        occurred_at: str,
        decision: LLMDecision,
        source_metadata: dict,
        ttl_seconds: int = 300,
    ) -> None:
        expires_at = time.time() + ttl_seconds
        decision_json = decision.model_dump_json()
        source_metadata_json = json.dumps(source_metadata, default=str)

        def run(db):
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT OR REPLACE INTO pending_confirmations (
                    draft_key, tenant_id, room_id, thread_id, switch_message_id,
                    author_name, body, occurred_at, decision_json, source_metadata_json, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    draft_key,
                    str(tenant_id),
                    room_id,
                    thread_id,
                    switch_message_id,
                    author_name,
                    body,
                    occurred_at,
                    decision_json,
                    source_metadata_json,
                    expires_at,
                ),
            )

        await self._execute(run)

    async def get(self, draft_key: str) -> dict | None:
        now = time.time()

        def run(db):
            row = db.execute(
                "SELECT * FROM pending_confirmations WHERE draft_key=?", (draft_key,)
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            if data["expires_at"] < now:
                db.execute("DELETE FROM pending_confirmations WHERE draft_key=?", (draft_key,))
                return None
            return {
                **data,
                "decision": LLMDecision.model_validate_json(data["decision_json"]),
                "source_metadata": json.loads(data["source_metadata_json"]),
            }

        return await self._execute(run)

    async def remove(self, draft_key: str) -> None:
        def run(db):
            db.execute("DELETE FROM pending_confirmations WHERE draft_key=?", (draft_key,))

        await self._execute(run)

