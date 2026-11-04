
-- ============================================================
-- TEAM MEMORY AGENT
-- Supabase PostgreSQL Schema — MVP v1
--
-- Architecture:
-- Slack / Discord -> Switch -> FastAPI -> Supabase + LLM
--
-- Tables:
-- 1. messages
-- 2. commitments
-- 3. decisions
--
-- Intended access:
-- Trusted FastAPI backend only.
-- No anonymous or authenticated client policies.
-- ============================================================


-- ============================================================
-- 1. EXTENSIONS
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ============================================================
-- 2. MESSAGES
-- Stores incoming Slack / Discord messages with source metadata.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.messages (

    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Messaging platform information
    platform TEXT,
    workspace_id TEXT,
    channel_id TEXT,
    external_message_id TEXT,
    external_author_id TEXT,
    thread_id TEXT,

    -- Human-readable information
    author_name TEXT,
    body TEXT NOT NULL,

    -- Original platform timestamp
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Database creation timestamp
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT messages_platform_check
        CHECK (
            platform IS NULL
            OR platform IN ('slack', 'discord', 'teams')
        )
);


-- ============================================================
-- 3. COMMITMENTS
-- Stores commitments extracted from conversations.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.commitments (

    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Original message evidence
    message_id UUID
        REFERENCES public.messages(id)
        ON DELETE RESTRICT,

    -- Commitment details
    description TEXT NOT NULL,
    owner_name TEXT,
    owner_external_id TEXT,
    due_at TIMESTAMPTZ,

    -- Has the commitment been approved?
    approval_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            approval_status IN (
                'pending',
                'approved',
                'rejected'
            )
        ),

    -- Actual task progress
    task_status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (
            task_status IN (
                'not_started',
                'in_progress',
                'completed',
                'cancelled'
            )
        ),

    -- Completion timestamp
    completed_at TIMESTAMPTZ,

    -- Audit timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT commitments_completed_at_check
        CHECK (
            completed_at IS NULL
            OR task_status = 'completed'
        )
);


-- ============================================================
-- 4. DECISIONS
-- Stores team decisions and their original evidence.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.decisions (

    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Original message evidence
    message_id UUID
        REFERENCES public.messages(id)
        ON DELETE RESTRICT,

    -- Decision details
    topic TEXT,
    summary TEXT NOT NULL,

    -- Approval workflow
    approval_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            approval_status IN (
                'pending',
                'approved',
                'rejected'
            )
        ),

    -- Whether this decision is still current
    decision_status TEXT NOT NULL DEFAULT 'active'
        CHECK (
            decision_status IN (
                'active',
                'superseded',
                'revoked'
            )
        ),

    -- Optional link to an older decision
    supersedes_decision_id UUID
        REFERENCES public.decisions(id)
        ON DELETE SET NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT decisions_no_self_supersede
        CHECK (
            supersedes_decision_id IS NULL
            OR supersedes_decision_id <> id
        )
);


-- ============================================================
-- 5. INDEXES
-- Improve query performance.
-- ============================================================

-- Prevent duplicate platform messages.
-- NULLS NOT DISTINCT means missing workspace IDs
-- are treated consistently for deduplication.
CREATE UNIQUE INDEX IF NOT EXISTS
    idx_messages_external_unique
ON public.messages (
    platform,
    workspace_id,
    channel_id,
    external_message_id
)
NULLS NOT DISTINCT
WHERE
    platform IS NOT NULL
    AND channel_id IS NOT NULL
    AND external_message_id IS NOT NULL;


-- Find messages by platform and channel
CREATE INDEX IF NOT EXISTS
    idx_messages_platform_channel
ON public.messages (
    platform,
    workspace_id,
    channel_id
);


-- Find messages by original time
CREATE INDEX IF NOT EXISTS
    idx_messages_occurred_at
ON public.messages (occurred_at DESC);


-- Find messages inside a thread
CREATE INDEX IF NOT EXISTS
    idx_messages_thread_id
ON public.messages (thread_id)
WHERE thread_id IS NOT NULL;


-- Commitment deadline queries
CREATE INDEX IF NOT EXISTS
    idx_commitments_due_at
ON public.commitments (due_at);


-- Commitment approval queries
CREATE INDEX IF NOT EXISTS
    idx_commitments_approval_status
ON public.commitments (approval_status);


-- Commitment progress queries
CREATE INDEX IF NOT EXISTS
    idx_commitments_task_status
ON public.commitments (task_status);


-- Commitment ownership queries
CREATE INDEX IF NOT EXISTS
    idx_commitments_owner_external_id
ON public.commitments (owner_external_id);


-- Source message lookups
CREATE INDEX IF NOT EXISTS
    idx_commitments_message_id
ON public.commitments (message_id);


-- Decision approval queries
CREATE INDEX IF NOT EXISTS
    idx_decisions_approval_status
ON public.decisions (approval_status);


-- Decision source lookups
CREATE INDEX IF NOT EXISTS
    idx_decisions_message_id
ON public.decisions (message_id);


-- Current decisions
CREATE INDEX IF NOT EXISTS
    idx_decisions_status
ON public.decisions (decision_status);


-- ============================================================
-- 6. AUTOMATIC UPDATED_AT TRIGGER
-- Keeps timestamps accurate when records change.
-- ============================================================

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


DROP TRIGGER IF EXISTS
    commitments_set_updated_at
ON public.commitments;

CREATE TRIGGER commitments_set_updated_at
BEFORE UPDATE ON public.commitments
FOR EACH ROW
EXECUTE FUNCTION public.set_updated_at();


DROP TRIGGER IF EXISTS
    decisions_set_updated_at
ON public.decisions;

CREATE TRIGGER decisions_set_updated_at
BEFORE UPDATE ON public.decisions
FOR EACH ROW
EXECUTE FUNCTION public.set_updated_at();


-- ============================================================
-- 7. ROW LEVEL SECURITY
-- Restrict direct client access.
-- ============================================================

ALTER TABLE public.messages
ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.commitments
ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.decisions
ENABLE ROW LEVEL SECURITY;


-- No anonymous or authenticated policies are created.
-- Trusted FastAPI code must use server-side credentials.
-- Never expose a Supabase secret/service-role key to clients.


-- ============================================================
-- 8. TABLE / COLUMN DOCUMENTATION
-- ============================================================

COMMENT ON TABLE public.messages IS
'Source messages received through messaging platforms.';

COMMENT ON TABLE public.commitments IS
'AI-extracted team commitments requiring confirmation.';

COMMENT ON TABLE public.decisions IS
'Team decisions with source evidence and approval state.';

COMMENT ON COLUMN public.messages.external_message_id IS
'Original Slack, Discord or Teams message identifier.';

COMMENT ON COLUMN public.messages.thread_id IS
'Original platform conversation or thread identifier.';

COMMENT ON COLUMN public.commitments.approval_status IS
'Confirmation state of the extracted commitment.';

COMMENT ON COLUMN public.commitments.task_status IS
'Actual progress of the committed task.';

COMMENT ON COLUMN public.decisions.supersedes_decision_id IS
'Older decision replaced by this decision.';


-- ============================================================
-- 9. VERIFICATION
-- ============================================================

SELECT
    tablename,
    rowsecurity AS rls_enabled
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
      'messages',
      'commitments',
      'decisions'
  )
ORDER BY tablename;


-- ============================================================
-- END OF SCHEMA
-- ============================================================
