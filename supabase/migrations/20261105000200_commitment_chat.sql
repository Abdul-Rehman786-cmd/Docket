-- Additive proposal; requires reviewed storage/embedding and commitment lifecycle.
-- Backend-only chat execution. Never apply the destructive core reset remotely.
BEGIN;
CREATE TABLE public.commitment_chat_policies (
    tenant_id uuid NOT NULL,room_id uuid NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    timezone text CHECK(timezone IS NULL OR btrim(timezone) <> ''),
    accept_pending_owner_completion boolean NOT NULL DEFAULT false,
    handoff text NOT NULL DEFAULT 'admin_only' CHECK(handoff='admin_only'),
    PRIMARY KEY(tenant_id,room_id),
    FOREIGN KEY(tenant_id,room_id) REFERENCES public.rooms(tenant_id,id) ON DELETE RESTRICT
);
-- Trusted onboarding only. Names are matching hints; immutable sender tuples confer identity.
CREATE TABLE public.commitment_chat_identities (
    tenant_id uuid NOT NULL,room_id uuid NOT NULL,switch_author_id text NOT NULL CHECK(btrim(switch_author_id)<>''),
    external_author_id text NOT NULL DEFAULT '',
    subject_id text NOT NULL,label text CHECK(length(label)<=256),
    PRIMARY KEY(tenant_id,room_id,switch_author_id,external_author_id),
    FOREIGN KEY(tenant_id,room_id,subject_id) REFERENCES public.room_members(tenant_id,room_id,switch_user_id) ON DELETE CASCADE
);
CREATE TABLE public.commitment_chat_sources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,room_id uuid NOT NULL,
    switch_message_id text NOT NULL,switch_author_id text NOT NULL,external_author_id text NOT NULL DEFAULT '',
    subject_id text NOT NULL,thread_id text,external_message_id text,workspace_id text,channel_id text,
    body text NOT NULL CHECK(length(body)<=64000),
    occurred_at timestamptz NOT NULL,source_input jsonb NOT NULL,policy_snapshot jsonb NOT NULL,
    UNIQUE(tenant_id,room_id,switch_message_id),UNIQUE(tenant_id,room_id,id),
    FOREIGN KEY(tenant_id,room_id) REFERENCES public.rooms(tenant_id,id) ON DELETE RESTRICT
);
ALTER TABLE public.commitments ADD COLUMN automated_acceptance_at timestamptz;
ALTER TABLE public.commitments ADD COLUMN automated_acceptance_source_id uuid;
ALTER TABLE public.commitments ADD CONSTRAINT commitments_automated_acceptance_pair CHECK (
    (automated_acceptance_at IS NULL AND automated_acceptance_source_id IS NULL) OR
    (automated_acceptance_at IS NOT NULL AND automated_acceptance_source_id IS NOT NULL AND
        task_status='completed' AND approval_status='pending'));
ALTER TABLE public.commitments ADD FOREIGN KEY(tenant_id,room_id,automated_acceptance_source_id)
    REFERENCES public.commitment_chat_sources(tenant_id,room_id,id) ON DELETE RESTRICT;
ALTER TABLE public.commitment_action_events ADD COLUMN backend_actor text;
ALTER TABLE public.commitment_action_events ADD COLUMN source_evidence_id uuid;
ALTER TABLE public.commitment_action_events ADD COLUMN evidence text;
ALTER TABLE public.commitment_action_events ADD COLUMN outcome text NOT NULL DEFAULT 'applied' CHECK(outcome IN ('applied','no_change'));
-- Classify existing equivalent-action events in the newly added column only.
UPDATE public.commitment_action_events SET outcome='no_change' WHERE NOT changed;
ALTER TABLE public.commitment_action_events ADD CONSTRAINT commitment_chat_event_pair CHECK (
    (source_evidence_id IS NULL AND backend_actor IS NULL AND evidence IS NULL) OR
    (source_evidence_id IS NOT NULL AND backend_actor='switch-ingestion-worker' AND evidence IS NOT NULL));
ALTER TABLE public.commitment_action_events ADD FOREIGN KEY(tenant_id,room_id,source_evidence_id)
    REFERENCES public.commitment_chat_sources(tenant_id,room_id,id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX commitment_chat_source_once ON public.commitment_action_events(source_evidence_id)
    WHERE source_evidence_id IS NOT NULL;
ALTER TABLE public.commitment_chat_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.commitment_chat_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.commitment_chat_sources ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.commitment_chat_policies,public.commitment_chat_identities,public.commitment_chat_sources
    FROM PUBLIC,anon,authenticated,service_role;
-- Runtime cannot provision policies/identities, alter evidence or delete history.
GRANT SELECT ON public.commitment_chat_policies,public.commitment_chat_identities TO service_role;
-- Row sharing locks require UPDATE privilege on at least one column. The protected
-- touch column permits locking only; source input and identity columns stay immutable.
ALTER TABLE public.commitment_chat_policies ADD COLUMN lock_marker boolean NOT NULL DEFAULT false;
ALTER TABLE public.commitment_chat_identities ADD COLUMN lock_marker boolean NOT NULL DEFAULT false;
ALTER TABLE public.commitment_chat_sources ADD COLUMN lock_marker boolean NOT NULL DEFAULT false;
GRANT UPDATE(lock_marker) ON public.commitment_chat_policies,public.commitment_chat_identities,public.commitment_chat_sources TO service_role;
GRANT SELECT,INSERT ON public.commitment_chat_sources TO service_role;

CREATE FUNCTION public.prepare_commitment_chat(p_tenant_id uuid,p_switch_room_id text,p_source jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE
    room_uuid uuid; policy public.commitment_chat_policies; identity_row public.commitment_chat_identities;
    source_row public.commitment_chat_sources; actor_role text; candidates jsonb; members jsonb;
BEGIN
    SELECT id INTO room_uuid FROM public.rooms WHERE tenant_id=p_tenant_id AND switch_room_id=p_switch_room_id AND enabled;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO policy FROM public.commitment_chat_policies WHERE tenant_id=p_tenant_id AND room_id=room_uuid AND enabled;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT i.* INTO identity_row FROM public.commitment_chat_identities i
      WHERE i.tenant_id=p_tenant_id AND i.room_id=room_uuid AND i.switch_author_id=p_source->>'switch_author_id'
        AND i.external_author_id=coalesce(p_source->>'external_author_id','');
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT role INTO actor_role FROM public.room_members WHERE tenant_id=p_tenant_id AND room_id=room_uuid
      AND switch_user_id=identity_row.subject_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF jsonb_typeof(p_source->'body') IS DISTINCT FROM 'string' OR length(p_source->>'body')>64000
      OR nullif(p_source->>'occurred_at','') IS NULL OR nullif(p_source->>'switch_message_id','') IS NULL THEN
      RAISE EXCEPTION USING ERRCODE='PT422',MESSAGE='invalid_action_payload'; END IF;
    -- Register exact redacted evidence once; a replay with changed identity/text conflicts.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_tenant_id::text||'/'||room_uuid::text||'/'||(p_source->>'switch_message_id'),0));
    SELECT * INTO source_row FROM public.commitment_chat_sources WHERE tenant_id=p_tenant_id AND room_id=room_uuid
      AND switch_message_id=p_source->>'switch_message_id';
    IF FOUND THEN
      IF source_row.source_input<>p_source OR source_row.subject_id<>identity_row.subject_id OR
        source_row.policy_snapshot<>jsonb_build_object('timezone',policy.timezone,'handoff',policy.handoff,
          'accept_pending_owner_completion',policy.accept_pending_owner_completion) THEN
        RAISE EXCEPTION USING ERRCODE='PT409',MESSAGE='idempotency_conflict'; END IF;
    ELSE
      INSERT INTO public.commitment_chat_sources(tenant_id,room_id,switch_message_id,switch_author_id,external_author_id,
        subject_id,thread_id,external_message_id,workspace_id,channel_id,body,occurred_at,source_input,policy_snapshot)
      VALUES(p_tenant_id,room_uuid,p_source->>'switch_message_id',identity_row.switch_author_id,identity_row.external_author_id,
        identity_row.subject_id,p_source->>'thread_id',p_source->>'external_message_id',p_source->>'workspace_id',p_source->>'channel_id',
        p_source->>'body',(p_source->>'occurred_at')::timestamptz,p_source,
        jsonb_build_object('timezone',policy.timezone,'handoff',policy.handoff,'accept_pending_owner_completion',policy.accept_pending_owner_completion))
      RETURNING * INTO source_row;
    END IF;
    SELECT coalesce(jsonb_agg(to_jsonb(candidate)),'[]') INTO candidates FROM (
      SELECT c.* FROM public.commitments c JOIN public.messages m ON m.id=c.message_id AND m.tenant_id=c.tenant_id AND m.room_id=c.room_id
      WHERE c.tenant_id=p_tenant_id AND c.room_id=room_uuid AND c.approval_status<>'rejected'
        AND c.task_status IN ('not_started','in_progress')
        AND (c.owner_switch_user_id=identity_row.subject_id OR actor_role='admin')
        AND m.thread_id IS NOT DISTINCT FROM source_row.thread_id
        AND m.occurred_at <= source_row.occurred_at
      ORDER BY c.created_at DESC,c.id LIMIT 21
    ) candidate;
    SELECT coalesce(jsonb_agg(to_jsonb(member)),'[]') INTO members FROM (
      SELECT i.subject_id,i.label,nullif(i.external_author_id,'') external_author_id
      FROM public.commitment_chat_identities i JOIN public.room_members m
        ON m.tenant_id=i.tenant_id AND m.room_id=i.room_id AND m.switch_user_id=i.subject_id
      WHERE i.tenant_id=p_tenant_id AND i.room_id=room_uuid ORDER BY i.subject_id,i.switch_author_id LIMIT 101
    ) member;
    RETURN jsonb_build_object('source',jsonb_build_object('id',source_row.id,
      'scope',jsonb_build_object('tenant_id',p_tenant_id,'room_id',room_uuid),
      'switch_message_id',source_row.switch_message_id,'switch_author_id',source_row.switch_author_id,
      'external_author_id',nullif(source_row.external_author_id,''),'actor',jsonb_build_object('subject_id',source_row.subject_id),
      'external_message_id',source_row.external_message_id,'workspace_id',source_row.workspace_id,'channel_id',source_row.channel_id,
      'body',source_row.body,'occurred_at',source_row.occurred_at,'thread_id',source_row.thread_id),
      'role',actor_role,'policy',jsonb_build_object('timezone',policy.timezone,'handoff',policy.handoff,
       'accept_pending_owner_completion',policy.accept_pending_owner_completion),
      'candidates',candidates,'members',members,'truncated',jsonb_array_length(candidates)>20 OR jsonb_array_length(members)>100);
END;
$$;

CREATE OR REPLACE FUNCTION public.commitment_summary(c public.commitments) RETURNS text
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
    SELECT 'Commitment: '||c.description||E'\nAssignee: '||coalesce(c.owner_switch_user_id,'unresolved: '||c.owner_name,'unassigned')||
      E'\nApproval: '||c.approval_status||E'\nTask: '||c.task_status||E'\nDue (UTC): '||
      coalesce(to_char(c.due_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'unscheduled')||
      CASE WHEN c.automated_acceptance_at IS NOT NULL THEN E'\nAcceptance: automated owner completion evidence' ELSE '' END;
$$;
-- Extend the audit snapshot without losing any existing lifecycle fields.
ALTER FUNCTION public.commitment_snapshot(public.commitments) RENAME TO commitment_snapshot_before_chat;
CREATE FUNCTION public.commitment_snapshot(c public.commitments) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
    SELECT public.commitment_snapshot_before_chat(c)||jsonb_build_object(
      'automated_acceptance_at',c.automated_acceptance_at,'automated_acceptance_source_id',c.automated_acceptance_source_id);
$$;

-- Shared transaction engine: unchanged manual rules, explicit internal chat policy.
CREATE FUNCTION public.apply_commitment_action_internal(
    p_tenant_id uuid, p_room_id uuid, p_actor_id text, p_commitment_id uuid,
    p_action text, p_expected_version bigint, p_idempotency_key uuid, p_payload jsonb,
    p_backend_actor text, p_chat_source_id uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    c public.commitments; old_c public.commitments; member_role text;
    receipt public.commitment_action_receipts; event public.commitment_action_events;
    request_content jsonb; result jsonb; changed boolean := false;
    action_time timestamptz := transaction_timestamp(); new_due timestamptz;
    chat_source public.commitment_chat_sources; chat_policy public.commitment_chat_policies;
    pending_owner_evidence boolean := false;
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
    IF p_chat_source_id IS NOT NULL THEN
      IF p_backend_actor IS DISTINCT FROM 'switch-ingestion-worker' OR p_action NOT IN ('complete','cancel','reassign','reschedule') THEN
        RAISE EXCEPTION USING ERRCODE='PT403',MESSAGE='forbidden_action'; END IF;
      SELECT * INTO chat_source FROM public.commitment_chat_sources WHERE id=p_chat_source_id
        AND tenant_id=p_tenant_id AND room_id=p_room_id AND subject_id=p_actor_id FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT404',MESSAGE='not_found'; END IF;
      SELECT * INTO chat_policy FROM public.commitment_chat_policies WHERE tenant_id=p_tenant_id AND room_id=p_room_id AND enabled FOR SHARE;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT403',MESSAGE='forbidden_action'; END IF;
      IF chat_source.policy_snapshot<>jsonb_build_object('timezone',chat_policy.timezone,'handoff',chat_policy.handoff,
          'accept_pending_owner_completion',chat_policy.accept_pending_owner_completion) THEN
        RAISE EXCEPTION USING ERRCODE='PT409',MESSAGE='stale_version'; END IF;
      PERFORM 1 FROM public.commitment_chat_identities WHERE tenant_id=p_tenant_id AND room_id=p_room_id
        AND switch_author_id=chat_source.switch_author_id AND external_author_id=chat_source.external_author_id AND subject_id=p_actor_id FOR SHARE;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT403',MESSAGE='forbidden_action'; END IF;
      PERFORM 1 FROM public.messages WHERE id=c.message_id AND tenant_id=p_tenant_id AND room_id=p_room_id
        AND thread_id IS NOT DISTINCT FROM chat_source.thread_id AND occurred_at<=chat_source.occurred_at;
      IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='PT404',MESSAGE='not_found'; END IF;
      IF member_role<>'admin' AND (p_action='reassign' OR c.owner_switch_user_id IS DISTINCT FROM p_actor_id) THEN
        RAISE EXCEPTION USING ERRCODE='PT403',MESSAGE='forbidden_action'; END IF;
      pending_owner_evidence := p_action='complete' AND c.approval_status='pending'
        AND c.owner_switch_user_id=p_actor_id AND chat_policy.accept_pending_owner_completion;
    ELSE
      IF p_backend_actor IS NOT NULL OR (p_action IN ('approve','reject','cancel','reassign','reschedule') AND member_role NOT IN ('approver','admin'))
        OR (p_action IN ('start','complete') AND c.owner_switch_user_id IS DISTINCT FROM p_actor_id) THEN
        RAISE EXCEPTION USING ERRCODE='PT403', MESSAGE='forbidden_action'; END IF;
    END IF;
    request_content := jsonb_build_object('commitment_id',p_commitment_id,'action',p_action,
                                         'expected_version',p_expected_version,'payload',p_payload);
    IF p_chat_source_id IS NOT NULL THEN request_content:=request_content||jsonb_build_object('chat_source_id',p_chat_source_id); END IF;
    SELECT * INTO receipt FROM public.commitment_action_receipts WHERE tenant_id=p_tenant_id AND room_id=p_room_id
      AND actor_id=p_actor_id AND idempotency_key=p_idempotency_key;
    IF FOUND THEN
      IF receipt.request <> request_content THEN RAISE EXCEPTION USING ERRCODE='PT409', MESSAGE='idempotency_conflict'; END IF;
      RETURN receipt.result;
    END IF;
    IF p_chat_source_id IS NOT NULL AND EXISTS(SELECT 1 FROM public.commitment_action_events WHERE source_evidence_id=p_chat_source_id) THEN
      RAISE EXCEPTION USING ERRCODE='PT409',MESSAGE='idempotency_conflict'; END IF;
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
      IF c.approval_status <> 'approved' AND NOT pending_owner_evidence THEN RAISE EXCEPTION USING ERRCODE='PT409', MESSAGE='invalid_transition'; END IF;
      IF p_action='start' THEN c.task_status:='in_progress';
      ELSE
        IF c.task_status <> 'completed' THEN
          c.completed_at:=action_time;
          IF pending_owner_evidence THEN c.automated_acceptance_at:=action_time; c.automated_acceptance_source_id:=p_chat_source_id; END IF;
        END IF;
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
        automated_acceptance_at=c.automated_acceptance_at,automated_acceptance_source_id=c.automated_acceptance_source_id,
        next_reminder_at=NULL,version=version+1 WHERE id=c.id AND tenant_id=p_tenant_id AND room_id=p_room_id RETURNING * INTO c;
    END IF;
    -- Source message/extraction keys are immutable. Updating text invokes the existing
    -- embedding invalidation trigger, including clearing in-flight worker claims.
    INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content,active)
      VALUES(c.tenant_id,c.room_id,c.message_id,'commitment',c.extraction_key,public.commitment_summary(c),
        c.approval_status <> 'rejected' AND c.task_status NOT IN ('completed','cancelled'))
      ON CONFLICT(message_id,memory_type,extraction_key) DO UPDATE SET content=EXCLUDED.content,active=EXCLUDED.active;
    INSERT INTO public.commitment_action_events(tenant_id,room_id,commitment_id,actor_id,action,occurred_at,
      idempotency_key,expected_version,changed,before,after,reason,backend_actor,source_evidence_id,evidence,outcome)
      VALUES(c.tenant_id,c.room_id,c.id,p_actor_id,p_action,action_time,p_idempotency_key,p_expected_version,changed,
        public.commitment_snapshot(old_c),public.commitment_snapshot(c),p_payload->>'reason',p_backend_actor,p_chat_source_id,chat_source.body,CASE WHEN changed THEN 'applied' ELSE 'no_change' END) RETURNING * INTO event;
    result:=jsonb_build_object('commitment',to_jsonb(c),'reminder',public.commitment_reminder(c),'event',to_jsonb(event),'changed',changed);
    INSERT INTO public.commitment_action_receipts VALUES(p_tenant_id,p_room_id,p_actor_id,p_idempotency_key,c.id,request_content,result);
    RETURN result;
END;
$$;

-- Preserve the public/manual RPC signature and its original authorization policy.
CREATE OR REPLACE FUNCTION public.apply_commitment_action(
    p_tenant_id uuid,p_room_id uuid,p_actor_id text,p_commitment_id uuid,
    p_action text,p_expected_version bigint,p_idempotency_key uuid,p_payload jsonb
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT public.apply_commitment_action_internal(p_tenant_id,p_room_id,p_actor_id,p_commitment_id,
        p_action,p_expected_version,p_idempotency_key,p_payload,NULL,NULL);
$$;
CREATE FUNCTION public.apply_commitment_chat_action(
    p_tenant_id uuid,p_room_id uuid,p_actor_id text,p_commitment_id uuid,
    p_action text,p_expected_version bigint,p_idempotency_key uuid,p_payload jsonb,
    p_backend_actor text,p_chat_source_id uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
    IF p_chat_source_id IS NULL OR p_backend_actor IS DISTINCT FROM 'switch-ingestion-worker' THEN
      RAISE EXCEPTION USING ERRCODE='PT403',MESSAGE='forbidden_action'; END IF;
    RETURN public.apply_commitment_action_internal(p_tenant_id,p_room_id,p_actor_id,p_commitment_id,
        p_action,p_expected_version,p_idempotency_key,p_payload,p_backend_actor,p_chat_source_id);
END;
$$;
REVOKE ALL ON FUNCTION public.prepare_commitment_chat(uuid,text,jsonb),public.commitment_snapshot(public.commitments),
  public.apply_commitment_action_internal(uuid,uuid,text,uuid,text,bigint,uuid,jsonb,text,uuid),
  public.apply_commitment_chat_action(uuid,uuid,text,uuid,text,bigint,uuid,jsonb,text,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.prepare_commitment_chat(uuid,text,jsonb),public.commitment_snapshot(public.commitments),
  public.apply_commitment_action_internal(uuid,uuid,text,uuid,text,bigint,uuid,jsonb,text,uuid),
  public.apply_commitment_chat_action(uuid,uuid,text,uuid,text,bigint,uuid,jsonb,text,uuid) TO service_role;
COMMIT;

