-- SQLite only; applied once by the local schema runner. Never send to PostgreSQL.
ALTER TABLE memories ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS room_members (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    switch_user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('member','approver','admin')),
    created_at TEXT DEFAULT (DATETIME('now')),
    PRIMARY KEY(tenant_id,room_id,switch_user_id),
    FOREIGN KEY(tenant_id,room_id) REFERENCES rooms(tenant_id,id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS profile_message_scope ON messages(tenant_id,room_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS profile_memory_scope ON memories(tenant_id,room_id,id);
CREATE TABLE IF NOT EXISTS person_profiles (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    switch_author_id TEXT NOT NULL CHECK(length(switch_author_id) BETWEEN 1 AND 512),
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
    UNIQUE(tenant_id,room_id,switch_author_id),
    UNIQUE(tenant_id,room_id,id),
    FOREIGN KEY(tenant_id,room_id) REFERENCES rooms(tenant_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS person_profile_claims (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    extraction_key TEXT NOT NULL,
    attribute TEXT NOT NULL CHECK(attribute IN ('responsibility','skill','preference','timezone')),
    value TEXT NOT NULL CHECK(length(value) BETWEEN 1 AND 256),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
    created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
    UNIQUE(profile_id,extraction_key),
    UNIQUE(tenant_id,room_id,id),
    FOREIGN KEY(tenant_id,room_id,profile_id) REFERENCES person_profiles(tenant_id,room_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS person_profile_evidence (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    memory_id TEXT NOT NULL UNIQUE,
    quote TEXT NOT NULL CHECK(length(quote) BETWEEN 1 AND 1000),
    correction INTEGER NOT NULL CHECK(correction IN (0,1)),
    occurred_at TEXT NOT NULL,
    PRIMARY KEY(claim_id,message_id),
    FOREIGN KEY(tenant_id,room_id,claim_id) REFERENCES person_profile_claims(tenant_id,room_id,id) ON DELETE CASCADE,
    FOREIGN KEY(tenant_id,room_id,message_id) REFERENCES messages(tenant_id,room_id,id) ON DELETE CASCADE,
    FOREIGN KEY(tenant_id,room_id,memory_id) REFERENCES memories(tenant_id,room_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS profile_evidence_message ON person_profile_evidence(message_id);
CREATE INDEX IF NOT EXISTS profile_claim_attribute ON person_profile_claims(profile_id,attribute);
CREATE VIEW IF NOT EXISTS person_profile_context AS
SELECT e.*,c.profile_id,c.attribute,c.value,c.status,p.display_name
FROM person_profile_evidence e JOIN person_profile_claims c ON c.id=e.claim_id
JOIN person_profiles p ON p.id=c.profile_id;
CREATE TABLE IF NOT EXISTS profile_extractions (
    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
    counts TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS refresh_profile_insert AFTER INSERT ON person_profile_evidence BEGIN 
    UPDATE person_profile_claims SET status=CASE WHEN EXISTS(
      SELECT 1 FROM person_profile_evidence e WHERE e.claim_id=person_profile_claims.id
      AND e.occurred_at >= COALESCE((SELECT MAX(x.occurred_at) FROM person_profile_evidence x
        JOIN person_profile_claims c ON c.id=x.claim_id
        WHERE c.profile_id=person_profile_claims.profile_id AND c.attribute=person_profile_claims.attribute
          AND x.correction=1),'')
    ) THEN 'active' ELSE 'superseded' END WHERE profile_id=(SELECT profile_id FROM person_profile_claims WHERE id=NEW.claim_id);
    UPDATE memories SET active=CASE WHEN EXISTS(
      SELECT 1 FROM person_profile_evidence e JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE e.memory_id=memories.id AND c.status='active'
    ) THEN 1 ELSE 0 END WHERE id IN (
      SELECT e.memory_id FROM person_profile_evidence e JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE c.profile_id=(SELECT profile_id FROM person_profile_claims WHERE id=NEW.claim_id));
    UPDATE person_profiles SET display_name=(SELECT m.author_name FROM messages m
      JOIN person_profile_evidence e ON e.message_id=m.id JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE c.profile_id=person_profiles.id ORDER BY e.occurred_at DESC,m.id DESC LIMIT 1)
      WHERE id=(SELECT profile_id FROM person_profile_claims WHERE id=NEW.claim_id) AND EXISTS(SELECT 1 FROM person_profile_evidence e
        JOIN person_profile_claims c ON c.id=e.claim_id WHERE c.profile_id=person_profiles.id);
    DELETE FROM person_profiles WHERE id=(SELECT profile_id FROM person_profile_claims WHERE id=NEW.claim_id)
      AND NOT EXISTS(SELECT 1 FROM person_profile_claims c JOIN person_profile_evidence e ON e.claim_id=c.id
        WHERE c.profile_id=person_profiles.id);
    DELETE FROM person_profile_claims WHERE profile_id=(SELECT profile_id FROM person_profile_claims WHERE id=NEW.claim_id)
      AND NOT EXISTS(SELECT 1 FROM person_profile_evidence e WHERE e.claim_id=person_profile_claims.id);
     END;
CREATE TRIGGER IF NOT EXISTS refresh_profile_delete AFTER DELETE ON person_profile_evidence BEGIN 
    UPDATE person_profile_claims SET status=CASE WHEN EXISTS(
      SELECT 1 FROM person_profile_evidence e WHERE e.claim_id=person_profile_claims.id
      AND e.occurred_at >= COALESCE((SELECT MAX(x.occurred_at) FROM person_profile_evidence x
        JOIN person_profile_claims c ON c.id=x.claim_id
        WHERE c.profile_id=person_profile_claims.profile_id AND c.attribute=person_profile_claims.attribute
          AND x.correction=1),'')
    ) THEN 'active' ELSE 'superseded' END WHERE profile_id=(SELECT profile_id FROM person_profile_claims WHERE id=OLD.claim_id);
    UPDATE memories SET active=CASE WHEN EXISTS(
      SELECT 1 FROM person_profile_evidence e JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE e.memory_id=memories.id AND c.status='active'
    ) THEN 1 ELSE 0 END WHERE id IN (
      SELECT e.memory_id FROM person_profile_evidence e JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE c.profile_id=(SELECT profile_id FROM person_profile_claims WHERE id=OLD.claim_id));
    UPDATE person_profiles SET display_name=(SELECT m.author_name FROM messages m
      JOIN person_profile_evidence e ON e.message_id=m.id JOIN person_profile_claims c ON c.id=e.claim_id
      WHERE c.profile_id=person_profiles.id ORDER BY e.occurred_at DESC,m.id DESC LIMIT 1)
      WHERE id=(SELECT profile_id FROM person_profile_claims WHERE id=OLD.claim_id) AND EXISTS(SELECT 1 FROM person_profile_evidence e
        JOIN person_profile_claims c ON c.id=e.claim_id WHERE c.profile_id=person_profiles.id);
    DELETE FROM person_profiles WHERE id=(SELECT profile_id FROM person_profile_claims WHERE id=OLD.claim_id)
      AND NOT EXISTS(SELECT 1 FROM person_profile_claims c JOIN person_profile_evidence e ON e.claim_id=c.id
        WHERE c.profile_id=person_profiles.id);
    DELETE FROM person_profile_claims WHERE profile_id=(SELECT profile_id FROM person_profile_claims WHERE id=OLD.claim_id)
      AND NOT EXISTS(SELECT 1 FROM person_profile_evidence e WHERE e.claim_id=person_profile_claims.id);
     END;