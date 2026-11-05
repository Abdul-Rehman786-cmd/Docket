-- Disposable local fixtures. Never use real tasks or adapter identities.
BEGIN;
CREATE FUNCTION public.fixture_chat_fail_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'fixture_chat_failure'; END $$;
DO $$
DECLARE
 t uuid:=gen_random_uuid(); r uuid:=gen_random_uuid(); other_r uuid:=gen_random_uuid(); msg uuid:=gen_random_uuid();
 c uuid:=gen_random_uuid(); c2 uuid:=gen_random_uuid(); source_id uuid; source2 uuid; key uuid:=gen_random_uuid();
 plan jsonb; src jsonb; result jsonb; replay jsonb; total integer;
BEGIN
 INSERT INTO public.tenants(id,name) VALUES(t,'Chat fixture');
 INSERT INTO public.rooms(id,tenant_id,switch_room_id) VALUES(r,t,'chat-fixture'),(other_r,t,'other-chat-fixture');
 INSERT INTO public.room_members(tenant_id,room_id,switch_user_id,role) VALUES
   (t,r,'owner','member'),(t,r,'replacement','member'),(t,r,'admin','admin'),(t,other_r,'outsider','admin');
 INSERT INTO public.commitment_chat_policies(tenant_id,room_id,enabled,timezone) VALUES(t,r,true,'UTC');
 INSERT INTO public.commitment_chat_identities(tenant_id,room_id,switch_author_id,subject_id,label) VALUES
   (t,r,'puppet-owner','owner','Owner'),(t,r,'puppet-admin','admin','Admin'),(t,r,'puppet-replacement','replacement','Rehman');
 INSERT INTO public.messages(id,tenant_id,room_id,switch_message_id,body,occurred_at)
   VALUES(msg,t,r,'original-evidence','Original report and budget evidence','2026-11-04T12:00:00Z');
 INSERT INTO public.commitments(id,tenant_id,room_id,message_id,extraction_key,description,owner_switch_user_id)
   VALUES(c,t,r,msg,'report','Deliver report','owner');
 INSERT INTO public.commitments(id,tenant_id,room_id,message_id,extraction_key,description,owner_switch_user_id,approval_status,approval_at,approval_by_switch_user_id,due_at)
   VALUES(c2,t,r,msg,'budget','Deliver budget','owner','approved',now(),'admin','2026-12-01T12:00:00Z');
 src:=jsonb_build_object('switch_message_id','chat-complete','switch_author_id','puppet-owner',
   'external_author_id',NULL,'body','I have sent the report.','occurred_at','2026-11-05T12:00:00Z','thread_id',NULL,
   'external_message_id','platform-update-source','workspace_id','fixture-workspace','channel_id','fixture-channel');
 EXECUTE 'SET LOCAL ROLE service_role';
 IF public.prepare_commitment_chat(t,'chat-fixture',src||'{"switch_author_id":"unmapped"}') IS NOT NULL THEN
   RAISE EXCEPTION 'unknown_identity_resolved'; END IF;
 plan:=public.prepare_commitment_chat(t,'chat-fixture',src);
 source_id:=(plan#>>'{source,id}')::uuid;
 IF plan#>>'{source,actor,subject_id}'<>'owner' OR plan#>>'{source,switch_author_id}'<>'puppet-owner'
   OR plan#>>'{source,external_message_id}'<>'platform-update-source'
   OR jsonb_array_length(plan->'candidates')<>2 THEN RAISE EXCEPTION 'prepare_scope_failed'; END IF;
 result:=public.prepare_commitment_chat(t,'chat-fixture',src||'{"switch_message_id":"too-early","occurred_at":"2026-11-03T12:00:00Z"}');
 IF jsonb_array_length(result->'candidates')<>0 THEN RAISE EXCEPTION 'future_task_offered'; END IF;
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'owner',c,'complete',1,gen_random_uuid(),'{"reason":null}',
     'switch-ingestion-worker',(result#>>'{source,id}')::uuid);
   RAISE EXCEPTION 'future_task_updated'; EXCEPTION WHEN SQLSTATE 'PT404' THEN NULL;
 END;
 BEGIN
   PERFORM public.prepare_commitment_chat(t,'chat-fixture',src||'{"body":"Changed evidence"}');
   RAISE EXCEPTION 'source_changed'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
 END;
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'owner',c,'complete',1,key,'{"reason":null}','switch-ingestion-worker',source_id);
   RAISE EXCEPTION 'pending_policy_bypassed'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
 END;
 EXECUTE 'RESET ROLE';
 UPDATE public.commitment_chat_policies SET accept_pending_owner_completion=true WHERE tenant_id=t AND room_id=r;
 EXECUTE 'SET LOCAL ROLE service_role';
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'owner',c,'complete',1,key,'{"reason":null}','switch-ingestion-worker',source_id);
   RAISE EXCEPTION 'changed_policy_allowed'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
 END;
 plan:=public.prepare_commitment_chat(t,'chat-fixture',src||'{"switch_message_id":"chat-complete-reconfirmed"}');
 source_id:=(plan#>>'{source,id}')::uuid;
 result:=public.apply_commitment_chat_action(t,r,'owner',c,'complete',1,key,'{"reason":null}','switch-ingestion-worker',source_id);
 IF result#>>'{commitment,task_status}'<>'completed' OR result#>>'{commitment,approval_status}'<>'pending'
   OR result#>>'{commitment,approval_at}' IS NOT NULL OR result#>>'{commitment,approval_by_switch_user_id}' IS NOT NULL
   OR result#>>'{commitment,automated_acceptance_source_id}'<>source_id::text THEN RAISE EXCEPTION 'pending_acceptance_failed'; END IF;
 IF result#>>'{event,actor_id}'<>'owner' OR result#>>'{event,backend_actor}'<>'switch-ingestion-worker'
   OR result#>>'{event,evidence}'<>'I have sent the report.' OR result#>>'{event,outcome}'<>'applied'
   OR result#>>'{event,before,version}'<>'1' OR result#>>'{event,after,version}'<>'2' THEN RAISE EXCEPTION 'audit_failed'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.memories WHERE message_id=msg AND extraction_key='report' AND NOT active
   AND embedding_status='pending' AND content LIKE '%Approval: pending%Task: completed%automated owner completion evidence%') THEN
   RAISE EXCEPTION 'linked_memory_failed'; END IF;
 replay:=public.apply_commitment_chat_action(t,r,'owner',c,'complete',1,key,'{"reason":null}','switch-ingestion-worker',source_id);
 IF replay<>result OR (SELECT count(*) FROM public.commitment_action_events WHERE source_evidence_id=source_id)<>1 THEN
   RAISE EXCEPTION 'chat_replay_failed'; END IF;
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'owner',c2,'cancel',1,gen_random_uuid(),'{"reason":null}','switch-ingestion-worker',source_id);
   RAISE EXCEPTION 'source_applied_twice'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
 END;
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'admin',c2,'complete',1,gen_random_uuid(),'{"reason":null}','switch-ingestion-worker',source_id);
   RAISE EXCEPTION 'source_sender_spoofed'; EXCEPTION WHEN SQLSTATE 'PT404' THEN NULL;
 END;
 plan:=public.prepare_commitment_chat(t,'chat-fixture',src||'{"switch_message_id":"chat-cancel-budget","body":"Cancel my budget task."}');
 source2:=(plan#>>'{source,id}')::uuid;
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'owner',c2,'reassign',1,gen_random_uuid(),'{"reason":null,"assignee_id":"replacement"}','switch-ingestion-worker',source2);
   RAISE EXCEPTION 'owner_handoff_allowed'; EXCEPTION WHEN SQLSTATE 'PT403' THEN NULL;
 END;
 EXECUTE 'RESET ROLE';
 SELECT count(*) INTO total FROM public.commitment_action_events;
 EXECUTE 'CREATE TRIGGER fixture_chat_fail BEFORE INSERT ON public.commitment_action_receipts FOR EACH ROW EXECUTE FUNCTION public.fixture_chat_fail_receipt()';
 EXECUTE 'SET LOCAL ROLE service_role';
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'owner',c2,'cancel',1,gen_random_uuid(),'{"reason":null}','switch-ingestion-worker',source2);
   RAISE EXCEPTION 'rollback_failure_not_triggered';
 EXCEPTION WHEN raise_exception THEN IF SQLERRM<>'fixture_chat_failure' THEN RAISE; END IF; END;
 IF (SELECT version FROM public.commitments WHERE id=c2)<>1 OR
   EXISTS(SELECT 1 FROM public.memories WHERE message_id=msg AND extraction_key='budget') OR
   (SELECT count(*) FROM public.commitment_action_events)<>total THEN RAISE EXCEPTION 'atomic_rollback_failed'; END IF;
 EXECUTE 'RESET ROLE';
 EXECUTE 'DROP TRIGGER fixture_chat_fail ON public.commitment_action_receipts';
 DELETE FROM public.commitment_chat_identities WHERE tenant_id=t AND room_id=r AND switch_author_id='puppet-owner';
 EXECUTE 'SET LOCAL ROLE service_role';
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'owner',c2,'cancel',1,gen_random_uuid(),'{"reason":null}','switch-ingestion-worker',source2);
   RAISE EXCEPTION 'revoked_mapping_allowed'; EXCEPTION WHEN SQLSTATE 'PT403' THEN NULL;
 END;
 plan:=public.prepare_commitment_chat(t,'chat-fixture',src||'{"switch_message_id":"admin-handoff","switch_author_id":"puppet-admin","body":"Rehman is taking over the budget."}');
 source2:=(plan#>>'{source,id}')::uuid;
 result:=public.apply_commitment_chat_action(t,r,'admin',c2,'reassign',1,gen_random_uuid(),'{"reason":null,"assignee_id":"replacement"}','switch-ingestion-worker',source2);
 IF result#>>'{commitment,owner_switch_user_id}'<>'replacement' THEN RAISE EXCEPTION 'admin_handoff_failed'; END IF;
 plan:=public.prepare_commitment_chat(t,'chat-fixture',src||'{"switch_message_id":"admin-move","switch_author_id":"puppet-admin","body":"Move the budget deadline to 2026-12-02T17:00:00+05:00."}');
 source2:=(plan#>>'{source,id}')::uuid;
 BEGIN
   PERFORM public.apply_commitment_chat_action(t,r,'admin',c2,'reschedule',1,gen_random_uuid(),'{"reason":null,"due_at":"2026-12-02T17:00:00+05:00"}','switch-ingestion-worker',source2);
   RAISE EXCEPTION 'stale_chat_allowed'; EXCEPTION WHEN SQLSTATE 'PT409' THEN NULL;
 END;
 result:=public.apply_commitment_chat_action(t,r,'admin',c2,'reschedule',2,gen_random_uuid(),'{"reason":null,"due_at":"2026-12-02T17:00:00+05:00"}','switch-ingestion-worker',source2);
 IF (result#>>'{commitment,due_at}')::timestamptz<>'2026-12-02T12:00:00Z'::timestamptz THEN RAISE EXCEPTION 'chat_due_failed'; END IF;
 plan:=public.prepare_commitment_chat(t,'chat-fixture',src||'{"switch_message_id":"admin-cancel","switch_author_id":"puppet-admin","body":"Cancel the budget task."}');
 result:=public.apply_commitment_chat_action(t,r,'admin',c2,'cancel',3,gen_random_uuid(),'{"reason":null}','switch-ingestion-worker',(plan#>>'{source,id}')::uuid);
 IF result#>>'{reminder,eligible}'<>'false' OR result#>>'{commitment,task_status}'<>'cancelled' THEN RAISE EXCEPTION 'chat_cancel_failed'; END IF;
 IF (SELECT body FROM public.messages WHERE id=msg)<>'Original report and budget evidence' THEN RAISE EXCEPTION 'original_evidence_changed'; END IF;
 EXECUTE 'RESET ROLE';
 IF has_function_privilege('anon','public.prepare_commitment_chat(uuid,text,jsonb)','EXECUTE') OR
    has_function_privilege('authenticated','public.apply_commitment_chat_action(uuid,uuid,text,uuid,text,bigint,uuid,jsonb,text,uuid)','EXECUTE') OR
    has_column_privilege('service_role','public.commitment_chat_identities','subject_id','UPDATE') OR
    has_table_privilege('service_role','public.commitment_chat_policies','INSERT') THEN RAISE EXCEPTION 'chat_privileges_failed'; END IF;
END $$;
ROLLBACK;
