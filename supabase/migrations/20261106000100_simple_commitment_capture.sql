-- ADDITIVE, REVIEW BEFORE HOSTED APPLICATION. No tables/columns/reset/history edits.
-- Requires the reviewed four-argument atomic storage RPC and existing owner columns.
-- Lifecycle/chat/reminder migrations are NOT prerequisites for simple capture.
BEGIN;
CREATE FUNCTION public.store_message_extraction(
    p_tenant_id uuid,p_switch_room_id text,p_source jsonb,p_items jsonb,p_capture boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE
    counts jsonb; m public.messages; c public.commitments; item jsonb; owner_id text;
BEGIN
    owner_id:=p_source->>'switch_author_id';
    IF p_capture IS DISTINCT FROM true OR owner_id IS NULL OR length(owner_id) NOT BETWEEN 1 AND 512
      OR owner_id ~ '\s' OR jsonb_typeof(p_items->'commitments') IS DISTINCT FROM 'array'
      OR jsonb_array_length(p_items->'commitments')<>1 THEN
      RAISE EXCEPTION 'invalid_capture' USING ERRCODE='22023'; END IF;
    -- The existing function locks enabled scope/source and performs the entire
    -- extraction. This call and owner initialization share ONE SQL transaction.
    counts:=public.store_message_extraction(p_tenant_id,p_switch_room_id,p_source,p_items);
    SELECT msg.* INTO m FROM public.messages msg JOIN public.rooms r
      ON r.id=msg.room_id AND r.tenant_id=msg.tenant_id
      WHERE msg.tenant_id=p_tenant_id AND r.switch_room_id=p_switch_room_id
        AND msg.switch_message_id=p_source->>'switch_message_id' FOR UPDATE OF msg;
    IF NOT FOUND OR m.switch_author_id IS DISTINCT FROM owner_id OR m.body IS DISTINCT FROM p_source->>'body'
      OR m.occurred_at IS DISTINCT FROM (p_source->>'occurred_at')::timestamptz
      OR m.thread_id IS DISTINCT FROM p_source->>'thread_id' THEN
      RAISE EXCEPTION 'capture_source_conflict' USING ERRCODE='23514'; END IF;
    item:=p_items->'commitments'->0;
    -- A distinct key namespace prevents a replay from claiming a legacy task.
    IF item->>'extraction_key' NOT LIKE 'capture-%' THEN
      RAISE EXCEPTION 'invalid_capture' USING ERRCODE='22023'; END IF;
    SELECT * INTO c FROM public.commitments WHERE tenant_id=p_tenant_id AND room_id=m.room_id
      AND message_id=m.id AND extraction_key=item->>'extraction_key' FOR UPDATE;
    IF NOT FOUND OR c.description IS DISTINCT FROM item->>'description'
      OR c.due_at IS DISTINCT FROM (item->>'due_at')::timestamptz THEN
      RAISE EXCEPTION 'capture_source_conflict' USING ERRCODE='23514'; END IF;
    IF c.owner_switch_user_id IS NULL THEN
      IF c.approval_status<>'pending' OR c.task_status<>'not_started'
        OR coalesce((to_jsonb(c)->>'version')::bigint,1)<>1 THEN
        RAISE EXCEPTION 'capture_source_conflict' USING ERRCODE='23514'; END IF;
      UPDATE public.commitments SET owner_switch_user_id=owner_id,owner_external_id=p_source->>'external_author_id',
        owner_name=p_source->>'author_name' WHERE id=c.id AND tenant_id=p_tenant_id AND room_id=m.room_id;
    ELSIF c.owner_switch_user_id<>owner_id THEN
      -- Preserve later lifecycle reassignment/history; never overwrite it on replay.
      RAISE EXCEPTION 'capture_source_conflict' USING ERRCODE='23514';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM public.memories WHERE tenant_id=p_tenant_id AND room_id=m.room_id
      AND message_id=m.id AND memory_type='commitment' AND extraction_key=c.extraction_key) THEN
      RAISE EXCEPTION 'capture_summary_missing' USING ERRCODE='23514'; END IF;
    RETURN counts;
END;
$$;
REVOKE ALL ON FUNCTION public.store_message_extraction(uuid,text,jsonb,jsonb,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.store_message_extraction(uuid,text,jsonb,jsonb,boolean) TO service_role;
COMMIT;
