BEGIN;

-- ============================================================
-- TEAM MEMORY: ONE-TIME FOUNDATION RESET
--
-- Deletes existing data in these eight tables, including vector memories.
-- Intended only for the current disposable development database.
-- Never rerun against real team data.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS extensions;

CREATE EXTENSION IF NOT EXISTS vector
WITH SCHEMA extensions;

-- Fail clearly if an existing vector installation uses another schema.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_extension AS e
        JOIN pg_catalog.pg_namespace AS n ON n.oid = e.extnamespace
        WHERE e.extname = 'vector' AND n.nspname = 'extensions'
    ) THEN
        RAISE EXCEPTION 'The vector extension must be installed in the extensions schema';
    END IF;
END;
$$;

-- Remove the previous search definition before resetting its source table.
DROP FUNCTION IF EXISTS public.search_memories(
    UUID, UUID, extensions.vector, INTEGER
);

DROP TABLE IF EXISTS
    public.memories,
    public.reminder_deliveries,
    public.room_members,
    public.commitments,
    public.decisions,
    public.messages,
    public.rooms,
    public.tenants;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- 1. TEAMS AND SWITCH ROOMS
-- ============================================================

CREATE TABLE public.tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.rooms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL
        REFERENCES public.tenants(id) ON DELETE RESTRICT,

    switch_room_id TEXT NOT NULL UNIQUE
        CHECK (btrim(switch_room_id) <> ''),

    name TEXT,
    platform TEXT
        CHECK (platform IN ('discord', 'slack', 'teams')),
    workspace_id TEXT,
    channel_id TEXT,

    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, id)
);

-- Backend-managed room permissions.
-- Populate only from trusted onboarding or admin actions.
CREATE TABLE public.room_members (
    tenant_id UUID NOT NULL,
    room_id UUID NOT NULL,
    switch_user_id TEXT NOT NULL
        CHECK (btrim(switch_user_id) <> ''),

    role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('member', 'approver', 'admin')),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, room_id, switch_user_id),

    FOREIGN KEY (tenant_id, room_id)
        REFERENCES public.rooms(tenant_id, id)
        ON DELETE RESTRICT
);

-- ============================================================
-- 2. MESSAGES AND EXTRACTION PROCESSING
-- ============================================================

CREATE TABLE public.messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    room_id UUID NOT NULL,

    switch_message_id TEXT NOT NULL
        CHECK (btrim(switch_message_id) <> ''),
    switch_author_id TEXT,

    platform TEXT
        CHECK (platform IN ('discord', 'slack', 'teams')),
    workspace_id TEXT,
    channel_id TEXT,
    external_message_id TEXT,
    external_author_id TEXT,
    thread_id TEXT,

    author_name TEXT,
    body TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    extraction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            extraction_status IN (
                'pending', 'processing', 'completed', 'failed'
            )
        ),

    extraction_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (extraction_attempts >= 0),
    extraction_next_attempt_at TIMESTAMPTZ DEFAULT now(),
    extraction_lease_until TIMESTAMPTZ,
    extraction_claim_token UUID,
    extraction_completed_at TIMESTAMPTZ,

    -- Store sanitized error codes, never credentials or raw errors.
    extraction_error_code TEXT,

    FOREIGN KEY (tenant_id, room_id)
        REFERENCES public.rooms(tenant_id, id)
        ON DELETE RESTRICT,

    UNIQUE (tenant_id, room_id, switch_message_id),
    UNIQUE (tenant_id, room_id, id),

    CHECK (
        extraction_status <> 'completed'
        OR extraction_completed_at IS NOT NULL
    )
);

CREATE INDEX idx_messages_room_time
    ON public.messages (tenant_id, room_id, occurred_at DESC);

CREATE INDEX idx_messages_extraction_queue
    ON public.messages (extraction_next_attempt_at)
    WHERE extraction_status IN ('pending', 'failed');

CREATE INDEX idx_messages_extraction_lease
    ON public.messages (extraction_lease_until)
    WHERE extraction_status = 'processing';

-- Switch message IDs are the primary deduplication identity.
-- Original platform IDs are retained for evidence and lookups.
CREATE INDEX idx_messages_external_source
    ON public.messages (
        platform, workspace_id, channel_id, external_message_id
    );

-- ============================================================
-- 3. COMMITMENTS
-- ============================================================

CREATE TABLE public.commitments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    room_id UUID NOT NULL,
    message_id UUID NOT NULL,

    -- Stable item key within a message's extraction result.
    extraction_key TEXT NOT NULL
        CHECK (btrim(extraction_key) <> ''),

    description TEXT NOT NULL,
    owner_name TEXT,
    owner_external_id TEXT,
    owner_switch_user_id TEXT,
    due_at TIMESTAMPTZ,

    approval_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            approval_status IN ('pending', 'approved', 'rejected')
        ),

    approval_by_switch_user_id TEXT,
    approval_at TIMESTAMPTZ,

    task_status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (
            task_status IN (
                'not_started', 'in_progress', 'completed', 'cancelled'
            )
        ),

    completed_at TIMESTAMPTZ,
    next_reminder_at TIMESTAMPTZ,
    last_chased_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    FOREIGN KEY (tenant_id, room_id, message_id)
        REFERENCES public.messages(tenant_id, room_id, id)
        ON DELETE RESTRICT,

    UNIQUE (message_id, extraction_key),
    UNIQUE (tenant_id, room_id, id),

    CHECK (
        (task_status = 'completed' AND completed_at IS NOT NULL)
        OR
        (task_status <> 'completed' AND completed_at IS NULL)
    ),

    CHECK (
        approval_status = 'pending'
        OR (
            approval_by_switch_user_id IS NOT NULL
            AND approval_at IS NOT NULL
        )
    )
);

CREATE INDEX idx_commitments_room_created
    ON public.commitments (tenant_id, room_id, created_at DESC);

CREATE INDEX idx_commitments_message
    ON public.commitments (tenant_id, room_id, message_id);

CREATE INDEX idx_commitments_owner
    ON public.commitments (
        tenant_id, room_id, owner_switch_user_id
    );

CREATE INDEX idx_commitments_reminder_queue
    ON public.commitments (next_reminder_at)
    WHERE approval_status = 'approved'
      AND task_status IN ('not_started', 'in_progress');

-- ============================================================
-- 4. DECISIONS
-- ============================================================

CREATE TABLE public.decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    room_id UUID NOT NULL,
    message_id UUID NOT NULL,

    extraction_key TEXT NOT NULL
        CHECK (btrim(extraction_key) <> ''),

    topic TEXT,
    summary TEXT NOT NULL,

    approval_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            approval_status IN ('pending', 'approved', 'rejected')
        ),

    approval_by_switch_user_id TEXT,
    approval_at TIMESTAMPTZ,

    decision_status TEXT NOT NULL DEFAULT 'active'
        CHECK (
            decision_status IN ('active', 'superseded', 'revoked')
        ),

    supersedes_decision_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (message_id, extraction_key),
    UNIQUE (tenant_id, room_id, id),

    FOREIGN KEY (tenant_id, room_id, message_id)
        REFERENCES public.messages(tenant_id, room_id, id)
        ON DELETE RESTRICT,

    FOREIGN KEY (tenant_id, room_id, supersedes_decision_id)
        REFERENCES public.decisions(tenant_id, room_id, id)
        ON DELETE RESTRICT,

    CHECK (
        supersedes_decision_id IS NULL
        OR supersedes_decision_id <> id
    ),

    CHECK (
        approval_status = 'pending'
        OR (
            approval_by_switch_user_id IS NOT NULL
            AND approval_at IS NOT NULL
        )
    )
);

CREATE INDEX idx_decisions_room_created
    ON public.decisions (tenant_id, room_id, created_at DESC);

CREATE INDEX idx_decisions_message
    ON public.decisions (tenant_id, room_id, message_id);

CREATE INDEX idx_decisions_supersedes
    ON public.decisions (
        tenant_id, room_id, supersedes_decision_id
    )
    WHERE supersedes_decision_id IS NOT NULL;

-- ============================================================
-- VECTOR MEMORY STORAGE
-- ============================================================

-- Searchable facts and summaries retain their original message evidence.
-- Commitment deadlines/status remain in commitments.
-- Decision approval/status remain in decisions.
-- FastAPI must synchronize these summaries with their structured records.
CREATE TABLE public.memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    room_id UUID NOT NULL,
    message_id UUID NOT NULL,

    memory_type TEXT NOT NULL
        CHECK (memory_type IN ('fact', 'decision', 'commitment')),
    extraction_key TEXT NOT NULL
        CHECK (btrim(extraction_key) <> ''),
    content TEXT NOT NULL CHECK (btrim(content) <> ''),

    -- Generate embeddings in the backend, using the same model for queries.
    -- NULL embeddings are allowed while generation is pending.
    embedding extensions.vector(384),
    embedding_model TEXT NOT NULL DEFAULT 'Supabase/gte-small'
        CHECK (embedding_model = 'Supabase/gte-small'),

    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    FOREIGN KEY (tenant_id, room_id, message_id)
        REFERENCES public.messages(tenant_id, room_id, id)
        ON DELETE RESTRICT,

    UNIQUE (message_id, memory_type, extraction_key)
);

CREATE INDEX idx_memories_room
    ON public.memories (tenant_id, room_id);

-- An edited memory must not be searched using its previous text's embedding.
-- The backend regenerates the embedding after updating content.
CREATE OR REPLACE FUNCTION public.clear_stale_memory_embedding()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $$
BEGIN
    IF NEW.content IS DISTINCT FROM OLD.content THEN
        NEW.embedding = NULL;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER memories_clear_stale_embedding
BEFORE UPDATE OF content ON public.memories
FOR EACH ROW EXECUTE FUNCTION public.clear_stale_memory_embedding();

-- Exact vector search is sufficient for the initial small dataset.
-- Add an approximate vector index only after measuring search performance.
CREATE FUNCTION public.search_memories(
    p_tenant_id UUID,
    p_room_id UUID,
    p_embedding extensions.vector(384),
    p_limit INTEGER DEFAULT 5
)
RETURNS TABLE (
    id UUID,
    message_id UUID,
    memory_type TEXT,
    content TEXT,
    similarity DOUBLE PRECISION
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
    SELECT
        m.id,
        m.message_id,
        m.memory_type,
        m.content,
        1 - (
            m.embedding OPERATOR(extensions.<=>) p_embedding
        ) AS similarity
    FROM public.memories AS m
    WHERE p_embedding IS NOT NULL
      AND m.tenant_id = p_tenant_id
      AND m.room_id = p_room_id
      AND m.active = true
      AND m.embedding IS NOT NULL
    ORDER BY
        m.embedding OPERATOR(extensions.<=>) p_embedding
    LIMIT greatest(1, least(coalesce(p_limit, 5), 50));
$$;

-- ============================================================
-- 5. REMINDER DELIVERY TRACKING
-- ============================================================

CREATE TABLE public.reminder_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    room_id UUID NOT NULL,
    commitment_id UUID NOT NULL,

    scheduled_for TIMESTAMPTZ NOT NULL,

    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN ('pending', 'sending', 'sent', 'failed')
        ),

    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ DEFAULT now(),
    lease_until TIMESTAMPTZ,
    claim_token UUID,

    switch_reply_message_id TEXT,
    sent_at TIMESTAMPTZ,
    error_code TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (commitment_id, scheduled_for),

    FOREIGN KEY (tenant_id, room_id, commitment_id)
        REFERENCES public.commitments(tenant_id, room_id, id)
        ON DELETE RESTRICT,

    CHECK (status <> 'sent' OR sent_at IS NOT NULL)
);

CREATE INDEX idx_reminder_deliveries_queue
    ON public.reminder_deliveries (next_attempt_at)
    WHERE status IN ('pending', 'failed');

CREATE INDEX idx_reminder_deliveries_lease
    ON public.reminder_deliveries (lease_until)
    WHERE status = 'sending';

CREATE INDEX idx_reminder_deliveries_room
    ON public.reminder_deliveries (tenant_id, room_id);

-- ============================================================
-- 6. AUTOMATIC UPDATE TIMESTAMPS
-- ============================================================

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER commitments_set_updated_at
BEFORE UPDATE ON public.commitments
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER decisions_set_updated_at
BEFORE UPDATE ON public.decisions
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER reminder_deliveries_set_updated_at
BEFORE UPDATE ON public.reminder_deliveries
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================
-- 7. BACKEND-ONLY DATABASE ACCESS
-- ============================================================

-- Required even if this migration created the extensions schema itself.
GRANT USAGE ON SCHEMA extensions TO service_role;

ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rooms ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.room_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.commitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reminder_deliveries ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE
    public.memories,
    public.tenants,
    public.rooms,
    public.room_members,
    public.messages,
    public.commitments,
    public.decisions,
    public.reminder_deliveries
FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    public.memories,
    public.tenants,
    public.rooms,
    public.room_members,
    public.messages,
    public.commitments,
    public.decisions,
    public.reminder_deliveries
TO service_role;

REVOKE ALL ON FUNCTION public.search_memories(
    UUID, UUID, extensions.vector, INTEGER
) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.search_memories(
    UUID, UUID, extensions.vector, INTEGER
) TO service_role;

COMMIT;

-- Expect eight rows with rls_enabled = true.
SELECT tablename, rowsecurity AS rls_enabled
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
      'tenants',
      'rooms',
      'room_members',
      'messages',
      'memories',
      'commitments',
      'decisions',
      'reminder_deliveries'
  )
ORDER BY tablename;

-- Expect true. Function argument identity does not include vector dimensions.
SELECT to_regprocedure(
    'public.search_memories(uuid,uuid,extensions.vector,integer)'
) IS NOT NULL AS search_function_exists;
