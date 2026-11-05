BEGIN;
CREATE FUNCTION public.fixture_reminder_ack_failure() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'fixture_ack_rollback';END $$;
DO $$
DECLARE t uuid:=gen_random_uuid();r uuid:=gen_random_uuid();m uuid:=gen_random_uuid();c uuid:=gen_random_uuid();
 item jsonb;rows jsonb;delivery jsonb;token uuid;d uuid;d2 uuid;c2 uuid:=gen_random_uuid();key uuid:=gen_random_uuid();p_version bigint;original_version bigint;
BEGIN
 INSERT INTO public.tenants(id,name) VALUES(t,'Reminder fixture');
 INSERT INTO public.rooms(id,tenant_id,switch_room_id,platform) VALUES(r,t,'reminder-fixture','discord');
 INSERT INTO public.room_members(tenant_id,room_id,switch_user_id,role) VALUES(t,r,'worker','member'),(t,r,'admin','admin');
 INSERT INTO public.messages(id,tenant_id,room_id,switch_message_id,body) VALUES(m,t,r,'reminder-source','Original source');
 INSERT INTO public.commitment_chat_policies(tenant_id,room_id,reminders_enabled,timezone,reminder_quiet_start,reminder_quiet_end)
 VALUES(t,r,true,'UTC',0,0);
 INSERT INTO public.commitments(id,tenant_id,room_id,message_id,extraction_key,description,owner_switch_user_id,due_at,
 approval_status,approval_by_switch_user_id,approval_at) VALUES(c,t,r,m,'one','Deliver fixture report','worker',now()-interval '2 hours','approved','admin',now());
 item:=jsonb_build_object('occurrence_key',key,'tenant_id',t,'room_id',r,'commitment_id',c,'recipient_id','worker',
 'commitment_version',1,'policy_version',1,'kind','overdue','ordinal',0,'occurrence_base',now()-interval '2 hours','scheduled_for',now());
 EXECUTE 'SET LOCAL ROLE service_role';
 rows:=public.read_reminder_work(t,ARRAY['reminder-fixture'],25,NULL);
 IF jsonb_array_length(rows->'items')<>1 THEN RAISE EXCEPTION 'eligible_work_missing';END IF;
 IF NOT public.persist_reminder_intention(t,ARRAY['reminder-fixture'],item) THEN RAISE EXCEPTION 'intention_missing';END IF;
 IF public.persist_reminder_intention(t,ARRAY['reminder-fixture'],item) THEN RAISE EXCEPTION 'duplicate_intention';END IF;
 IF jsonb_array_length(public.claim_reminder_deliveries(gen_random_uuid(),ARRAY['reminder-fixture'],1))<>0 THEN RAISE EXCEPTION 'cross_tenant_claim';END IF;
 IF (SELECT last_chased_at FROM public.commitments WHERE id=c) IS NOT NULL THEN RAISE EXCEPTION 'planned_counted_sent';END IF;
 rows:=public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1);delivery:=rows->0;
 d:=(delivery->>'id')::uuid;token:=(delivery->>'claim_token')::uuid;
 IF d IS NULL OR jsonb_array_length(public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1))<>0 THEN RAISE EXCEPTION 'competing_claim';END IF;
 IF public.begin_reminder_dispatch(t,ARRAY['other-room'],d,token,'Fixture reminder') THEN RAISE EXCEPTION 'cross_room_dispatch';END IF;
 IF NOT public.begin_reminder_dispatch(t,ARRAY['reminder-fixture'],d,token,'Fixture reminder') THEN RAISE EXCEPTION 'begin_failed';END IF;
 IF public.begin_reminder_dispatch(t,ARRAY['reminder-fixture'],d,token,'Fixture reminder') THEN RAISE EXCEPTION 'duplicate_send_started';END IF;
 IF NOT public.finish_reminder_delivery(t,ARRAY['reminder-fixture'],d,token,'sent','fixture-switch-reference',NULL) THEN RAISE EXCEPTION 'finish_failed';END IF;
 IF NOT EXISTS(SELECT 1 FROM public.reminder_deliveries WHERE id=d AND status='sent' AND sent_at=now() AND attempts=1 AND switch_reply_message_id='fixture-switch-reference') THEN RAISE EXCEPTION 'sent_state_failed';END IF;
 IF (SELECT last_chased_at FROM public.commitments WHERE id=c) IS DISTINCT FROM now() OR (SELECT version FROM public.commitments WHERE id=c)<>1 THEN RAISE EXCEPTION 'success_bookkeeping_failed';END IF;
 IF public.finish_reminder_delivery(t,ARRAY['reminder-fixture'],d,token,'sent','duplicate-reference',NULL) THEN RAISE EXCEPTION 'old_claim_finished';END IF;
 EXECUTE 'RESET ROLE';
 INSERT INTO public.commitments(id,tenant_id,room_id,message_id,extraction_key,description,owner_switch_user_id,due_at,
 approval_status,approval_by_switch_user_id,approval_at) VALUES(c2,t,r,m,'two','Second fixture task','worker',now()-interval '2 hours','approved','admin',now());
 EXECUTE 'SET LOCAL ROLE service_role';
 PERFORM public.persist_reminder_intention(t,ARRAY['reminder-fixture'],item||jsonb_build_object('occurrence_key',gen_random_uuid(),'commitment_id',c2));
 rows:=public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1);d2:=(rows->0->>'id')::uuid;
 IF d2 IS NULL THEN RAISE EXCEPTION 'room_spacing_fixture_missing';END IF;
 IF public.begin_reminder_dispatch(t,ARRAY['reminder-fixture'],d2,(rows->0->>'claim_token')::uuid,'Must wait for room spacing') THEN RAISE EXCEPTION 'room_burst_allowed';END IF;
 IF NOT EXISTS(SELECT 1 FROM public.reminder_deliveries WHERE id=d2 AND attempts=0 AND status='pending' AND next_attempt_at>=now()+interval '60 seconds') THEN RAISE EXCEPTION 'room_spacing_not_deferred';END IF;
 EXECUTE 'RESET ROLE';
 -- Change deadline/version to invalidate old work and start a distinct planning cycle.
 UPDATE public.commitments SET due_at=now()-interval '1 hour',version=version+1 WHERE id=c;
 item:=item||jsonb_build_object('occurrence_key',gen_random_uuid(),'commitment_version',2);
 -- Clear only synthetic successful-history timing so this fixture can exercise send failures without sleeping.
 UPDATE public.reminder_deliveries SET sent_at=now()-interval '2 days' WHERE id=d;
 EXECUTE 'SET LOCAL ROLE service_role';
 IF NOT public.persist_reminder_intention(t,ARRAY['reminder-fixture'],item) THEN RAISE EXCEPTION 'second_cycle_missing';END IF;
 rows:=public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1);delivery:=rows->0;d:=(delivery->>'id')::uuid;token:=(delivery->>'claim_token')::uuid;
 IF NOT public.begin_reminder_dispatch(t,ARRAY['reminder-fixture'],d,token,'Fixture retry') THEN RAISE EXCEPTION 'retry_begin_failed';END IF;
 PERFORM public.finish_reminder_delivery(t,ARRAY['reminder-fixture'],d,token,'transient',NULL,'switch_connect_failed');
 IF NOT EXISTS(SELECT 1 FROM public.reminder_deliveries WHERE id=d AND status='failed' AND attempts=1 AND next_attempt_at=now()+interval '30 seconds' AND sent_at IS NULL) THEN RAISE EXCEPTION 'retry_state_failed';END IF;
 IF jsonb_array_length(public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1))<>0 THEN RAISE EXCEPTION 'retry_early';END IF;
 EXECUTE 'RESET ROLE';
 UPDATE public.reminder_deliveries SET next_attempt_at=now() WHERE id=d;
 EXECUTE 'SET LOCAL ROLE service_role';
 rows:=public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1);token:=(rows->0->>'claim_token')::uuid;
 EXECUTE 'RESET ROLE';
 -- Expired unstarted lease returns to the queue and rotates the claim token.
 UPDATE public.reminder_deliveries SET lease_until=now()-interval '1 second' WHERE id=d;
 EXECUTE 'SET LOCAL ROLE service_role';
 rows:=public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1);
 IF jsonb_array_length(rows)<>1 OR (rows->0->>'claim_token')::uuid=token THEN RAISE EXCEPTION 'unstarted_recovery_failed';END IF;
 token:=(rows->0->>'claim_token')::uuid;
 IF NOT public.begin_reminder_dispatch(t,ARRAY['reminder-fixture'],d,token,'Fixture uncertain') THEN RAISE EXCEPTION 'uncertain_begin_failed';END IF;
 EXECUTE 'RESET ROLE';
 UPDATE public.reminder_deliveries SET lease_until=now()-interval '1 second' WHERE id=d;
 EXECUTE 'SET LOCAL ROLE service_role';
 rows:=public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1);
 IF jsonb_array_length(rows)<>0 OR NOT EXISTS(SELECT 1 FROM public.reminder_deliveries WHERE id=d AND status='ambiguous') THEN RAISE EXCEPTION 'started_recovery_resent';END IF;
 EXECUTE 'RESET ROLE';
 EXECUTE 'CREATE TRIGGER fixture_ack_rollback BEFORE UPDATE OF last_chased_at ON public.commitments FOR EACH ROW EXECUTE FUNCTION public.fixture_reminder_ack_failure()';
 EXECUTE 'SET LOCAL ROLE service_role';
 BEGIN
 PERFORM public.finish_reminder_delivery(t,ARRAY['reminder-fixture'],d,token,'sent','rollback-switch-reference',NULL);
 RAISE EXCEPTION 'ack_rollback_not_triggered';
 EXCEPTION WHEN raise_exception THEN IF SQLERRM<>'fixture_ack_rollback' THEN RAISE;END IF;END;
 IF NOT EXISTS(SELECT 1 FROM public.reminder_deliveries WHERE id=d AND status='ambiguous' AND sent_at IS NULL) THEN RAISE EXCEPTION 'ack_partial_write';END IF;
 EXECUTE 'RESET ROLE';
 EXECUTE 'DROP TRIGGER fixture_ack_rollback ON public.commitments';
 -- Invalidation after expiry must not discard a genuine late acknowledgement.
 UPDATE public.reminder_deliveries SET status='superseded',superseded_at=now() WHERE id=d;
 EXECUTE 'SET LOCAL ROLE service_role';
 -- A late acknowledgement for the same quarantined attempt can still record truth.
 IF NOT public.finish_reminder_delivery(t,ARRAY['reminder-fixture'],d,token,'sent','late-switch-reference',NULL) THEN RAISE EXCEPTION 'invalidated_late_ack_lost';END IF;
 IF NOT EXISTS(SELECT 1 FROM public.reminder_deliveries WHERE id=d AND status='sent' AND error_code='accepted_after_invalidation') THEN RAISE EXCEPTION 'invalidated_late_ack_missing';END IF;
 EXECUTE 'RESET ROLE';
 UPDATE public.commitments SET version=version+1 WHERE id=c;
 UPDATE public.reminder_deliveries SET sent_at=now()-interval '2 days' WHERE id=d;
 item:=item||jsonb_build_object('occurrence_key',gen_random_uuid(),'commitment_version',3);
 EXECUTE 'SET LOCAL ROLE service_role';
 PERFORM public.persist_reminder_intention(t,ARRAY['reminder-fixture'],item);
 rows:=public.claim_reminder_deliveries(t,ARRAY['reminder-fixture'],1);d:=(rows->0->>'id')::uuid;token:=(rows->0->>'claim_token')::uuid;
 EXECUTE 'RESET ROLE';
 UPDATE public.commitments SET task_status='completed',completed_at=now(),version=version+1 WHERE id=c;
 EXECUTE 'SET LOCAL ROLE service_role';
 IF public.begin_reminder_dispatch(t,ARRAY['reminder-fixture'],d,token,'Must not send') THEN RAISE EXCEPTION 'completed_sent';END IF;
 IF NOT EXISTS(SELECT 1 FROM public.reminder_deliveries WHERE id=d AND status='superseded') THEN RAISE EXCEPTION 'completion_invalidation_failed';END IF;
 -- Null tokens cannot acknowledge an explicitly quarantined row.
 UPDATE public.reminder_deliveries SET status='ambiguous',claim_token=NULL,lease_until=NULL,dispatch_started_at=now() WHERE id=d;
 IF public.finish_reminder_delivery(t,ARRAY['reminder-fixture'],d,NULL,'sent','invalid-null-token',NULL) THEN RAISE EXCEPTION 'null_claim_acknowledged';END IF;
 EXECUTE 'RESET ROLE';
 -- Policy version is maintained by the database, never caller-supplied authority.
 SELECT reminder_policy_version INTO p_version FROM public.commitment_chat_policies WHERE tenant_id=t AND room_id=r;
 UPDATE public.commitment_chat_policies SET reminder_lead_minutes=15 WHERE tenant_id=t AND room_id=r;
 IF (SELECT reminder_policy_version FROM public.commitment_chat_policies WHERE tenant_id=t AND room_id=r)<>p_version+1 THEN RAISE EXCEPTION 'policy_version_failed';END IF;
 IF has_function_privilege('anon','public.claim_reminder_deliveries(uuid,text[],integer)','EXECUTE') OR
 has_function_privilege('authenticated','public.begin_reminder_dispatch(uuid,text[],uuid,uuid,text)','EXECUTE') THEN RAISE EXCEPTION 'client_claim_privilege';END IF;
 IF (SELECT body FROM public.messages WHERE id=m)<>'Original source' THEN RAISE EXCEPTION 'source_rewritten';END IF;
END $$;
ROLLBACK;
