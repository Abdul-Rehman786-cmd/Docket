"""Single-host durable ingestion journal. Contains redacted data, never settings/keys.

Keep backend/.local on persistent, private storage. One API worker owns recovery.
Completed jobs retain only identity/status/counts for replay protection.
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from app.core.config import BACKEND_DIR

JOURNAL_PATH = BACKEND_DIR / ".local" / "ingestion.sqlite3"


class IngestionJobs:
    def __init__(self, path: Path | None = None):
        self.path = path or JOURNAL_PATH

    async def _execute(self, operation):
        def run():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=5) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA secure_delete=ON")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    ref TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT,
                    decision TEXT, counts TEXT, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    error TEXT)""")
                db.execute("CREATE TABLE IF NOT EXISTS posted_replies (fingerprint TEXT PRIMARY KEY)")
                columns={row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
                for name in ('chat_plan','chat_outcome'):
                    if name not in columns:
                        db.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')
                return operation(db)
        return await asyncio.to_thread(run)

    async def accept(self, ref, scope, payload):
        def run(db):
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE ref=?", (ref,)).fetchone()
            if existing:
                return dict(existing), False
            db.execute("INSERT INTO jobs(ref,scope,payload) VALUES(?,?,?)",
                       (ref, json.dumps(scope), json.dumps(payload)))
            return dict(db.execute("SELECT * FROM jobs WHERE ref=?", (ref,)).fetchone()), True
        return await self._execute(run)

    async def get(self, ref):
        return await self._execute(lambda db: (
            dict(row) if (row := db.execute("SELECT * FROM jobs WHERE ref=?", (ref,)).fetchone()) else None
        ))

    async def is_reply(self, fingerprint):
        return await self._execute(lambda db: db.execute(
            "SELECT 1 FROM posted_replies WHERE fingerprint=?", (fingerprint,)
        ).fetchone() is not None)

    async def remember_reply(self, fingerprint):
        def run(db):
            db.execute("INSERT OR IGNORE INTO posted_replies VALUES(?)", (fingerprint,))
            db.execute("DELETE FROM posted_replies WHERE rowid NOT IN (SELECT rowid FROM posted_replies ORDER BY rowid DESC LIMIT 100)")
        await self._execute(run)

    async def recoverable(self, scope, allowed_rooms):
        rooms = sorted(allowed_rooms)
        if not rooms:
            return []
        placeholders = ','.join('?' for _ in rooms)
        return await self._execute(lambda db: [dict(row) for row in db.execute(
            "SELECT * FROM jobs WHERE scope=? AND state<>'done' AND attempts<3 AND retry_at<=? "
            f"AND json_extract(payload,'$.meta.room_id') IN ({placeholders}) ORDER BY rowid LIMIT 4",
            (json.dumps(scope), time.time(), *rooms),
        )])

    async def diagnostics(self):
        return await self._execute(lambda db: [dict(row) for row in db.execute(
            "SELECT ref,state,attempts,error,counts,chat_outcome FROM jobs ORDER BY rowid DESC LIMIT 100"
        )])

    async def retry(self, ref):
        return await self._execute(lambda db: db.execute(
            "UPDATE jobs SET state='pending',attempts=0,retry_at=0,error=NULL WHERE ref=? AND state<>'done'", (ref,)
        ).rowcount)

    async def start(self, ref):
        await self._execute(lambda db: db.execute(
            "UPDATE jobs SET state='running',attempts=attempts+1 WHERE ref=?", (ref,)
        ).rowcount)

    async def extracted(self, ref, decision):
        await self._execute(lambda db: db.execute(
            "UPDATE jobs SET decision=? WHERE ref=?", (decision.model_dump_json(), ref)
        ).rowcount)

    async def stored(self, ref, counts):
        await self._execute(lambda db: db.execute(
            "UPDATE jobs SET counts=? WHERE ref=?", (json.dumps(counts), ref)
        ).rowcount)

    async def finish(self, ref):
        await self._execute(lambda db: db.execute(
            "UPDATE jobs SET state='done',payload=NULL,decision=NULL,chat_plan=NULL,error=NULL WHERE ref=?", (ref,)
        ).rowcount)

    async def chat_prepared(self,ref,plan):
        await self._execute(lambda db: db.execute('UPDATE jobs SET chat_plan=? WHERE ref=?',
            (plan.model_dump_json(),ref)).rowcount)

    async def chat_result(self,ref,outcome):
        await self._execute(lambda db: db.execute('UPDATE jobs SET chat_outcome=? WHERE ref=?',
            (outcome.model_dump_json(),ref)).rowcount)

    async def fail(self, ref, category):
        await self._execute(lambda db: db.execute(
            "UPDATE jobs SET state='failed',error=?,retry_at=? WHERE ref=? AND state<>'done'",
            (category, time.time() + 30, ref),
        ).rowcount)
