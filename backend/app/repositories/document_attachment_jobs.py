"""Single-host durable metadata/CAS/outbox store. No attachment contents or secrets.

Separate from the message journal. Keep .local private/persistent and run one API
worker. SQLite full-sync commits precede acknowledgement; transitions and outbox
enqueue commit together. Revisions reject stale workers and racing decisions.
"""

import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from app.core.config import BACKEND_DIR

ATTACHMENT_JOURNAL = BACKEND_DIR / ".local" / "document_attachments.sqlite3"


class AttachmentJobs:
    def __init__(self,path=None,clock=time.time):
        self.path=Path(path or ATTACHMENT_JOURNAL)
        self.clock=clock

    async def _execute(self,operation):
        def run():
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(self.path,timeout=5)) as db, db:
                db.row_factory=sqlite3.Row
                db.execute("PRAGMA synchronous=FULL")
                db.executescript("""
                CREATE TABLE IF NOT EXISTS attachment_requests (
                  ref TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                  tenant TEXT NOT NULL, room TEXT NOT NULL, sender TEXT NOT NULL,
                  state TEXT NOT NULL, data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                  expires REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                  lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attachment_outbox (
                  id TEXT PRIMARY KEY, ref TEXT NOT NULL, tenant TEXT NOT NULL, room TEXT NOT NULL,
                  data TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                  retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0, token TEXT);
                CREATE TABLE IF NOT EXISTS attachment_confirmations (
                  identity TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request_ref TEXT, outcome TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attachment_request_consent ON attachment_requests(tenant,room,sender,state);
                CREATE INDEX IF NOT EXISTS attachment_request_queue ON attachment_requests(tenant,room,state,retry_at);
                CREATE INDEX IF NOT EXISTS attachment_outbox_queue ON attachment_outbox(tenant,room,state,retry_at);
                """)
                db.execute("BEGIN IMMEDIATE")
                return operation(db)
        return await asyncio.to_thread(run)

    @staticmethod
    def _row(row):
        return {**dict(row),"data":json.loads(row["data"])} if row else None

    @staticmethod
    def _enqueue(db,row,kind):
        payload={"kind":kind,"body":row["data"]["replies"][kind],"thread_id":None}
        db.execute("INSERT OR IGNORE INTO attachment_outbox(id,ref,tenant,room,data) VALUES(?,?,?,?,?)",
                   (row["ref"]+":"+kind,row["ref"],row["tenant"],row["room"],json.dumps(payload)))

    async def insert(self,row,limit,record_limit=10000):
        def run(db):
            existing=db.execute("SELECT * FROM attachment_requests WHERE event_key=?",(row["event_key"],)).fetchone()
            if existing:
                return self._row(existing),False
            count=db.execute("SELECT count(*) FROM attachment_requests WHERE state IN ('confirmation_requested','queued','processing')").fetchone()[0]
            if count>=limit or db.execute("SELECT count(*) FROM attachment_requests").fetchone()[0]>=record_limit:
                return None,False
            db.execute("INSERT INTO attachment_requests(ref,event_key,tenant,room,sender,state,data,expires) VALUES(?,?,?,?,?,?,?,?)",
                (row["ref"],row["event_key"],row["tenant"],row["room"],row["sender"],row["state"],json.dumps(row["data"]),row["expires"]))
            result=self._row(db.execute("SELECT * FROM attachment_requests WHERE ref=?",(row["ref"],)).fetchone())
            if row["data"].get("ready"):
                self._enqueue(db,result,"prompt" if row["state"]=="confirmation_requested" else "failed")
            return result,True
        return await self._execute(run)

    async def get(self,ref):
        return await self._execute(lambda db:self._row(db.execute("SELECT * FROM attachment_requests WHERE ref=?",(ref,)).fetchone()))

    async def change(self,row,*,state=None,data=None,reply=None,retry_at=0,release=False):
        def run(db):
            result={**row,"state":state or row["state"],"data":data if data is not None else row["data"]}
            updated=db.execute("UPDATE attachment_requests SET state=?,data=?,revision=revision+1,retry_at=?,lease_until=? WHERE ref=? AND revision=?",
                (result["state"],json.dumps(result["data"]),retry_at,0 if release else row["lease_until"],row["ref"],row["revision"])).rowcount
            if not updated:
                return None
            result["revision"]+=1
            result["retry_at"]=retry_at
            result["lease_until"]=0 if release else row["lease_until"]
            if reply:
                self._enqueue(db,result,reply)
            return result
        return await self._execute(run)

    async def decide_once(self,identity,fingerprint,tenant,room,sender,choose,include_ref=None):
        """Select via domain callback while holding the same transaction as decision/outbox/replay binding."""
        def run(db):
            prior=db.execute("SELECT * FROM attachment_confirmations WHERE identity=?",(identity,)).fetchone()
            if prior:
                if prior["fingerprint"]!=fingerprint:
                    return None,"not_authorized",True
                row=self._row(db.execute("SELECT * FROM attachment_requests WHERE ref=?",(prior["request_ref"],)).fetchone())
                return row,prior["outcome"],True
            if db.execute("SELECT count(*) FROM attachment_confirmations").fetchone()[0]>=100000:
                from fastapi import HTTPException
                raise HTTPException(429,"document_confirmation_journal_full")
            candidates=[self._row(r) for r in db.execute(
                "SELECT * FROM attachment_requests WHERE tenant=? AND room=? AND sender=? AND (state='confirmation_requested' OR ref=?)",(str(tenant),room,sender,include_ref))]
            row,outcome,data,reply,clarification=choose(candidates)
            if row and data is not None:
                db.execute("UPDATE attachment_requests SET state=?,data=?,revision=revision+1,lease_until=0,retry_at=0 WHERE ref=? AND revision=?",
                    (outcome,json.dumps(data),row["ref"],row["revision"]))
                row={**row,"state":outcome,"data":data,"revision":row["revision"]+1}
                if reply:
                    self._enqueue(db,row,reply)
            if row and clarification:
                payload=dict(kind="clarify",body=clarification,thread_id=None)
                db.execute("INSERT OR IGNORE INTO attachment_outbox(id,ref,tenant,room,data) VALUES(?,?,?,?,?)",
                    ("confirmation:"+identity,row["ref"],row["tenant"],row["room"],json.dumps(payload)))
            db.execute("INSERT INTO attachment_confirmations(identity,fingerprint,request_ref,outcome) VALUES(?,?,?,?)",
                       (identity,fingerprint,row["ref"] if row else None,outcome))
            return row,outcome,False
        return await self._execute(run)

    async def due(self,tenant,rooms):
        def run(db):
            if not rooms:
                return []
            placeholders=','.join('?' for _ in rooms)
            return [self._row(r) for r in db.execute(
                f"SELECT * FROM attachment_requests WHERE tenant=? AND room IN ({placeholders}) AND state='confirmation_requested' AND expires<=? LIMIT 20",
                (str(tenant),*sorted(rooms),self.clock()))]
        return await self._execute(run)

    async def claim(self,tenant,rooms):
        def run(db):
            if not rooms:
                return None
            placeholders=','.join('?' for _ in rooms)
            row=self._row(db.execute(f"SELECT * FROM attachment_requests WHERE tenant=? AND room IN ({placeholders}) AND state IN ('queued','processing') AND lease_until<=? AND retry_at<=? ORDER BY rowid LIMIT 1",
                (str(tenant),*sorted(rooms),self.clock(),self.clock())).fetchone())
            if not row:
                return None
            attempts=row["attempts"]+(row["data"].get("phase")=="importing")
            db.execute("UPDATE attachment_requests SET state='processing',revision=revision+1,lease_until=?,attempts=? WHERE ref=?",
                       (self.clock()+300,attempts,row["ref"]))
            return self._row(db.execute("SELECT * FROM attachment_requests WHERE ref=?",(row["ref"],)).fetchone())
        return await self._execute(run)

    async def claim_reply(self,tenant,rooms):
        def run(db):
            if not rooms:
                return None
            placeholders=','.join('?' for _ in rooms)
            db.execute(f"UPDATE attachment_outbox SET state='failed' WHERE tenant=? AND room IN ({placeholders}) AND state='inflight' AND attempts>=3 AND lease_until<=?",
                       (str(tenant),*sorted(rooms),self.clock()))
            row=self._row(db.execute(f"SELECT * FROM attachment_outbox WHERE tenant=? AND room IN ({placeholders}) AND state IN ('pending','inflight') AND attempts<3 AND retry_at<=? AND lease_until<=? ORDER BY rowid LIMIT 1",
                (str(tenant),*sorted(rooms),self.clock(),self.clock())).fetchone())
            if not row:
                return None
            token=str(uuid4())
            db.execute("UPDATE attachment_outbox SET state='inflight',attempts=attempts+1,token=?,lease_until=? WHERE id=?",(token,self.clock()+60,row["id"]))
            return self._row(db.execute("SELECT * FROM attachment_outbox WHERE id=?",(row["id"],)).fetchone())
        return await self._execute(run)

    async def finish_reply(self,row,success):
        state="superseded" if success=="superseded" else ("sent" if success else ("failed" if row["attempts"]>=3 else "pending"))
        return await self._execute(lambda db:db.execute("UPDATE attachment_outbox SET state=?,lease_until=0,retry_at=? WHERE id=? AND token=? AND state='inflight'",
            (state,self.clock()+30,row["id"],row["token"])).rowcount)

    async def reply_states(self,ref):
        return await self._execute(lambda db:[r[0] for r in db.execute("SELECT state FROM attachment_outbox WHERE ref=? ORDER BY id",(ref,))])
