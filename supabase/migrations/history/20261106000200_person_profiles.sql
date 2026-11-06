-- Additive optional profiles. Review before applying remotely. No historical backfill.
BEGIN;
CREATE TABLE public.person_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    room_id uuid NOT NULL,
    switch_author_id text NOT NULL CHECK(length(switch_author_id) BETWEEN 1 AND 512 AND switch_author_id !~ '\s'),
    display_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id,room_id,switch_author_id),
    UNIQUE(tenant_id,room_id,id),
    FOREIGN KEY(tenant_id,room_id) REFERENCES public.rooms(tenant_id,id) ON DELETE CASCADE
);
CREATE TABLE public.person_profile_claims (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    room_id uuid NOT NULL,
    profile_id uuid NOT NULL,
    extraction_key text NOT NULL,
    attribute text NOT NULL CHECK(attribute IN ('responsibility','skill','preference','timezone')),
    value text NOT NULL CHECK(length(value) BETWEEN 1 AND 256),
    status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(profile_id,extraction_key),
    UNIQUE(tenant_id,room_id,id),
    FOREIGN KEY(tenant_id,room_id,profile_id) REFERENCES public.person_profiles(tenant_id,room_id,id) ON DELETE CASCADE
);
ALTER TABLE public.memories ADD CONSTRAINT profile_memory_scope UNIQUE(tenant_id,room_id,id);
CREATE TABLE public.person_profile_evidence (
    tenant_id uuid NOT NULL,
    room_id uuid NOT NULL,
    claim_id uuid NOT NULL,
    message_id uuid NOT NULL,
    memory_id uuid NOT NULL UNIQUE,
    quote text NOT NULL CHECK(length(quote) BETWEEN 1 AND 1000),
    correction boolean NOT NULL,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY(claim_id,message_id),
    FOREIGN KEY(tenant_id,room_id,claim_id) REFERENCES public.person_profile_claims(tenant_id,room_id,id) ON DELETE CASCADE,
    FOREIGN KEY(tenant_id,room_id,message_id) REFERENCES public.messages(tenant_id,room_id,id) ON DELETE CASCADE,
    FOREIGN KEY(tenant_id,room_id,memory_id) REFERENCES public.memories(tenant_id,room_id,id) ON DELETE CASCADE
);
CREATE INDEX profile_evidence_message ON public.person_profile_evidence(message_id);
CREATE INDEX profile_claim_attribute ON public.person_profile_claims(profile_id,attribute);
CREATE VIEW public.person_profile_context WITH (security_invoker=true) AS
SELECT e.*,c.profile_id,c.attribute,c.value,c.status,p.display_name
FROM public.person_profile_evidence e JOIN public.person_profile_claims c ON c.id=e.claim_id
JOIN public.person_profiles p ON p.id=c.profile_id;
REVOKE ALL ON public.person_profile_context FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.person_profile_context TO service_role;
CREATE TABLE public.profile_extractions (
    message_id uuid PRIMARY KEY REFERENCES public.messages(id) ON DELETE CASCADE,
    request_hash text NOT NULL,
    counts jsonb NOT NULL
);
ALTER TABLE public.person_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.person_profile_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.person_profile_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.profile_extractions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.person_profiles,public.person_profile_claims,public.person_profile_evidence,public.profile_extractions FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.person_profiles,public.person_profile_claims,public.person_profile_evidence,public.profile_extractions TO service_role;

CREATE FUNCTION public.refresh_person_profile(p_profile_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
    PERFORM 1 FROM public.person_profiles WHERE id=p_profile_id FOR UPDATE;
    DELETE FROM public.person_profile_claims c WHERE c.profile_id=p_profile_id
      AND NOT EXISTS(SELECT 1 FROM public.person_profile_evidence e WHERE e.claim_id=c.id);
    UPDATE public.person_profile_claims c SET status=CASE WHEN EXISTS(
      SELECT 1 FROM public.person_profile_evidence e WHERE e.claim_id=c.id
      AND e.occurred_at >= coalesce((SELECT max(x.occurred_at) FROM public.person_profile_evidence x
        JOIN public.person_profile_claims other ON other.id=x.claim_id
        WHERE other.profile_id=c.profile_id AND other.attribute=c.attribute AND x.correction),'-infinity'::timestamptz)
    ) THEN 'active' ELSE 'superseded' END WHERE c.profile_id=p_profile_id;
    UPDATE public.memories m SET active=EXISTS(
      SELECT 1 FROM public.person_profile_evidence e JOIN public.person_profile_claims c ON c.id=e.claim_id
      WHERE e.memory_id=m.id AND c.status='active'
    ) WHERE m.id IN (SELECT e.memory_id FROM public.person_profile_evidence e
      JOIN public.person_profile_claims c ON c.id=e.claim_id WHERE c.profile_id=p_profile_id);
    UPDATE public.person_profiles SET display_name=(SELECT m.author_name FROM public.messages m
      JOIN public.person_profile_evidence e ON e.message_id=m.id
      JOIN public.person_profile_claims c ON c.id=e.claim_id WHERE c.profile_id=p_profile_id
      ORDER BY e.occurred_at DESC,m.id DESC LIMIT 1)
      WHERE id=p_profile_id AND EXISTS(SELECT 1 FROM public.person_profile_claims WHERE profile_id=p_profile_id);
    DELETE FROM public.person_profiles p WHERE p.id=p_profile_id
      AND NOT EXISTS(SELECT 1 FROM public.person_profile_claims c WHERE c.profile_id=p.id);
END;
$$;
CREATE FUNCTION public.refresh_profile_evidence() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE profile_uuid uuid;
BEGIN
    SELECT profile_id INTO profile_uuid FROM public.person_profile_claims
      WHERE id=CASE WHEN TG_OP='DELETE' THEN OLD.claim_id ELSE NEW.claim_id END;
    IF profile_uuid IS NOT NULL THEN PERFORM public.refresh_person_profile(profile_uuid); END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER refresh_profile_evidence AFTER INSERT OR DELETE ON public.person_profile_evidence
FOR EACH ROW EXECUTE FUNCTION public.refresh_profile_evidence();

-- Distinct name makes missing deployment fail explicitly; no overload ambiguity.
CREATE FUNCTION public.store_profile_message_extraction(
    p_tenant_id uuid,p_switch_room_id text,p_source jsonb,p_items jsonb,p_capture boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE
    room_uuid uuid; message_uuid uuid; profile_uuid uuid; claim_uuid uuid; memory_uuid uuid;
    owner_id text; item jsonb; counts jsonb; saved public.profile_extractions;
    source public.messages; fingerprint text; quote_text text; pattern text; extracted text[];
BEGIN
    SELECT id INTO room_uuid FROM public.rooms WHERE tenant_id=p_tenant_id
      AND switch_room_id=p_switch_room_id AND enabled FOR SHARE;
    IF room_uuid IS NULL THEN RAISE EXCEPTION 'unauthorized_room' USING ERRCODE='42501'; END IF;
    owner_id:=p_source->>'switch_author_id';
    IF owner_id IS NULL OR length(owner_id) NOT BETWEEN 1 AND 512 OR owner_id ~ '\s'
      OR jsonb_typeof(p_items->'profile_facts') IS DISTINCT FROM 'array'
      OR jsonb_array_length(p_items->'profile_facts') NOT BETWEEN 1 AND 10 THEN
      RAISE EXCEPTION 'invalid_profile_source' USING ERRCODE='22023'; END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
      p_tenant_id::text||':'||room_uuid::text||':'||owner_id,17));
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
      p_tenant_id::text||':'||room_uuid::text||':'||(p_source->>'switch_message_id'),0));
    fingerprint:=md5(jsonb_build_array(p_items,owner_id,p_source->>'body',
      extract(epoch FROM (p_source->>'occurred_at')::timestamptz),p_capture)::text);
    SELECT * INTO source FROM public.messages WHERE tenant_id=p_tenant_id AND room_id=room_uuid
      AND switch_message_id=p_source->>'switch_message_id' FOR UPDATE;
    IF FOUND THEN
      SELECT * INTO saved FROM public.profile_extractions WHERE message_id=source.id;
      IF NOT FOUND OR saved.request_hash<>fingerprint THEN
        RAISE EXCEPTION 'profile_source_conflict' USING ERRCODE='23514'; END IF;
      RETURN saved.counts;
    END IF;
    -- The existing writer and new profile records share this single transaction.
    IF p_capture THEN
      counts:=public.store_message_extraction(p_tenant_id,p_switch_room_id,p_source,p_items,true);
    ELSE
      counts:=public.store_message_extraction(p_tenant_id,p_switch_room_id,p_source,p_items);
    END IF;
    SELECT id INTO STRICT message_uuid FROM public.messages WHERE tenant_id=p_tenant_id AND room_id=room_uuid
      AND switch_message_id=p_source->>'switch_message_id';
    INSERT INTO public.person_profiles(tenant_id,room_id,switch_author_id,display_name)
      VALUES(p_tenant_id,room_uuid,owner_id,p_source->>'author_name')
      ON CONFLICT(tenant_id,room_id,switch_author_id) DO UPDATE SET display_name=public.person_profiles.display_name
      RETURNING id INTO profile_uuid;
    FOR item IN SELECT value FROM jsonb_array_elements(p_items->'profile_facts') LOOP
      quote_text:=regexp_replace(btrim(p_source->>'body',E' \t\n\r'),'^(?:@[\w.-]+|<@!?[A-Za-z0-9_-]+>)[,:]?\s+','');
      IF item->>'evidence' IS DISTINCT FROM btrim(p_source->>'body',E' \t\n\r')
        OR quote_text ~* '\m(if|might|maybe|perhaps|would|could|not|never|quote|said|hypothetically)\M'
        OR jsonb_typeof(item->'correction') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION 'invalid_profile_evidence' USING ERRCODE='22023'; END IF;
      IF (item->>'correction')::boolean THEN
        pattern:=CASE item->>'attribute' WHEN 'responsibility' THEN 'My responsibility is now '
          WHEN 'skill' THEN 'My specialty is now ' WHEN 'preference' THEN 'My preference is now '
          WHEN 'timezone' THEN 'My timezone is now ' END;
      ELSE
        pattern:=CASE item->>'attribute' WHEN 'responsibility' THEN 'I maintain '
          WHEN 'skill' THEN 'I (?:specialize in|am skilled in) ' WHEN 'preference' THEN 'I prefer '
          WHEN 'timezone' THEN 'My timezone is ' END;
      END IF;
      extracted:=regexp_match(quote_text,'^'||pattern||'([[:alnum:]_ /+#.:-]+?)\.?$','i');
      IF pattern IS NULL OR extracted IS NULL OR btrim(extracted[1]) IS DISTINCT FROM item->>'value'
        OR item->>'extraction_key' NOT LIKE 'profile-%' THEN
        RAISE EXCEPTION 'invalid_profile_evidence' USING ERRCODE='22023'; END IF;
      INSERT INTO public.person_profile_claims(tenant_id,room_id,profile_id,extraction_key,attribute,value)
        VALUES(p_tenant_id,room_uuid,profile_uuid,item->>'extraction_key',item->>'attribute',item->>'value')
        ON CONFLICT(profile_id,extraction_key) DO UPDATE SET extraction_key=public.person_profile_claims.extraction_key
        RETURNING id INTO claim_uuid;
      SELECT id INTO memory_uuid FROM public.memories WHERE tenant_id=p_tenant_id AND room_id=room_uuid
        AND message_id=message_uuid AND memory_type='fact' AND extraction_key=item->>'extraction_key';
      IF memory_uuid IS NULL THEN RAISE EXCEPTION 'profile_summary_missing' USING ERRCODE='23514'; END IF;
      INSERT INTO public.person_profile_evidence(tenant_id,room_id,claim_id,message_id,memory_id,quote,correction,occurred_at)
        VALUES(p_tenant_id,room_uuid,claim_uuid,message_uuid,memory_uuid,item->>'evidence',
          (item->>'correction')::boolean,(p_source->>'occurred_at')::timestamptz);
    END LOOP;
    PERFORM public.refresh_person_profile(profile_uuid);
    INSERT INTO public.profile_extractions VALUES(message_uuid,fingerprint,counts);
    RETURN counts;
END;
$$;
REVOKE ALL ON FUNCTION public.refresh_person_profile(uuid),public.refresh_profile_evidence(),
  public.store_profile_message_extraction(uuid,text,jsonb,jsonb,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.refresh_person_profile(uuid),public.refresh_profile_evidence(),
  public.store_profile_message_extraction(uuid,text,jsonb,jsonb,boolean) TO service_role;
COMMIT;
