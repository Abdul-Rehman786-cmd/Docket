-- Requires the storage functions from the consolidated baseline, installed on a
-- fresh disposable database or through an approved non-destructive hosted upgrade.
-- Real database integration test; no model or Discord request. All fixture writes roll back.
BEGIN;
DO $$
DECLARE
    tenant uuid := gen_random_uuid(); room uuid := gen_random_uuid(); other_tenant uuid := gen_random_uuid();
    marker text := 'TM-atomic-fixture-' || gen_random_uuid()::text;
    source jsonb; items jsonb; counts jsonb; source_id uuid;
BEGIN
    IF has_function_privilege('anon','public.store_message_extraction(uuid,text,jsonb,jsonb)','EXECUTE')
       OR has_function_privilege('authenticated','public.store_message_extraction(uuid,text,jsonb,jsonb)','EXECUTE')
       OR NOT has_function_privilege('service_role','public.store_message_extraction(uuid,text,jsonb,jsonb)','EXECUTE') THEN
        RAISE EXCEPTION 'fixture_rpc_privileges_failed';
    END IF;
    INSERT INTO public.tenants(id,name) VALUES(tenant,marker),(other_tenant,marker || '-other');
    INSERT INTO public.rooms(id,tenant_id,switch_room_id,name) VALUES(room,tenant,marker,marker);
    source := jsonb_build_object('switch_message_id',marker,'switch_author_id','fixture-human',
        'author_name','Fixture person','body','Good morning, planning meeting is Friday',
        'occurred_at','2026-11-05T10:00:00Z','thread_id','fixture-thread','platform','discord',
        'external_message_id','fixture-external-id');
    items := jsonb_build_object('commitments','[]'::jsonb,'decisions','[]'::jsonb,
        'memories',jsonb_build_array(jsonb_build_object('memory_type','fact','extraction_key','fact-1',
                                                     'content','Planning meeting is Friday')));
    counts := public.store_message_extraction(tenant,marker,source,items);
    IF counts <> '{"messages":1,"commitments":0,"decisions":0,"memories":1}'::jsonb THEN
        RAISE EXCEPTION 'fixture_fact_counts_failed';
    END IF;
    IF public.store_message_extraction(tenant,marker,source,items) <> counts THEN
        RAISE EXCEPTION 'fixture_replay_failed';
    END IF;
    -- Malicious item scope/status fields must not affect authorized rows/defaults.
    items := jsonb_build_object(
        'commitments',jsonb_build_array(jsonb_build_object('extraction_key','commitment-1',
            'description','Send fixture report by Friday','tenant_id',other_tenant,
            'room_id',gen_random_uuid(),'approval_status','approved','task_status','completed')),
        'decisions',jsonb_build_array(jsonb_build_object('extraction_key','decision-1',
            'summary','Use FastAPI','approval_status','approved')),
        'memories',jsonb_build_array(
            jsonb_build_object('extraction_key','commitment-1','memory_type','commitment','content','Send fixture report by Friday'),
            jsonb_build_object('extraction_key','decision-1','memory_type','decision','content','Use FastAPI')));
    source := source || jsonb_build_object('switch_message_id',marker || '-structured');
    counts := public.store_message_extraction(tenant,marker,source,items);
    IF counts <> '{"messages":1,"commitments":1,"decisions":1,"memories":2}'::jsonb THEN
        RAISE EXCEPTION 'fixture_structured_counts_failed';
    END IF;
    SELECT id INTO source_id FROM public.messages WHERE tenant_id=tenant AND switch_message_id=marker || '-structured';
    IF NOT EXISTS(SELECT 1 FROM public.commitments WHERE message_id=source_id AND tenant_id=tenant
        AND room_id=room AND approval_status='pending' AND task_status='not_started' AND due_at IS NULL
        AND owner_name IS NULL AND approval_at IS NULL) THEN RAISE EXCEPTION 'fixture_commitment_defaults_failed'; END IF;
    IF NOT EXISTS(SELECT 1 FROM public.decisions WHERE message_id=source_id AND tenant_id=tenant
        AND room_id=room AND approval_status='pending' AND approval_at IS NULL) THEN
        RAISE EXCEPTION 'fixture_decision_defaults_failed'; END IF;
    IF NOT EXISTS(SELECT 1 FROM public.messages WHERE id=source_id AND extraction_status='completed'
        AND thread_id='fixture-thread' AND external_message_id='fixture-external-id'
        AND switch_author_id='fixture-human' AND occurred_at='2026-11-05T10:00:00Z'::timestamptz) THEN
        RAISE EXCEPTION 'fixture_source_metadata_failed'; END IF;
    BEGIN
        PERFORM public.store_message_extraction(other_tenant,marker,source,items);
        RAISE EXCEPTION 'fixture_scope_was_not_rejected';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
    -- A failure after inserting the source AND structured rows must roll back all writes.
    BEGIN
        PERFORM public.store_message_extraction(tenant,marker,
            source || jsonb_build_object('switch_message_id',marker || '-rollback'),
            items || jsonb_build_object('memories',jsonb_build_array(jsonb_build_object(
                'extraction_key','bad-1','memory_type','invalid','content','Invalid memory'))));
        RAISE EXCEPTION 'fixture_invalid_memory_was_not_rejected';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    IF (SELECT count(*) FROM public.messages WHERE tenant_id=tenant) <> 2
       OR (SELECT count(*) FROM public.commitments WHERE tenant_id=tenant) <> 1
       OR (SELECT count(*) FROM public.decisions WHERE tenant_id=tenant) <> 1
       OR (SELECT count(*) FROM public.memories WHERE tenant_id=tenant) <> 3 THEN
        RAISE EXCEPTION 'fixture_atomicity_or_dedup_failed';
    END IF;
END $$;
ROLLBACK;
SELECT 'atomic_storage_fixture_passed_and_rolled_back' AS result,
       '{"messages":2,"commitments":1,"decisions":1,"memories":3}'::jsonb AS verified_transaction_counts;
