-- ADDITIVE PROPOSAL. Review before hosted deployment; never replay the core reset.
-- Requires the reviewed storage + embedding recovery baseline (or equivalent upgrades).
BEGIN;
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='memories'
        AND column_name IN ('content_hash','embedding_content_hash','embedding_status','embedding_claim_token','embedding_lease_until')) <> 5
      OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger
        WHERE tgrelid='public.memories'::regclass AND tgname='memories_clear_stale_embedding' AND NOT tgisinternal AND tgenabled <> 'D') THEN
      RAISE EXCEPTION 'commitment_lifecycle_requires_reviewed_embedding_recovery_upgrade';
    END IF;
END $$;
ALTER TABLE public.commitments ADD COLUMN version bigint NOT NULL DEFAULT 1 CHECK (version > 0);
ALTER TABLE public.commitments ADD COLUMN cancelled_at timestamptz;
ALTER TABLE public.commitments ADD CONSTRAINT commitments_cancelled_at_check
    CHECK (task_status = 'cancelled' OR cancelled_at IS NULL);
-- Existing unresolved/legacy identities are retained. Newly assigned identities are
-- checked under a membership lock by the RPC, without constraining AI extraction.

CREATE TABLE public.commitment_action_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    tenant_id uuid NOT NULL, room_id uuid NOT NULL, commitment_id uuid NOT NULL,
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    action text NOT NULL CHECK (action IN ('approve','reject','start','complete','cancel','reassign','reschedule')),
    occurred_at timestamptz NOT NULL,
    idempotency_key uuid NOT NULL, expected_version bigint NOT NULL CHECK (expected_version > 0),
    changed boolean NOT NULL, before jsonb NOT NULL, after jsonb NOT NULL,
    reason text CHECK (length(reason) <= 1000),
    FOREIGN KEY (tenant_id,room_id,commitment_id) REFERENCES public.commitments(tenant_id,room_id,id) ON DELETE RESTRICT
);
CREATE INDEX commitment_events_scope ON public.commitment_action_events(tenant_id,room_id,commitment_id,sequence);
CREATE TABLE public.commitment_action_receipts (
    tenant_id uuid NOT NULL, room_id uuid NOT NULL, actor_id text NOT NULL,
    idempotency_key uuid NOT NULL, commitment_id uuid NOT NULL,
    request jsonb NOT NULL, result jsonb NOT NULL,
    PRIMARY KEY (tenant_id,room_id,actor_id,idempotency_key),
    FOREIGN KEY (tenant_id,room_id,commitment_id) REFERENCES public.commitments(tenant_id,room_id,id) ON DELETE RESTRICT
);
ALTER TABLE public.commitment_action_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.commitment_action_receipts ENABLE ROW LEVEL SECURITY;
-- Override hosted default privileges too; GRANT alone would retain UPDATE/DELETE.
REVOKE ALL ON public.commitment_action_events, public.commitment_action_receipts FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT ON public.commitment_action_events, public.commitment_action_receipts TO service_role;
REVOKE ALL ON SEQUENCE public.commitment_action_events_sequence_seq FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE public.commitment_action_events_sequence_seq TO service_role;

CREATE FUNCTION public.commitment_summary(c public.commitments) RETURNS text
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT 'Commitment: ' || c.description || E'\nAssignee: ' ||
      coalesce(c.owner_switch_user_id, 'unresolved: ' || c.owner_name, 'unassigned') ||
      E'\nApproval: ' || c.approval_status || E'\nTask: ' || c.task_status ||
      E'\nDue (UTC): ' || coalesce(to_char(c.due_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'unscheduled');
$$;
CREATE FUNCTION public.commitment_reminder(c public.commitments) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT jsonb_build_object('eligible', reason = 'eligible', 'reason',reason,
                             'due_at',c.due_at,'assignee_id',c.owner_switch_user_id)
    FROM (SELECT CASE WHEN c.approval_status <> 'approved' THEN c.approval_status
        WHEN c.task_status IN ('completed','cancelled') THEN c.task_status
        WHEN c.owner_switch_user_id IS NULL THEN 'unassigned'
        WHEN c.due_at IS NULL THEN 'no_deadline' ELSE 'eligible' END reason) s;
$$;
CREATE FUNCTION public.commitment_snapshot(c public.commitments) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT jsonb_build_object('approval_status',c.approval_status,'approval_by_switch_user_id',c.approval_by_switch_user_id,
      'approval_at',c.approval_at,'task_status',c.task_status,'owner_switch_user_id',c.owner_switch_user_id,
      'owner_name',c.owner_name,'due_at',c.due_at,'completed_at',c.completed_at,'cancelled_at',c.cancelled_at,
      'next_reminder_at',c.next_reminder_at,'version',c.version);
$$;

CREATE FUNCTION public.read_commitment_lifecycle(
    p_tenant_id uuid, p_room_id uuid, p_actor_id text, p_mode text,
    p_commitment_id uuid DEFAULT NULL, p_options jsonb DEFAULT '{}'
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    c public.commitments; member_role text; receipt public.commitment_action_receipts;
    result jsonb; page_limit integer; page_offset integer; total integer;
BEGIN
    SELECT m.role INTO member_role FROM public.room_members m JOIN public.rooms r
      ON r.tenant_id=m.tenant_id AND r.id=m.room_id
      WHERE m.tenant_id=p_tenant_id AND m.room_id=p_room_id AND m.switch_user_id=p_actor_id AND r.enabled;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT404', MESSAGE='not_found'; END IF;
    IF p_mode <> 'list' THEN
      SELECT * INTO c FROM public.commitments WHERE tenant_id=p_tenant_id AND room_id=p_room_id AND id=p_commitment_id;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT404', MESSAGE='not_found'; END IF;
    END IF;
    IF p_mode = 'context' THEN
      SELECT * INTO receipt FROM public.commitment_action_receipts WHERE tenant_id=p_tenant_id AND room_id=p_room_id
        AND actor_id=p_actor_id AND idempotency_key=(p_options->>'idempotency_key')::uuid;
      RETURN jsonb_build_object('commitment',to_jsonb(c),'role',member_role,
        'assignee_valid',EXISTS(SELECT 1 FROM public.room_members WHERE tenant_id=p_tenant_id AND room_id=p_room_id
             AND switch_user_id=p_options->>'assignee_id'),
        'previous_request',receipt.request,'previous_result',receipt.result);
    END IF;
    page_limit := coalesce((p_options->>'limit')::integer,25);
    page_offset := coalesce((p_options->>'offset')::integer,0);
    IF page_limit NOT BETWEEN 1 AND 100 OR page_offset NOT BETWEEN 0 AND 10000 THEN
      RAISE EXCEPTION USING ERRCODE='PT422', MESSAGE='invalid_action_payload';
    END IF;
    IF p_mode = 'list' THEN
      SELECT coalesce(jsonb_agg(to_jsonb(page) ORDER BY page.created_at DESC,page.id DESC),'[]'::jsonb),count(*)
      INTO result,total FROM (
        SELECT * FROM public.commitments WHERE tenant_id=p_tenant_id AND room_id=p_room_id
          AND (p_options->>'approval_status' IS NULL OR approval_status=p_options->>'approval_status')
          AND (p_options->>'task_status' IS NULL OR task_status=p_options->>'task_status')
          AND (p_options->>'assignee_id' IS NULL OR owner_switch_user_id=p_options->>'assignee_id')
          AND (p_options->>'due_from' IS NULL OR due_at >= (p_options->>'due_from')::timestamptz)
          AND (p_options->>'due_to' IS NULL OR due_at <= (p_options->>'due_to')::timestamptz)
        ORDER BY created_at DESC,id DESC LIMIT page_limit+1 OFFSET page_offset
      ) page;
    ELSIF p_mode = 'history' THEN
      SELECT coalesce(jsonb_agg(to_jsonb(page) ORDER BY page.sequence),'[]'::jsonb),count(*) INTO result,total FROM (
        SELECT * FROM public.commitment_action_events WHERE tenant_id=p_tenant_id AND room_id=p_room_id AND commitment_id=p_commitment_id
          ORDER BY sequence LIMIT page_limit+1 OFFSET page_offset
      ) page;
    ELSE RAISE EXCEPTION USING ERRCODE='PT422', MESSAGE='invalid_action_payload';
    END IF;
    IF total > page_limit THEN result := result - page_limit; END IF;
    RETURN jsonb_build_object('items',result,'next_offset',CASE WHEN total>page_limit THEN page_offset+page_limit END);
END;
$$;

CREATE FUNCTION public.apply_commitment_action(
    p_tenant_id uuid, p_room_id uuid, p_actor_id text, p_commitment_id uuid,
    p_action text, p_expected_version bigint, p_idempotency_key uuid, p_payload jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    c public.commitments; old_c public.commitments; member_role text;
    receipt public.commitment_action_receipts; event public.commitment_action_events;
    request_content jsonb; result jsonb; changed boolean := false;
    action_time timestamptz := transaction_timestamp(); new_due timestamptz;
BEGIN
    IF p_action IS NULL OR p_action NOT IN ('approve','reject','start','complete','cancel','reassign','reschedule')
      OR p_expected_version IS NULL OR p_expected_version < 1 OR p_idempotency_key IS NULL
      OR p_actor_id IS NULL OR btrim(p_actor_id) = '' OR jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
      OR (p_payload - 'reason' - CASE WHEN p_action='reassign' THEN 'assignee_id' WHEN p_action='reschedule' THEN 'due_at' ELSE 'reason' END) <> '{}'
      OR (p_payload->'reason' IS NOT NULL AND jsonb_typeof(p_payload->'reason') NOT IN ('string','null'))
      OR length(p_payload->>'reason') > 1000 THEN
      RAISE EXCEPTION USING ERRCODE='PT422', MESSAGE='invalid_action_payload';
    END IF;
    -- Serialize the same scoped key even if concurrent requests name different commitments.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_tenant_id::text || '/' || p_room_id::text || '/' || p_actor_id || '/' || p_idempotency_key::text,0));
    PERFORM 1 FROM public.rooms WHERE tenant_id=p_tenant_id AND id=p_room_id AND enabled FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT404', MESSAGE='not_found'; END IF;
    SELECT role INTO member_role FROM public.room_members WHERE tenant_id=p_tenant_id AND room_id=p_room_id
      AND switch_user_id=p_actor_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT404', MESSAGE='not_found'; END IF;
    SELECT * INTO c FROM public.commitments WHERE tenant_id=p_tenant_id AND room_id=p_room_id AND id=p_commitment_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT404', MESSAGE='not_found'; END IF;
    IF (p_action IN ('approve','reject','cancel','reassign','reschedule') AND member_role NOT IN ('approver','admin'))
      OR (p_action IN ('start','complete') AND c.owner_switch_user_id IS DISTINCT FROM p_actor_id) THEN
      RAISE EXCEPTION USING ERRCODE='PT403', MESSAGE='forbidden_action';
    END IF;
    request_content := jsonb_build_object('commitment_id',p_commitment_id,'action',p_action,
                                         'expected_version',p_expected_version,'payload',p_payload);
    SELECT * INTO receipt FROM public.commitment_action_receipts WHERE tenant_id=p_tenant_id AND room_id=p_room_id
      AND actor_id=p_actor_id AND idempotency_key=p_idempotency_key;
    IF FOUND THEN
      IF receipt.request <> request_content THEN RAISE EXCEPTION USING ERRCODE='PT409', MESSAGE='idempotency_conflict'; END IF;
      RETURN receipt.result;
    END IF;
    IF c.version <> p_expected_version THEN RAISE EXCEPTION USING ERRCODE='PT409', MESSAGE='stale_version'; END IF;
    IF (c.task_status IN ('completed','cancelled') AND NOT (p_action='complete' AND c.task_status='completed')
        AND NOT (p_action='cancel' AND c.task_status='cancelled'))
      OR (c.approval_status='rejected' AND p_action <> 'reject') THEN
      RAISE EXCEPTION USING ERRCODE='PT409', MESSAGE='invalid_transition';
    END IF;
    old_c := c;
    CASE p_action
    WHEN 'approve' THEN
      IF c.approval_status='pending' THEN
        IF c.task_status <> 'not_started' THEN RAISE EXCEPTION USING ERRCODE='PT409', MESSAGE='invalid_transition'; END IF;
        c.approval_status:='approved'; c.approval_by_switch_user_id:=p_actor_id; c.approval_at:=action_time;
      END IF;
    WHEN 'reject' THEN
      IF c.approval_status NOT IN ('pending','rejected') OR c.task_status <> 'not_started' THEN
        RAISE EXCEPTION USING ERRCODE='PT409', MESSAGE='invalid_transition'; END IF;
      IF c.approval_status='pending' THEN
        c.approval_status:='rejected'; c.approval_by_switch_user_id:=p_actor_id; c.approval_at:=action_time;
      END IF;
    WHEN 'start','complete' THEN
      IF c.approval_status <> 'approved' THEN RAISE EXCEPTION USING ERRCODE='PT409', MESSAGE='invalid_transition'; END IF;
      IF p_action='start' THEN c.task_status:='in_progress';
      ELSE
        IF c.task_status <> 'completed' THEN c.completed_at:=action_time; END IF;
        c.task_status:='completed';
      END IF;
    WHEN 'cancel' THEN
      IF c.task_status <> 'cancelled' THEN c.task_status:='cancelled'; c.cancelled_at:=action_time; END IF;
    WHEN 'reassign' THEN
      IF jsonb_typeof(p_payload->'assignee_id') IS DISTINCT FROM 'string'
        OR length(p_payload->>'assignee_id') NOT BETWEEN 1 AND 512 OR (p_payload->>'assignee_id') ~ '\s' THEN
        RAISE EXCEPTION USING ERRCODE='PT422', MESSAGE='invalid_assignee'; END IF;
      PERFORM 1 FROM public.room_members WHERE tenant_id=p_tenant_id AND room_id=p_room_id
        AND switch_user_id=p_payload->>'assignee_id' FOR SHARE;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT422', MESSAGE='invalid_assignee'; END IF;
      IF c.owner_switch_user_id IS DISTINCT FROM p_payload->>'assignee_id' THEN
        c.owner_switch_user_id:=p_payload->>'assignee_id'; c.owner_name:=NULL;
      END IF;
    WHEN 'reschedule' THEN
      IF NOT (p_payload ? 'due_at') OR jsonb_typeof(p_payload->'due_at') NOT IN ('null','string') THEN
        RAISE EXCEPTION USING ERRCODE='PT422', MESSAGE='invalid_action_payload'; END IF;
      IF p_payload->>'due_at' IS NOT NULL THEN
        IF (p_payload->>'due_at') !~ '^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$' THEN
          RAISE EXCEPTION USING ERRCODE='PT422', MESSAGE='invalid_action_payload'; END IF;
        BEGIN new_due:=(p_payload->>'due_at')::timestamptz;
        EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
          RAISE EXCEPTION USING ERRCODE='PT422', MESSAGE='invalid_action_payload'; END;
      END IF;
      c.due_at:=new_due;
    END CASE;
    changed := public.commitment_snapshot(c) <> public.commitment_snapshot(old_c);
    IF changed THEN
      UPDATE public.commitments SET approval_status=c.approval_status, approval_by_switch_user_id=c.approval_by_switch_user_id,
        approval_at=c.approval_at, task_status=c.task_status, completed_at=c.completed_at,cancelled_at=c.cancelled_at,
        owner_switch_user_id=c.owner_switch_user_id,owner_name=c.owner_name,due_at=c.due_at,
        next_reminder_at=NULL,version=version+1 WHERE id=c.id AND tenant_id=p_tenant_id AND room_id=p_room_id RETURNING * INTO c;
    END IF;
    -- Source message/extraction keys are immutable. Updating text invokes the existing
    -- embedding invalidation trigger, including clearing in-flight worker claims.
    INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content,active)
      VALUES(c.tenant_id,c.room_id,c.message_id,'commitment',c.extraction_key,public.commitment_summary(c),
        c.approval_status <> 'rejected' AND c.task_status NOT IN ('completed','cancelled'))
      ON CONFLICT(message_id,memory_type,extraction_key) DO UPDATE SET content=EXCLUDED.content,active=EXCLUDED.active;
    INSERT INTO public.commitment_action_events(tenant_id,room_id,commitment_id,actor_id,action,occurred_at,
      idempotency_key,expected_version,changed,before,after,reason)
      VALUES(c.tenant_id,c.room_id,c.id,p_actor_id,p_action,action_time,p_idempotency_key,p_expected_version,changed,
        public.commitment_snapshot(old_c),public.commitment_snapshot(c),p_payload->>'reason') RETURNING * INTO event;
    result:=jsonb_build_object('commitment',to_jsonb(c),'reminder',public.commitment_reminder(c),'event',to_jsonb(event),'changed',changed);
    INSERT INTO public.commitment_action_receipts VALUES(p_tenant_id,p_room_id,p_actor_id,p_idempotency_key,c.id,request_content,result);
    RETURN result;
END;
$$;

-- Repair old searchable summaries without editing source evidence or lifecycle state.
UPDATE public.memories m SET content=public.commitment_summary(c),
    active=c.approval_status <> 'rejected' AND c.task_status NOT IN ('completed','cancelled')
FROM public.commitments c WHERE m.tenant_id=c.tenant_id AND m.room_id=c.room_id AND m.message_id=c.message_id
    AND m.memory_type='commitment' AND m.extraction_key=c.extraction_key;

REVOKE ALL ON FUNCTION public.commitment_summary(public.commitments),public.commitment_reminder(public.commitments),
  public.commitment_snapshot(public.commitments), public.read_commitment_lifecycle(uuid,uuid,text,text,uuid,jsonb),
  public.apply_commitment_action(uuid,uuid,text,uuid,text,bigint,uuid,jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.commitment_summary(public.commitments),public.commitment_reminder(public.commitments),
  public.commitment_snapshot(public.commitments), public.read_commitment_lifecycle(uuid,uuid,text,text,uuid,jsonb),
  public.apply_commitment_action(uuid,uuid,text,uuid,text,bigint,uuid,jsonb) TO service_role;
COMMIT;
