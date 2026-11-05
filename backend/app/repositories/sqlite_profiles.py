"""SQLite profile storage. Writes run inside the caller's extraction transaction."""

import json
import sqlite3
from uuid import uuid4

from fastapi import HTTPException

from app.services.profile_service import observed_time, profile_items


# Profile/source references are composite even on existing SQLite installations.


def refresh_sql(profile_expression):
    # Source time, not processing order, controls explicit corrections. Equal-time
    # contradictions remain active conflicts. Removing evidence recomputes truth.
    return f"""
    UPDATE person_profile_claims SET status=CASE WHEN EXISTS(
      SELECT 1 FROM person_profile_evidence e WHERE e.claim_id=person_profile_claims.id
      AND e.occurred_at >= COALESCE((SELECT MAX(x.occurred_at) FROM person_profile_evidence x
        JOIN person_profile_claims c ON c.id=x.claim_id
        WHERE c.profile_id=person_profile_claims.profile_id AND c.attribute=person_profile_claims.attribute
          AND x.correction=1),'')
    ) THEN 'active' ELSE 'superseded' END WHERE profile_id={profile_expression};
    UPDATE memories SET active=CASE WHEN EXISTS(
      SELECT 1 FROM person_profile_evidence e JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE e.memory_id=memories.id AND c.status='active'
    ) THEN 1 ELSE 0 END WHERE id IN (
      SELECT e.memory_id FROM person_profile_evidence e JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE c.profile_id={profile_expression});
    UPDATE person_profiles SET display_name=(SELECT m.author_name FROM messages m
      JOIN person_profile_evidence e ON e.message_id=m.id JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE c.profile_id=person_profiles.id ORDER BY e.occurred_at DESC,m.id DESC LIMIT 1)
      WHERE id={profile_expression} AND EXISTS(SELECT 1 FROM person_profile_evidence e
        JOIN person_profile_claims c ON c.id=e.claim_id WHERE c.profile_id=person_profiles.id);
    DELETE FROM person_profiles WHERE id={profile_expression}
      AND NOT EXISTS(SELECT 1 FROM person_profile_claims c JOIN person_profile_evidence e ON e.claim_id=c.id
        WHERE c.profile_id=person_profiles.id);
    DELETE FROM person_profile_claims WHERE profile_id={profile_expression}
      AND NOT EXISTS(SELECT 1 FROM person_profile_evidence e WHERE e.claim_id=person_profile_claims.id);
    """


def initialize(conn):
    from app.core.config import ROOT_DIR
    version = '20261106000200_person_profiles'
    migration = ROOT_DIR / 'supabase' / 'migrations' / 'sqlite' / (version + '.sql')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS local_schema_migrations(version TEXT PRIMARY KEY)')
        if conn.execute('SELECT 1 FROM local_schema_migrations WHERE version=?', (version,)).fetchone():
            return
        statement = ''
        for line in migration.read_text(encoding='utf-8').splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ''
        if statement.strip():
            raise RuntimeError('Incomplete local profile migration')
        conn.execute('INSERT INTO local_schema_migrations VALUES(?)', (version,))


def trigger_statements():
    for event, row in (("INSERT", "NEW"), ("DELETE", "OLD")):
        expression = f"(SELECT profile_id FROM person_profile_claims WHERE id={row}.claim_id)"
        yield (f"CREATE TRIGGER IF NOT EXISTS refresh_profile_{event.lower()} AFTER {event} "
               f"ON person_profile_evidence BEGIN {refresh_sql(expression)} END;")


def store(conn, settings, *, tenant_id, room_id, message_id, author_name, body, metadata, occurred_at, facts, memories):
    checked = profile_items(facts, settings, metadata, body)
    sender = metadata["switch_author_id"]
    when = observed_time(occurred_at)
    conn.execute("INSERT INTO person_profiles(id,tenant_id,room_id,switch_author_id,display_name) "
                 "VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,room_id,switch_author_id) DO NOTHING",
                 (str(uuid4()), tenant_id, room_id, sender, author_name))
    profile_id = conn.execute("SELECT id FROM person_profiles WHERE tenant_id=? AND room_id=? AND switch_author_id=?",
                              (tenant_id, room_id, sender)).fetchone()[0]
    for fact in checked:
        conn.execute("INSERT INTO person_profile_claims(id,tenant_id,room_id,profile_id,extraction_key,attribute,value) "
                     "VALUES(?,?,?,?,?,?,?) ON CONFLICT(profile_id,extraction_key) DO NOTHING",
                     (str(uuid4()), tenant_id, room_id, profile_id, fact['extraction_key'], fact['attribute'], fact['value']))
        claim_id = conn.execute("SELECT id FROM person_profile_claims WHERE profile_id=? AND extraction_key=?",
                                (profile_id, fact['extraction_key'])).fetchone()[0]
        memory_id = memories.get(fact['extraction_key'])
        if not memory_id:
            raise HTTPException(422, "Profile summary is missing")
        conn.execute("INSERT INTO person_profile_evidence(tenant_id,room_id,claim_id,message_id,memory_id,quote,correction,occurred_at) "
                     "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(claim_id,message_id) DO NOTHING",
                     (tenant_id, room_id, claim_id, message_id, memory_id, fact['evidence'], int(fact['correction']), when))
    # Name is the label attached to the newest retained evidence, never identity.
    conn.execute("UPDATE person_profiles SET display_name=(SELECT m.author_name FROM messages m "
                 "JOIN person_profile_evidence e ON e.message_id=m.id JOIN person_profile_claims c ON c.id=e.claim_id "
                 "WHERE c.profile_id=? ORDER BY e.occurred_at DESC,m.id DESC LIMIT 1) WHERE id=?", (profile_id, profile_id))


def replay_hash(items, metadata, body, occurred_at):
    import hashlib
    content = [items, metadata.get('switch_author_id'), body, observed_time(occurred_at)]
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
