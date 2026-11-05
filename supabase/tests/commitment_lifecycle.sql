BEGIN;
-- Every fixture is transaction-local and is rolled back, including failure triggers.
CREATE FUNCTION public.fixture_fail_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'fixture_failure'; END $$;
DO $$
DECLARE
 t uuid:=gen_random_uuid(); r uuid:=gen_random_uuid(); other_r uuid:=gen_random_uuid(); msg uuid:=gen_random_uuid();
 c uuid:=gen_random_uuid(); c2 uuid:=gen_random_uuid(); k uuid:=gen_random_uuid(); result jsonb; replay jsonb;
 old_count integer; old_content text; v bigint;
BEGIN
 INSERT INTO public.tenants(id,name) VALUES(t,'Lifecycle fixture');
 INSERT INTO public.rooms(id,tenant_id,switch_room_id) VALUES(r,t,'fixture-lifecycle'),(other_r,t,'fixture-lifecycle-other');
 INSERT INTO public.room_members(tenant_id,room_id,switch_user_id,role) VALUES
 (t,r,'reviewer','approver'),(t,r,'worker','member'),(t,r,'replacement','member'),(t,other_r,'outsider','admin');
 INSERT INTO public.messages(id,tenant_id,room_id,switch_message_id,body) VALUES(msg,t,r,'fixture-source','Original evidence');
 INSERT INTO public.commitments(id,tenant_id,room_id,message_id,extraction_key,description,owner_switch_user_id,due_at)
 VALUES(c,t,r,msg,'one','Prepare fixture report','worker','2026-12-01T12:00:00Z'),
       (c2,t,r,msg,'two','Other task','worker',NULL);
 INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content)
 VALUES(t,r,msg,'commitment','one','Old summary');
 UPDATE public.memories SET embedding_status='processing',embedding_claim_token=gen_random_uuid(),
   embedding_lease_until=now()+interval '10 minutes' WHERE message_id=msg;
 -- Backend-only privileges, without impersonating service-role inputs as real authentication.
 IF has_function_privilege('anon','public.apply_commitment_action(uuid,uuid,text,uuid,text,bigint,uuid,jsonb)','EXECUTE')
 OR has_function_privilege('authenticated','public.read_commitment_lifecycle(uuid,uuid,text,text,uuid,jsonb)','EXECUTE')
 OR has_table_privilege('service_role','public.commitment_action_events','UPDATE') THEN RAISE EXCEPTION 'privilege_failure'; END IF;
 BEGIN PERFORM public.apply_commitment_action(t,r,'worker',c,'complete',1,gen_random_uuid(),'{"reason":null}');
   RAISE EXCEPTION 'pending_completed'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
 BEGIN PERFORM public.apply_commitment_action(t,r,'worker',c,'approve',1,gen_random_uuid(),'{"reason":null}');
   RAISE EXCEPTION 'member_approved'; EXCEPTION WHEN SQLSTATE 'PT403' THEN NULL; END;
 BEGIN PERFORM public.read_commitment_lifecycle(t,r,'outsider','context',c);
   RAISE EXCEPTION 'cross_room_read'; EXCEPTION WHEN SQLSTATE 'PT404' THEN NULL; END;
 result:=public.apply_commitment_action(t,r,'reviewer',c,'approve',1,k,'{"reason":"Reviewed"}');
 IF EXISTS(SELECT 1 FROM public.memories WHERE message_id=msg AND
   (embedding_claim_token IS NOT NULL OR embedding_lease_until IS NOT NULL OR embedding_status <> 'pending')) THEN
   RAISE EXCEPTION 'stale_embedding_claim_survived'; END IF;
 IF result#>>'{commitment,approval_status}' <> 'approved' OR result#>>'{commitment,version}' <> '2'
   OR result#>>'{reminder,eligible}' <> 'true' THEN RAISE EXCEPTION 'approve_failure'; END IF;
 replay:=public.apply_commitment_action(t,r,'reviewer',c,'approve',1,k,'{"reason":"Reviewed"}');
 IF replay <> result THEN RAISE EXCEPTION 'replay_failure'; END IF;
 BEGIN PERFORM public.apply_commitment_action(t,r,'reviewer',c2,'approve',1,k,'{"reason":"Reviewed"}');
   RAISE EXCEPTION 'key_reused'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
 BEGIN PERFORM public.apply_commitment_action(t,r,'reviewer',c,'cancel',1,gen_random_uuid(),'{"reason":null}');
   RAISE EXCEPTION 'stale_allowed'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
 result:=public.apply_commitment_action(t,r,'reviewer',c,'approve',2,gen_random_uuid(),'{"reason":null}');
 IF result->>'changed' <> 'false' OR result#>>'{commitment,version}' <> '2' THEN RAISE EXCEPTION 'equivalent_changed'; END IF;
 BEGIN PERFORM public.apply_commitment_action(t,r,'reviewer',c,'reassign',2,gen_random_uuid(),'{"reason":null,"assignee_id":"outsider"}');
   RAISE EXCEPTION 'out_of_scope_assignee'; EXCEPTION WHEN SQLSTATE 'PT422' THEN NULL; END;
 result:=public.apply_commitment_action(t,r,'reviewer',c,'reschedule',2,gen_random_uuid(),'{"reason":null,"due_at":"2026-12-02T17:00:00+05:00"}');
 IF (result#>>'{commitment,due_at}')::timestamptz <> '2026-12-02T12:00:00Z'::timestamptz THEN RAISE EXCEPTION 'timezone_failure'; END IF;
 BEGIN PERFORM public.apply_commitment_action(t,r,'reviewer',c,'reschedule',3,gen_random_uuid(),'{"reason":null}');
   RAISE EXCEPTION 'omitted_due'; EXCEPTION WHEN SQLSTATE 'PT422' THEN NULL; END;
 BEGIN PERFORM public.apply_commitment_action(t,r,'reviewer',c,'reschedule',3,gen_random_uuid(),'{"due_at":"2026-12-02T12:00:00"}');
   RAISE EXCEPTION 'naive_due'; EXCEPTION WHEN SQLSTATE 'PT422' THEN NULL; END;
 result:=public.apply_commitment_action(t,r,'reviewer',c,'reschedule',3,gen_random_uuid(),'{"reason":null,"due_at":null}');
 IF result#>>'{reminder,reason}' <> 'no_deadline' THEN RAISE EXCEPTION 'clear_failure'; END IF;
 result:=public.apply_commitment_action(t,r,'reviewer',c,'reassign',4,gen_random_uuid(),'{"reason":null,"assignee_id":"replacement"}');
 BEGIN PERFORM public.apply_commitment_action(t,r,'worker',c,'start',5,gen_random_uuid(),'{"reason":null}');
   RAISE EXCEPTION 'former_owner_start'; EXCEPTION WHEN SQLSTATE 'PT403' THEN NULL; END;
 result:=public.apply_commitment_action(t,r,'replacement',c,'start',5,gen_random_uuid(),'{"reason":null}');
 result:=public.apply_commitment_action(t,r,'replacement',c,'complete',6,gen_random_uuid(),'{"reason":null}');
 IF result#>>'{commitment,completed_at}' IS NULL OR result#>>'{reminder,reason}' <> 'completed'
   OR result#>'{event,before,task_status}' <> '"in_progress"' OR result#>'{event,after,task_status}' <> '"completed"'
   THEN RAISE EXCEPTION 'complete_failure'; END IF;
 IF EXISTS(SELECT 1 FROM public.memories WHERE message_id=msg AND extraction_key='one' AND
   (active OR embedding IS NOT NULL OR embedding_status <> 'pending' OR content NOT LIKE '%Task: completed%')) THEN
   RAISE EXCEPTION 'memory_failure'; END IF;
 BEGIN PERFORM public.apply_commitment_action(t,r,'reviewer',c,'reassign',7,gen_random_uuid(),'{"assignee_id":"worker"}');
   RAISE EXCEPTION 'terminal_changed'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
 result:=public.apply_commitment_action(t,r,'reviewer',c2,'reject',1,gen_random_uuid(),'{"reason":"Unsupported"}');
 IF result#>>'{reminder,reason}' <> 'rejected' THEN RAISE EXCEPTION 'reject_failure'; END IF;
 BEGIN PERFORM public.apply_commitment_action(t,r,'reviewer',c2,'approve',2,gen_random_uuid(),'{"reason":null}');
   RAISE EXCEPTION 'rejected_approved'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL; END;
 -- Reuse a third pending fixture for cancellation and forced rollback after memory/audit writes.
 INSERT INTO public.commitments(tenant_id,room_id,message_id,extraction_key,description) VALUES(t,r,msg,'three','Rollback task') RETURNING id INTO c2;
 SELECT count(*) INTO old_count FROM public.commitment_action_events;
 EXECUTE 'CREATE TRIGGER fixture_fail BEFORE INSERT ON public.commitment_action_receipts FOR EACH ROW EXECUTE FUNCTION public.fixture_fail_receipt()';
 BEGIN
   PERFORM public.apply_commitment_action(t,r,'reviewer',c2,'cancel',1,gen_random_uuid(),'{"reason":"Cancelled"}');
   RAISE EXCEPTION 'failure_not_triggered';
 EXCEPTION WHEN raise_exception THEN IF SQLERRM <> 'fixture_failure' THEN RAISE; END IF; END;
 EXECUTE 'DROP TRIGGER fixture_fail ON public.commitment_action_receipts';
 IF (SELECT version FROM public.commitments WHERE id=c2) <> 1 OR
   (SELECT count(*) FROM public.commitment_action_events) <> old_count OR
   EXISTS(SELECT 1 FROM public.memories WHERE message_id=msg AND extraction_key='three') THEN RAISE EXCEPTION 'rollback_failure'; END IF;
 result:=public.apply_commitment_action(t,r,'reviewer',c2,'cancel',1,gen_random_uuid(),'{"reason":"Cancelled"}');
 IF result#>>'{commitment,cancelled_at}' IS NULL OR result#>>'{reminder,eligible}' <> 'false' THEN RAISE EXCEPTION 'cancel_failure'; END IF;
 result:=public.read_commitment_lifecycle(t,r,'worker','list',NULL,'{"limit":1}');
 IF jsonb_array_length(result->'items') <> 1 OR result->>'next_offset' <> '1' THEN RAISE EXCEPTION 'pagination_failure'; END IF;
 result:=public.read_commitment_lifecycle(t,r,'worker','history',c,'{"limit":100}');
 IF jsonb_array_length(result->'items') <> 7 THEN RAISE EXCEPTION 'history_failure'; END IF;
 IF (SELECT body FROM public.messages WHERE id=msg) <> 'Original evidence' THEN RAISE EXCEPTION 'source_changed'; END IF;
 -- Exercise execution using the backend role rather than just owner privileges.
 EXECUTE 'SET LOCAL ROLE service_role';
 result:=public.read_commitment_lifecycle(t,r,'worker','context',c2);
 result:=public.apply_commitment_action(t,r,'reviewer',c2,'cancel',2,gen_random_uuid(),'{"reason":null}');
 IF result->>'changed' <> 'false' THEN RAISE EXCEPTION 'backend_role_rpc_failure'; END IF;
 EXECUTE 'RESET ROLE';
END $$;
ROLLBACK;
