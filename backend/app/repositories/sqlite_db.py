import logging
from pathlib import Path
import sqlite3
from typing import Generator
from contextlib import contextmanager
from fastapi import HTTPException

from app.core.config import Settings, get_settings

logger = logging.getLogger("uvicorn.error")

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- 1. Tenants
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    discord_guild_id TEXT UNIQUE,
    name TEXT,
    settings TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (DATETIME('now'))
);

-- 2. Rooms
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    switch_room_id TEXT NOT NULL UNIQUE,
    name TEXT,
    created_at TEXT DEFAULT (DATETIME('now')),
    UNIQUE (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rooms_tenant ON rooms(tenant_id);


-- 3. Messages
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    switch_message_id TEXT NOT NULL UNIQUE,
    switch_author_id TEXT,
    author_name TEXT NOT NULL,
    body TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT DEFAULT (DATETIME('now')),
    FOREIGN KEY(tenant_id, room_id) REFERENCES rooms(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(tenant_id, room_id);
CREATE INDEX IF NOT EXISTS idx_messages_occurred ON messages(occurred_at DESC);

-- 4. Commitments
CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    owner_name TEXT,
    description TEXT NOT NULL,
    due_at TEXT,
    approval_status TEXT NOT NULL DEFAULT 'pending',
    task_status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT DEFAULT (DATETIME('now')),
    FOREIGN KEY(tenant_id, room_id) REFERENCES rooms(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_commitments_scope ON commitments(tenant_id, room_id);

-- 5. Decisions
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    topic TEXT,
    summary TEXT NOT NULL,
    approval_status TEXT NOT NULL DEFAULT 'pending',
    decision_status TEXT NOT NULL DEFAULT 'accepted',
    created_at TEXT DEFAULT (DATETIME('now')),
    FOREIGN KEY(tenant_id, room_id) REFERENCES rooms(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_decisions_scope ON decisions(tenant_id, room_id);

-- 6. Memories & Vector Embeddings
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL CHECK(memory_type IN ('fact', 'decision', 'commitment')),
    content_hash TEXT NOT NULL,
    embedding BLOB,
    claim_token TEXT,
    claim_expires_at TEXT,
    error_code TEXT,
    created_at TEXT DEFAULT (DATETIME('now')),
    FOREIGN KEY(tenant_id, room_id) REFERENCES rooms(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(tenant_id, room_id);
CREATE INDEX IF NOT EXISTS idx_memories_unembedded ON memories(tenant_id, room_id) WHERE embedding IS NULL;

-- 7. Documents (Additive Module)
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('markdown', 'text')),
    title TEXT NOT NULL,
    created_by TEXT NOT NULL,
    latest_version INTEGER NOT NULL DEFAULT 0,
    published_version_id TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (DATETIME('now')),
    UNIQUE (tenant_id, room_id, source_key),
    UNIQUE (tenant_id, room_id, id),
    FOREIGN KEY (tenant_id, room_id) REFERENCES rooms(tenant_id, id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(tenant_id, room_id);

-- 8. Document Versions
CREATE TABLE IF NOT EXISTS document_versions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    version_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    source_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    recipe TEXT NOT NULL,
    model_recipe TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'completed', 'failed')),
    error TEXT,
    created_at TEXT DEFAULT (DATETIME('now')),
    UNIQUE (document_id, version_number),
    UNIQUE (tenant_id, room_id, document_id, id)
);
CREATE INDEX IF NOT EXISTS idx_doc_versions_doc ON document_versions(document_id);

-- 9. Document Chunks & Embeddings
CREATE TABLE IF NOT EXISTS document_chunks (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    section TEXT,
    model_recipe TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    retry_at TEXT DEFAULT (DATETIME('now')),
    lease_until TEXT,
    claim_token TEXT,
    embedding BLOB,
    embedding_content_hash TEXT,
    created_at TEXT DEFAULT (DATETIME('now')),
    UNIQUE (version_id, position)
);
CREATE INDEX IF NOT EXISTS idx_doc_chunks_queue ON document_chunks(retry_at) WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_doc_chunks_search ON document_chunks(tenant_id, room_id, status) WHERE status = 'completed';

-- 10. Document Imports
CREATE TABLE IF NOT EXISTS document_imports (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    import_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
    request_hash TEXT NOT NULL,
    PRIMARY KEY (tenant_id, import_id)
);

-- 11. Commitment Chat Policies (Reminders & Notice Configuration)
CREATE TABLE IF NOT EXISTS commitment_chat_policies (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    reminders_enabled INTEGER NOT NULL DEFAULT 0,
    reminder_policy_version INTEGER NOT NULL DEFAULT 1,
    reminder_lead_minutes INTEGER NOT NULL DEFAULT 30,
    reminder_cadence_minutes INTEGER NOT NULL DEFAULT 1440,
    reminder_max_followups INTEGER NOT NULL DEFAULT 3,
    reminder_gap_minutes INTEGER NOT NULL DEFAULT 30,
    reminder_room_gap_seconds INTEGER NOT NULL DEFAULT 60,
    reminder_quiet_start INTEGER NOT NULL DEFAULT 22,
    reminder_quiet_end INTEGER NOT NULL DEFAULT 8,
    reminder_delivery_mode TEXT NOT NULL DEFAULT 'source_thread',
    timezone TEXT,
    PRIMARY KEY (tenant_id, room_id)
);

-- 12. Reminder Deliveries (Outbox)
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL REFERENCES commitments(id) ON DELETE CASCADE,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    occurrence_key TEXT UNIQUE,
    recipient_id TEXT,
    commitment_version INTEGER DEFAULT 1,
    policy_version INTEGER DEFAULT 1,
    reminder_type TEXT,
    ordinal INTEGER DEFAULT 0,
    occurrence_base TEXT,
    claim_token TEXT,
    lease_until TEXT,
    dispatch_started_at TEXT,
    sent_at TEXT,
    next_attempt_at TEXT,
    switch_reply_message_id TEXT,
    error_code TEXT,
    notification_body TEXT,
    created_at TEXT DEFAULT (DATETIME('now'))
);
CREATE INDEX IF NOT EXISTS idx_reminder_deliv_status ON reminder_deliveries(status, next_attempt_at);
"""


def get_sqlite_file_path(settings: Settings | None = None) -> Path:
    """Resolve user-selected SQLite path. Never use a silent default if unconfigured."""
    active_settings = settings or get_settings()
    raw_path = (active_settings.sqlite_db_path or "").strip()
    if not raw_path:
        logger.error(
            "SQLite database path is unconfigured (SQLITE_DB_PATH is empty). "
            "User must select a storage directory in Settings."
        )
        raise HTTPException(
            status_code=503,
            detail="Database is not configured",
        )

    path = Path(raw_path)
    if not path.is_absolute():
        from app.core.config import ROOT_DIR
        path = ROOT_DIR / path

    if path.is_dir() or (not path.suffix and not path.exists()):
        path = path / "team_memory.db"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Failed to create SQLite storage directory at %s: %s", path.parent, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Cannot create SQLite directory at {path.parent}: {exc}",
        ) from exc

    return path


@contextmanager
def get_sqlite_connection(settings: Settings | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Provide a thread-safe connection to the user-selected SQLite database."""
    db_file = get_sqlite_file_path(settings)
    conn = sqlite3.connect(str(db_file), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    try:
        yield conn
    finally:
        conn.close()


def init_sqlite_db(settings: Settings | None = None) -> None:
    """Initialize schema in the user-selected SQLite database and ensure schema migrations."""
    with get_sqlite_connection(settings) as conn:
        conn.executescript(SCHEMA_SQL)
        from app.repositories.sqlite_profiles import initialize
        initialize(conn)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(commitments);")
        for col in cursor.fetchall():
            if col[1] == "owner_name" and col[3] == 1:
                conn.execute("PRAGMA foreign_keys = OFF;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS commitments_patch (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        room_id TEXT NOT NULL,
                        message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                        owner_name TEXT,
                        description TEXT NOT NULL,
                        due_at TEXT,
                        approval_status TEXT NOT NULL DEFAULT 'pending',
                        task_status TEXT NOT NULL DEFAULT 'open',
                        created_at TEXT DEFAULT (DATETIME('now')),
                        FOREIGN KEY(tenant_id, room_id) REFERENCES rooms(tenant_id, id) ON DELETE CASCADE
                    );
                """)
                conn.execute("INSERT OR REPLACE INTO commitments_patch SELECT * FROM commitments;")
                conn.execute("DROP TABLE commitments;")
                conn.execute("ALTER TABLE commitments_patch RENAME TO commitments;")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_commitments_scope ON commitments(tenant_id, room_id);")
                conn.execute("PRAGMA foreign_keys = ON;")
                break
        conn.commit()
    logger.info("SQLite schema initialized successfully at %s", get_sqlite_file_path(settings))
