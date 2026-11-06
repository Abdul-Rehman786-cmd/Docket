-- REVIEW BEFORE HOSTED DEPLOYMENT. Requires lifecycle + chat migrations.
-- Extends existing room policy and reminder_deliveries; does not reset core data.
BEGIN;
ALTER TABLE public.commitment_chat_policies
 ADD COLUMN reminders_enabled boolean NOT NULL DEFAULT false,
 ADD COLUMN reminder_policy_version bigint NOT NULL DEFAULT 1 CHECK(reminder_policy_version>0),
 ADD COLUMN reminder_lead_minutes integer NOT NULL DEFAULT 30 CHECK(reminder_lead_minutes BETWEEN 0 AND 10080),
 ADD COLUMN reminder_cadence_minutes integer NOT NULL DEFAULT 1440 CHECK(reminder_cadence_minutes BETWEEN 60 AND 43200),
 ADD COLUMN reminder_max_followups integer NOT NULL DEFAULT 3 CHECK(reminder_max_followups BETWEEN 0 AND 10),
 ADD COLUMN reminder_gap_minutes integer NOT NULL DEFAULT 30 CHECK(reminder_gap_minutes BETWEEN 1 AND 1440),
 ADD COLUMN reminder_room_gap_seconds integer NOT NULL DEFAULT 60 CHECK(reminder_room_gap_seconds BETWEEN 15 AND 3600),
 ADD COLUMN reminder_quiet_start integer NOT NULL DEFAULT 22 CHECK(reminder_quiet_start BETWEEN 0 AND 23),
 ADD COLUMN reminder_quiet_end integer NOT NULL DEFAULT 8 CHECK(reminder_quiet_end BETWEEN 0 AND 23),
 ADD COLUMN reminder_delivery_mode text NOT NULL DEFAULT 'source_thread' CHECK(reminder_delivery_mode IN ('source_thread','room'));
ALTER TABLE public.reminder_deliveries DROP CONSTRAINT reminder_deliveries_status_check;
ALTER TABLE public.reminder_deliveries ADD CONSTRAINT reminder_deliveries_status_check
 CHECK(status IN ('pending','sending','sent','failed','superseded','ambiguous'));
ALTER TABLE public.reminder_deliveries
 ADD COLUMN occurrence_key uuid,
 ADD COLUMN recipient_id text,
 ADD COLUMN commitment_version bigint,
 ADD COLUMN policy_version bigint,
 ADD COLUMN reminder_type text CHECK(reminder_type IN ('upcoming','overdue')),
 ADD COLUMN ordinal integer CHECK(ordinal BETWEEN 0 AND 10),
 ADD COLUMN occurrence_base timestamptz,
 ADD COLUMN dispatch_started_at timestamptz,
 ADD COLUMN superseded_at timestamptz,
 ADD COLUMN notification_body text CHECK(length(notification_body)<=2000);
-- Unversioned unsent legacy rows cannot prove current intent; retain them as history.
UPDATE public.reminder_deliveries SET status='superseded',error_code='legacy_unversioned',superseded_at=now(),claim_token=NULL,lease_until=NULL
 WHERE status<>'sent';
ALTER TABLE public.reminder_deliveries DROP CONSTRAINT reminder_deliveries_commitment_id_scheduled_for_key;
CREATE UNIQUE INDEX reminder_occurrence_unique ON public.reminder_deliveries(occurrence_key) WHERE occurrence_key IS NOT NULL;
CREATE UNIQUE INDEX reminder_logical_unique ON public.reminder_deliveries(commitment_id,commitment_version,policy_version,reminder_type,ordinal)
 WHERE occurrence_key IS NOT NULL;
CREATE UNIQUE INDEX reminder_one_active ON public.reminder_deliveries(commitment_id)
 WHERE occurrence_key IS NOT NULL AND status IN ('pending','sending');
ALTER TABLE public.reminder_deliveries ADD CONSTRAINT reminder_versioned_fields CHECK(occurrence_key IS NULL OR (
 recipient_id IS NOT NULL AND commitment_version>0 AND policy_version>0 AND reminder_type IS NOT NULL
 AND ordinal IS NOT NULL AND occurrence_base IS NOT NULL));
ALTER TABLE public.reminder_deliveries ADD CONSTRAINT reminder_claim_fields CHECK(status<>'sending' OR (claim_token IS NOT NULL AND lease_until IS NOT NULL));

CREATE FUNCTION public.reminder_policy_json(p public.commitment_chat_policies) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT jsonb_build_object('version',p.reminder_policy_version,'enabled',p.reminders_enabled,'timezone',p.timezone,
 'lead_minutes',p.reminder_lead_minutes,'cadence_minutes',p.reminder_cadence_minutes,'max_followups',p.reminder_max_followups,
 'minimum_gap_minutes',p.reminder_gap_minutes,'room_gap_seconds',p.reminder_room_gap_seconds,
 'quiet_start',p.reminder_quiet_start,'quiet_end',p.reminder_quiet_end,'delivery_mode',p.reminder_delivery_mode);
$$;
CREATE FUNCTION public.reminder_quiet(p public.commitment_chat_policies,at_time timestamptz) RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT CASE WHEN p.reminder_quiet_start=p.reminder_quiet_end THEN false
 WHEN p.reminder_quiet_start>p.reminder_quiet_end THEN h>=p.reminder_quiet_start OR h<p.reminder_quiet_end
 ELSE h>=p.reminder_quiet_start AND h<p.reminder_quiet_end END
 FROM (SELECT extract(hour FROM at_time AT TIME ZONE p.timezone) h) s;
$$;
CREATE FUNCTION public.reminder_record_eligible(c public.commitments) RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT coalesce((public.commitment_reminder(c)->>'eligible')::boolean,false) AND EXISTS(
 SELECT 1 FROM public.rooms r JOIN public.commitment_chat_policies p ON p.tenant_id=r.tenant_id AND p.room_id=r.id
 JOIN public.room_members m ON m.tenant_id=r.tenant_id AND m.room_id=r.id AND m.switch_user_id=c.owner_switch_user_id
 WHERE r.tenant_id=c.tenant_id AND r.id=c.room_id AND r.enabled AND r.platform='discord' AND p.reminders_enabled
 AND p.timezone IS NOT NULL AND EXISTS(SELECT 1 FROM pg_catalog.pg_timezone_names z WHERE z.name=p.timezone));
$$;
CREATE FUNCTION public.reminder_delivery_current(d public.reminder_deliveries) RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT EXISTS(SELECT 1 FROM public.commitments c JOIN public.commitment_chat_policies p ON p.tenant_id=c.tenant_id AND p.room_id=c.room_id
 WHERE c.tenant_id=d.tenant_id AND c.room_id=d.room_id AND c.id=d.commitment_id AND public.reminder_record_eligible(c)
 AND c.version=d.commitment_version AND p.reminder_policy_version=d.policy_version AND c.owner_switch_user_id=d.recipient_id
 AND (d.reminder_type<>'upcoming' OR c.due_at>now()));
$$;
CREATE FUNCTION public.reminder_history_json(c public.commitments,p public.commitment_chat_policies) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT jsonb_build_object(
 'upcoming_sent',coalesce(bool_or(status='sent' AND reminder_type='upcoming' AND commitment_version=c.version AND policy_version=p.reminder_policy_version),false),
 'overdue_sent',count(*) FILTER(WHERE status='sent' AND reminder_type='overdue' AND commitment_version=c.version AND policy_version=p.reminder_policy_version),
 'last_sent_at',max(sent_at) FILTER(WHERE status='sent'),
 'last_room_sent_at',(SELECT max(room_history.sent_at) FROM public.reminder_deliveries room_history WHERE room_history.tenant_id=c.tenant_id AND room_history.room_id=c.room_id AND room_history.status='sent'),
 'last_overdue_at',max(sent_at) FILTER(WHERE status='sent' AND reminder_type='overdue' AND commitment_version=c.version AND policy_version=p.reminder_policy_version),
 'blocking_delivery',coalesce(bool_or(commitment_version=c.version AND policy_version=p.reminder_policy_version
 AND status IN ('pending','sending','failed','ambiguous') AND
 (status='ambiguous' OR (status='failed' AND next_attempt_at IS NULL) OR reminder_type<>'upcoming' OR c.due_at>now() OR dispatch_started_at IS NOT NULL)),false))
 FROM public.reminder_deliveries WHERE tenant_id=c.tenant_id AND room_id=c.room_id AND commitment_id=c.id;
$$;
CREATE FUNCTION public.reminder_work_json(c public.commitments) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT jsonb_build_object('commitment',to_jsonb(c),'policy',public.reminder_policy_json(p),'history',public.reminder_history_json(c,p),
 'room_enabled',r.enabled,'assignee_is_member',EXISTS(SELECT 1 FROM public.room_members m WHERE m.tenant_id=c.tenant_id AND m.room_id=c.room_id AND m.switch_user_id=c.owner_switch_user_id),
 'switch_room_id',r.switch_room_id,'platform',r.platform,'thread_id',m.thread_id)
 FROM public.rooms r JOIN public.commitment_chat_policies p ON p.tenant_id=r.tenant_id AND p.room_id=r.id
 JOIN public.messages m ON m.id=c.message_id AND m.tenant_id=c.tenant_id AND m.room_id=c.room_id WHERE r.id=c.room_id AND r.tenant_id=c.tenant_id;
$$;
CREATE FUNCTION public.reminder_delivery_json(d public.reminder_deliveries) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT to_jsonb(d)||jsonb_build_object('intention',jsonb_build_object('occurrence_key',d.occurrence_key,'tenant_id',d.tenant_id,
 'room_id',d.room_id,'commitment_id',d.commitment_id,'recipient_id',d.recipient_id,'commitment_version',d.commitment_version,
 'policy_version',d.policy_version,'kind',d.reminder_type,'ordinal',d.ordinal,'occurrence_base',d.occurrence_base,'scheduled_for',d.scheduled_for));
$$;

CREATE FUNCTION public.invalidate_commitment_reminders() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
 UPDATE public.reminder_deliveries SET superseded_at=now(),
 status=CASE WHEN status='sending' AND dispatch_started_at IS NOT NULL THEN status ELSE 'superseded' END
 WHERE commitment_id=NEW.id AND tenant_id=NEW.tenant_id AND room_id=NEW.room_id AND status IN ('pending','sending','failed','ambiguous')
 AND commitment_version IS DISTINCT FROM NEW.version;
 RETURN NEW;
END; $$;
CREATE TRIGGER commitments_invalidate_reminders AFTER UPDATE OF version ON public.commitments
 FOR EACH ROW EXECUTE FUNCTION public.invalidate_commitment_reminders();
CREATE FUNCTION public.version_reminder_policy() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
 IF (to_jsonb(NEW)-'lock_marker'-'enabled'-'accept_pending_owner_completion'-'handoff'-'reminder_policy_version')
 IS DISTINCT FROM (to_jsonb(OLD)-'lock_marker'-'enabled'-'accept_pending_owner_completion'-'handoff'-'reminder_policy_version') THEN
   NEW.reminder_policy_version:=OLD.reminder_policy_version+1;
 ELSE NEW.reminder_policy_version:=OLD.reminder_policy_version; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER reminder_policy_version BEFORE UPDATE ON public.commitment_chat_policies FOR EACH ROW EXECUTE FUNCTION public.version_reminder_policy();
CREATE FUNCTION public.invalidate_policy_reminders() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
 IF NEW.reminder_policy_version<>OLD.reminder_policy_version THEN
 UPDATE public.reminder_deliveries SET superseded_at=now(),
 status=CASE WHEN status='sending' AND dispatch_started_at IS NOT NULL THEN status ELSE 'superseded' END
 WHERE tenant_id=NEW.tenant_id AND room_id=NEW.room_id AND status IN ('pending','sending','failed','ambiguous') AND policy_version<>NEW.reminder_policy_version;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER reminder_policy_invalidate AFTER UPDATE ON public.commitment_chat_policies FOR EACH ROW EXECUTE FUNCTION public.invalidate_policy_reminders();

CREATE FUNCTION public.read_reminder_work(p_tenant_id uuid,p_switch_room_ids text[],p_limit integer,p_after uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE items jsonb; n integer;
BEGIN
 IF p_limit NOT BETWEEN 1 AND 100 OR coalesce(cardinality(p_switch_room_ids),0) NOT BETWEEN 1 AND 32 THEN
 RAISE EXCEPTION USING ERRCODE='PT422',MESSAGE='invalid_action_payload'; END IF;
 SELECT coalesce(jsonb_agg(public.reminder_work_json(c) ORDER BY c.id),'[]'),count(*) INTO items,n FROM (
 SELECT c.* FROM public.commitments c JOIN public.rooms r ON r.tenant_id=c.tenant_id AND r.id=c.room_id
 WHERE c.tenant_id=p_tenant_id AND r.switch_room_id=ANY(p_switch_room_ids) AND public.reminder_record_eligible(c)
 AND (p_after IS NULL OR c.id>p_after) ORDER BY c.id LIMIT p_limit) c;
 RETURN jsonb_build_object('items',items,'next_cursor',CASE WHEN n=p_limit THEN items->(n-1)->'commitment'->>'id' END);
END; $$;

CREATE FUNCTION public.persist_reminder_intention(p_tenant_id uuid,p_switch_room_ids text[],p_intention jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE c public.commitments; p public.commitment_chat_policies; h jsonb; inserted uuid;
BEGIN
 SELECT task.* INTO c FROM public.commitments task JOIN public.rooms r ON r.tenant_id=task.tenant_id AND r.id=task.room_id
 WHERE task.tenant_id=p_tenant_id AND task.id=(p_intention->>'commitment_id')::uuid AND task.room_id=(p_intention->>'room_id')::uuid
 AND r.switch_room_id=ANY(p_switch_room_ids) FOR UPDATE OF task;
 IF NOT FOUND OR NOT public.reminder_record_eligible(c) OR p_intention->>'tenant_id'<>p_tenant_id::text THEN RETURN false; END IF;
 SELECT * INTO p FROM public.commitment_chat_policies WHERE tenant_id=c.tenant_id AND room_id=c.room_id FOR SHARE;
 IF c.version<>(p_intention->>'commitment_version')::bigint OR p.reminder_policy_version<>(p_intention->>'policy_version')::bigint
 OR c.owner_switch_user_id<>p_intention->>'recipient_id' THEN RETURN false; END IF;
 -- Expired upcoming intents are replaced by one current overdue intent, not a backlog.
 UPDATE public.reminder_deliveries SET status='superseded',superseded_at=now() WHERE commitment_id=c.id AND tenant_id=c.tenant_id
 AND status IN ('pending','failed') AND reminder_type='upcoming' AND dispatch_started_at IS NULL AND c.due_at<=now();
 h:=public.reminder_history_json(c,p);
 IF (h->>'blocking_delivery')::boolean THEN RETURN false; END IF;
 IF p_intention->>'kind'='upcoming' THEN
   IF c.due_at<=now() OR (h->>'upcoming_sent')::boolean OR (p_intention->>'ordinal')::integer<>0
      OR now()<c.due_at-make_interval(mins=>p.reminder_lead_minutes) THEN RETURN false; END IF;
 ELSIF p_intention->>'kind'='overdue' THEN
   IF c.due_at>now() OR (h->>'overdue_sent')::integer>=p.reminder_max_followups OR
      (h->>'overdue_sent')::integer<>(p_intention->>'ordinal')::integer THEN RETURN false; END IF;
 ELSE RETURN false; END IF;
 IF (p_intention->>'scheduled_for')::timestamptz<now()-interval '5 minutes' OR
    (p_intention->>'occurrence_base')::timestamptz> (p_intention->>'scheduled_for')::timestamptz THEN RETURN false; END IF;
 INSERT INTO public.reminder_deliveries(tenant_id,room_id,commitment_id,scheduled_for,occurrence_key,recipient_id,commitment_version,policy_version,
 reminder_type,ordinal,occurrence_base,next_attempt_at)
 VALUES(c.tenant_id,c.room_id,c.id,(p_intention->>'scheduled_for')::timestamptz,(p_intention->>'occurrence_key')::uuid,c.owner_switch_user_id,c.version,
 p.reminder_policy_version,p_intention->>'kind',(p_intention->>'ordinal')::integer,(p_intention->>'occurrence_base')::timestamptz,(p_intention->>'scheduled_for')::timestamptz)
 ON CONFLICT DO NOTHING RETURNING id INTO inserted;
 RETURN inserted IS NOT NULL;
END; $$;

CREATE FUNCTION public.claim_reminder_deliveries(p_tenant_id uuid,p_switch_room_ids text[],p_limit integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE result jsonb;
BEGIN
 IF p_limit NOT BETWEEN 1 AND 10 THEN RAISE EXCEPTION USING ERRCODE='PT422',MESSAGE='invalid_action_payload'; END IF;
 -- Recovery is conservative: an expired claim after durable send-start is uncertain.
 UPDATE public.reminder_deliveries d SET status=CASE WHEN dispatch_started_at IS NOT NULL THEN 'ambiguous' WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,
 error_code=CASE WHEN dispatch_started_at IS NOT NULL THEN 'send_outcome_unknown' WHEN attempts>=3 THEN 'attempts_exhausted' END,
 next_attempt_at=CASE WHEN dispatch_started_at IS NULL AND attempts<3 THEN now() END,
 claim_token=CASE WHEN dispatch_started_at IS NOT NULL THEN claim_token END,lease_until=NULL
 WHERE d.id IN(SELECT d.id FROM public.reminder_deliveries d JOIN public.rooms r ON r.tenant_id=d.tenant_id AND r.id=d.room_id
 WHERE d.tenant_id=p_tenant_id AND r.switch_room_id=ANY(p_switch_room_ids) AND d.status='sending' AND d.lease_until<=now()
 ORDER BY d.id LIMIT 100 FOR UPDATE OF d SKIP LOCKED);
 UPDATE public.reminder_deliveries d SET status='superseded',superseded_at=now()
 WHERE d.id IN(SELECT d.id FROM public.reminder_deliveries d JOIN public.rooms r ON r.tenant_id=d.tenant_id AND r.id=d.room_id
 WHERE d.tenant_id=p_tenant_id AND r.switch_room_id=ANY(p_switch_room_ids) AND d.status IN ('pending','failed')
 AND NOT public.reminder_delivery_current(d) ORDER BY d.id LIMIT 100 FOR UPDATE OF d SKIP LOCKED);
 WITH picked AS(SELECT d.id FROM public.reminder_deliveries d JOIN public.rooms r ON r.tenant_id=d.tenant_id AND r.id=d.room_id
 WHERE d.tenant_id=p_tenant_id AND r.switch_room_id=ANY(p_switch_room_ids) AND d.status IN ('pending','failed')
 AND d.next_attempt_at<=now() AND d.scheduled_for<=now() AND d.attempts<3 AND public.reminder_delivery_current(d)
 ORDER BY d.next_attempt_at,d.id LIMIT p_limit FOR UPDATE OF d SKIP LOCKED), claimed AS(
 UPDATE public.reminder_deliveries d SET status='sending',claim_token=gen_random_uuid(),lease_until=now()+interval '2 minutes'
 FROM picked WHERE d.id=picked.id RETURNING d.*)
 SELECT coalesce(jsonb_agg(public.reminder_delivery_json(claimed)),'[]') INTO result FROM claimed;
 RETURN result;
END; $$;

CREATE FUNCTION public.read_reminder_dispatch(p_tenant_id uuid,p_switch_room_ids text[],p_delivery_id uuid,p_claim_token uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT jsonb_build_object('delivery',public.reminder_delivery_json(d),'work',public.reminder_work_json(c))
 FROM public.reminder_deliveries d JOIN public.commitments c ON c.tenant_id=d.tenant_id AND c.room_id=d.room_id AND c.id=d.commitment_id
 JOIN public.rooms r ON r.id=d.room_id AND r.tenant_id=d.tenant_id
 WHERE d.id=p_delivery_id AND d.tenant_id=p_tenant_id AND r.switch_room_id=ANY(p_switch_room_ids) AND d.claim_token=p_claim_token
 AND d.status='sending' AND d.lease_until>now() AND public.reminder_delivery_current(d);
$$;

CREATE FUNCTION public.defer_reminder_delivery(p_tenant_id uuid,p_switch_room_ids text[],p_delivery_id uuid,p_claim_token uuid,p_until timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
 UPDATE public.reminder_deliveries d SET status=CASE WHEN p_until IS NULL THEN 'superseded' ELSE 'pending' END,
 superseded_at=CASE WHEN p_until IS NULL THEN now() END,next_attempt_at=p_until,claim_token=NULL,lease_until=NULL
 FROM public.rooms r WHERE d.id=p_delivery_id AND d.tenant_id=p_tenant_id AND r.id=d.room_id AND r.tenant_id=d.tenant_id
 AND r.switch_room_id=ANY(p_switch_room_ids) AND d.claim_token=p_claim_token AND d.status='sending' AND d.dispatch_started_at IS NULL;
 RETURN FOUND;
END; $$;

CREATE FUNCTION public.begin_reminder_dispatch(p_tenant_id uuid,p_switch_room_ids text[],p_delivery_id uuid,p_claim_token uuid,p_body text)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE d public.reminder_deliveries;c public.commitments;p public.commitment_chat_policies;h jsonb;earliest timestamptz;
BEGIN
 -- Lock task before delivery, matching lifecycle invalidation order. No network inside this transaction.
 SELECT task.* INTO c FROM public.commitments task JOIN public.reminder_deliveries outbox ON outbox.commitment_id=task.id AND outbox.tenant_id=task.tenant_id AND outbox.room_id=task.room_id
 JOIN public.rooms r ON r.id=task.room_id AND r.tenant_id=task.tenant_id WHERE outbox.id=p_delivery_id AND task.tenant_id=p_tenant_id AND r.switch_room_id=ANY(p_switch_room_ids) FOR UPDATE OF task;
 IF NOT FOUND THEN RETURN false; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('reminder-room:'||p_tenant_id::text||'/'||c.room_id::text,0));
 -- Acquire the policy lock before the delivery lock; policy invalidation also locks deliveries.
 SELECT * INTO p FROM public.commitment_chat_policies WHERE tenant_id=c.tenant_id AND room_id=c.room_id FOR SHARE;
 SELECT * INTO d FROM public.reminder_deliveries WHERE id=p_delivery_id AND tenant_id=p_tenant_id FOR UPDATE;
 IF d.status<>'sending' OR d.claim_token IS DISTINCT FROM p_claim_token OR d.lease_until<=now() OR d.dispatch_started_at IS NOT NULL THEN RETURN false; END IF;
 IF p.tenant_id IS NULL OR NOT public.reminder_delivery_current(d) THEN
   UPDATE public.reminder_deliveries SET status='superseded',superseded_at=now() WHERE id=d.id; RETURN false; END IF;
 PERFORM 1 FROM public.rooms WHERE id=c.room_id AND tenant_id=c.tenant_id AND enabled FOR SHARE;
 IF NOT FOUND THEN RETURN false; END IF;
 PERFORM 1 FROM public.room_members WHERE tenant_id=c.tenant_id AND room_id=c.room_id AND switch_user_id=d.recipient_id FOR SHARE;
 IF NOT FOUND THEN RETURN false; END IF;
 h:=public.reminder_history_json(c,p);
 IF (d.reminder_type='upcoming' AND (h->>'upcoming_sent')::boolean) OR (d.reminder_type='overdue' AND
 ((h->>'overdue_sent')::integer<>d.ordinal OR d.ordinal>=p.reminder_max_followups)) THEN
 UPDATE public.reminder_deliveries SET status='superseded',superseded_at=now() WHERE id=d.id; RETURN false; END IF;
 earliest:=greatest(d.scheduled_for,d.occurrence_base,coalesce((h->>'last_sent_at')::timestamptz+make_interval(mins=>p.reminder_gap_minutes),d.occurrence_base));
 earliest:=greatest(earliest,coalesce((h->>'last_room_sent_at')::timestamptz+make_interval(secs=>p.reminder_room_gap_seconds),earliest));
 IF EXISTS(SELECT 1 FROM public.reminder_deliveries other WHERE other.tenant_id=c.tenant_id AND other.room_id=c.room_id
   AND other.id<>d.id AND other.status='sending' AND other.dispatch_started_at IS NOT NULL AND other.lease_until>now()) THEN
   earliest:=greatest(earliest,now()+make_interval(secs=>p.reminder_room_gap_seconds));
 END IF;
 IF d.reminder_type='overdue' AND h->>'last_overdue_at' IS NOT NULL THEN earliest:=greatest(earliest,(h->>'last_overdue_at')::timestamptz+make_interval(mins=>p.reminder_cadence_minutes)); END IF;
 IF earliest>now() OR public.reminder_quiet(p,now()) THEN
   UPDATE public.reminder_deliveries SET status='pending',next_attempt_at=greatest(earliest,now()+interval '1 minute'),claim_token=NULL,lease_until=NULL WHERE id=d.id; RETURN false; END IF;
 IF p_body IS NULL OR length(p_body)>2000 OR d.attempts>=3 THEN RETURN false; END IF;
 UPDATE public.reminder_deliveries SET attempts=attempts+1,dispatch_started_at=now(),notification_body=p_body WHERE id=d.id;
 RETURN true;
END; $$;

CREATE FUNCTION public.finish_reminder_delivery(p_tenant_id uuid,p_switch_room_ids text[],p_delivery_id uuid,p_claim_token uuid,
 p_outcome text,p_message_id text DEFAULT NULL,p_error text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE d public.reminder_deliveries;c public.commitments;p public.commitment_chat_policies;current_intent boolean;n integer;
BEGIN
 SELECT task.* INTO c FROM public.commitments task JOIN public.reminder_deliveries outbox ON outbox.commitment_id=task.id AND outbox.tenant_id=task.tenant_id AND outbox.room_id=task.room_id
 JOIN public.rooms r ON r.id=task.room_id AND r.tenant_id=task.tenant_id WHERE outbox.id=p_delivery_id AND task.tenant_id=p_tenant_id AND r.switch_room_id=ANY(p_switch_room_ids) FOR UPDATE OF task;
 IF NOT FOUND THEN RETURN false; END IF;
 SELECT * INTO d FROM public.reminder_deliveries WHERE id=p_delivery_id AND tenant_id=p_tenant_id FOR UPDATE;
 IF p_claim_token IS NULL OR d.claim_token IS NULL OR d.claim_token IS DISTINCT FROM p_claim_token
 OR d.status NOT IN ('sending','ambiguous','superseded') OR d.dispatch_started_at IS NULL THEN RETURN false; END IF;
 current_intent:=public.reminder_delivery_current(d) AND d.superseded_at IS NULL;
 SELECT * INTO p FROM public.commitment_chat_policies WHERE tenant_id=c.tenant_id AND room_id=c.room_id;
 IF p_outcome='sent' THEN
   IF nullif(p_message_id,'') IS NULL OR length(p_message_id)>512 THEN RAISE EXCEPTION USING ERRCODE='PT422',MESSAGE='invalid_action_payload'; END IF;
   UPDATE public.reminder_deliveries SET status='sent',sent_at=now(),switch_reply_message_id=p_message_id,next_attempt_at=NULL,claim_token=NULL,lease_until=NULL,
     error_code=CASE WHEN NOT current_intent THEN 'accepted_after_invalidation' END WHERE id=d.id;
   IF current_intent THEN
     SELECT count(*) INTO n FROM public.reminder_deliveries WHERE commitment_id=c.id AND tenant_id=c.tenant_id AND room_id=c.room_id AND commitment_version=c.version AND policy_version=p.reminder_policy_version AND status='sent' AND reminder_type='overdue';
     UPDATE public.commitments SET last_chased_at=now(),next_reminder_at=CASE WHEN n>=p.reminder_max_followups THEN NULL
       WHEN d.reminder_type='upcoming' THEN greatest(due_at,now()+make_interval(mins=>p.reminder_gap_minutes)) ELSE now()+make_interval(mins=>p.reminder_cadence_minutes) END WHERE id=c.id;
   END IF;
 ELSIF p_outcome IN ('transient','permanent','ambiguous') THEN
   IF p_error IS NULL OR p_error NOT IN ('switch_connect_failed','switch_rate_limited','switch_rejected','send_outcome_unknown','acknowledgement_unavailable','delivery_not_configured') THEN p_error:='send_outcome_unknown'; END IF;
   UPDATE public.reminder_deliveries SET status=CASE WHEN NOT current_intent THEN 'superseded' WHEN p_outcome='ambiguous' THEN 'ambiguous' ELSE 'failed' END,
     next_attempt_at=CASE WHEN current_intent AND p_outcome='transient' AND attempts<3 THEN now()+make_interval(secs=>least(900,30*power(4,attempts-1)::integer)) END,
     error_code=p_error,dispatch_started_at=CASE WHEN p_outcome='transient' THEN NULL ELSE dispatch_started_at END,claim_token=NULL,lease_until=NULL WHERE id=d.id;
 ELSE RAISE EXCEPTION USING ERRCODE='PT422',MESSAGE='invalid_action_payload'; END IF;
 RETURN true;
END; $$;

-- Backend-only functions, with empty search paths. Existing policy provisioning stays privileged.
DO $$ DECLARE f record; BEGIN
 FOR f IN SELECT oid::regprocedure signature FROM pg_catalog.pg_proc WHERE pronamespace='public'::regnamespace
 AND proname IN ('reminder_policy_json','reminder_quiet','reminder_record_eligible','reminder_delivery_current','reminder_history_json',
 'reminder_work_json','reminder_delivery_json','invalidate_commitment_reminders','version_reminder_policy','invalidate_policy_reminders',
 'read_reminder_work','persist_reminder_intention','claim_reminder_deliveries','read_reminder_dispatch','defer_reminder_delivery','begin_reminder_dispatch','finish_reminder_delivery') LOOP
 EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated',f.signature);
 EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',f.signature);
 END LOOP;
END $$;
NOTIFY pgrst,'reload schema';
COMMIT;
