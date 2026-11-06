BEGIN;
DO $$
DECLARE
    tenant uuid:=gen_random_uuid(); room uuid:=gen_random_uuid(); other_room uuid:=gen_random_uuid();
    import_id uuid:=gen_random_uuid(); source text:='# Design' || E'\n\n' || 'The backend uses FastAPI. café 🚀';
    payload jsonb; first jsonb; duplicate jsonb; replacement jsonb; latest jsonb; jobs jsonb; hits jsonb;
    job jsonb; vector jsonb:=to_jsonb(ARRAY[1.0]::float8[] || array_fill(0.0::float8,ARRAY[383]));
    fixture_document_id uuid; first_version uuid; failed boolean;
BEGIN
    INSERT INTO public.tenants(id,name) VALUES(tenant,'document fixture');
    INSERT INTO public.rooms(id,tenant_id,switch_room_id) VALUES(room,tenant,'doc-fixture-'||room),(other_room,tenant,'doc-other-'||other_room);
    payload:=jsonb_build_object('import_id',import_id,'source_key','design','filename','design.md','source_type','markdown',
        'title','Design','expected_version',0,'room_id',room,'actor','development-admin','source_text',source,
        'content_hash',encode(sha256(convert_to(source,'UTF8')),'hex'),'recipe','chunk-v1','model_recipe','model-v1',
        'request_hash','request-1','chunks',jsonb_build_array(jsonb_build_object('position',0,'text',source,
            'content_hash',encode(sha256(convert_to(source,'UTF8')),'hex'),'start_char',0,'end_char',length(source),
            'start_line',1,'end_line',3,'section','Design')));
    first:=public.document_operation(tenant,ARRAY[room],'import',payload);
    fixture_document_id:=(first->>'document_id')::uuid; first_version:=(first->>'version_id')::uuid;
    IF first->>'status'<>'processing' THEN RAISE EXCEPTION 'premature completion'; END IF;
    duplicate:=public.document_operation(tenant,ARRAY[room],'import',payload);
    IF duplicate->>'version_id'<>first->>'version_id' OR duplicate->>'duplicate'<>'true' THEN RAISE EXCEPTION 'idempotency'; END IF;
    failed:=false;
    BEGIN
        PERFORM public.document_operation(tenant,ARRAY[room],'import',payload||'{"request_hash":"changed"}');
    EXCEPTION WHEN serialization_failure THEN failed:=true; END;
    IF NOT failed THEN RAISE EXCEPTION 'idempotency collision accepted'; END IF;
    duplicate:=public.document_operation(tenant,ARRAY[room],'import',payload||jsonb_build_object('import_id',gen_random_uuid(),'expected_version',1,'request_hash','unchanged'));
    IF duplicate->>'version_id'<>first->>'version_id' OR (SELECT count(*) FROM public.document_chunks WHERE document_id=fixture_document_id)=0 THEN RAISE EXCEPTION 'unchanged dedupe'; END IF;
    hits:=public.document_operation(tenant,ARRAY[room],'search',jsonb_build_object('vector',vector,'model_recipe','model-v1','limit',5));
    IF jsonb_array_length(hits)<>0 THEN RAISE EXCEPTION 'incomplete retrieval'; END IF;
    jobs:=public.document_operation(tenant,ARRAY[room],'claim','{}');
    IF jsonb_array_length(jobs)<>1 OR jsonb_array_length(public.document_operation(tenant,ARRAY[room],'claim','{}'))<>0 THEN RAISE EXCEPTION 'duplicate claim'; END IF;
    job:=jobs->0;
    IF public.document_operation(tenant,ARRAY[room],'finish',job||jsonb_build_object('claim_token',gen_random_uuid(),'vector',vector))<>'false' THEN RAISE EXCEPTION 'wrong token'; END IF;
    IF public.document_operation(tenant,ARRAY[room],'finish',job||jsonb_build_object('vector',vector))<>'true' THEN RAISE EXCEPTION 'finish rejected'; END IF;
    hits:=public.document_operation(tenant,ARRAY[room],'search',jsonb_build_object('vector',vector,'model_recipe','model-v1','limit',5));
    IF jsonb_array_length(hits)<>1 OR hits->0->>'text'<>source OR hits->0->>'version_id'<>first->>'version_id' THEN RAISE EXCEPTION 'published retrieval/citation'; END IF;
    -- Metadata-only catalog join used by chat; no source/chunk text is needed.
    IF (SELECT count(*) FROM public.documents a
        JOIN public.rooms r ON (r.tenant_id,r.id)=(a.tenant_id,a.room_id)
        JOIN public.document_versions z ON (z.tenant_id,z.room_id,z.document_id,z.id)=
            (a.tenant_id,a.room_id,a.id,a.published_version_id)
        WHERE a.tenant_id=tenant AND a.room_id=room AND a.active AND r.enabled
            AND z.status='completed' AND z.filename='design.md')<>1 THEN
        RAISE EXCEPTION 'published catalog filename join';
    END IF;
    IF jsonb_array_length(public.document_operation(tenant,ARRAY[other_room],'search',jsonb_build_object('vector',vector,'model_recipe','model-v1','limit',5)))<>0 THEN RAISE EXCEPTION 'room isolation'; END IF;
    failed:=false;
    BEGIN PERFORM public.document_operation(tenant,ARRAY[other_room],'document',jsonb_build_object('document_id',fixture_document_id));
    EXCEPTION WHEN no_data_found THEN failed:=true; END;
    IF NOT failed THEN RAISE EXCEPTION 'document existence leak'; END IF;
    replacement:=public.document_operation(tenant,ARRAY[room],'import',payload||jsonb_build_object('import_id',gen_random_uuid(),'expected_version',1,'title','Replacement','request_hash','replacement'));
    jobs:=public.document_operation(tenant,ARRAY[room],'claim','{}'); job:=jobs->0;
    hits:=public.document_operation(tenant,ARRAY[room],'search',jsonb_build_object('vector',vector,'model_recipe','model-v1','limit',5));
    IF hits->0->>'version_id'<>first_version::text THEN RAISE EXCEPTION 'old version unavailable'; END IF;
    latest:=public.document_operation(tenant,ARRAY[room],'import',payload||jsonb_build_object('import_id',gen_random_uuid(),'expected_version',2,'title','Newest','request_hash','newest'));
    IF public.document_operation(tenant,ARRAY[room],'finish',job||jsonb_build_object('vector',vector))<>'false' THEN RAISE EXCEPTION 'obsolete worker'; END IF;
    failed:=false;
    BEGIN PERFORM public.document_operation(tenant,ARRAY[room],'import',payload||jsonb_build_object('import_id',gen_random_uuid(),'expected_version',2));
    EXCEPTION WHEN serialization_failure THEN failed:=true; END;
    IF NOT failed THEN RAISE EXCEPTION 'stale expected version'; END IF;
    jobs:=public.document_operation(tenant,ARRAY[room],'claim','{}'); job:=jobs->0;
    -- Expired leases reject results, then restart claim creates a different token.
    UPDATE public.document_chunks SET lease_until=now()-interval '1 second' WHERE id=(job->>'id')::uuid;
    IF public.document_operation(tenant,ARRAY[room],'finish',job||jsonb_build_object('vector',vector))<>'false' THEN RAISE EXCEPTION 'expired lease'; END IF;
    jobs:=public.document_operation(tenant,ARRAY[room],'claim','{}');
    IF jobs->0->>'claim_token'=job->>'claim_token' THEN RAISE EXCEPTION 'recovery token unchanged'; END IF;
    job:=jobs->0;
    PERFORM public.document_operation(tenant,ARRAY[room],'finish',job||jsonb_build_object('vector',vector));
    IF (SELECT published_version_id FROM public.documents WHERE id=fixture_document_id)<>(latest->>'version_id')::uuid THEN RAISE EXCEPTION 'publication failed'; END IF;
    PERFORM public.document_operation(tenant,ARRAY[room],'archive',jsonb_build_object('document_id',fixture_document_id,'expected_version',3));
    IF jsonb_array_length(public.document_operation(tenant,ARRAY[room],'search',jsonb_build_object('vector',vector,'model_recipe','model-v1','limit',5)))<>0 THEN RAISE EXCEPTION 'archive searchable'; END IF;
    IF (SELECT source_text FROM public.document_versions WHERE id=first_version)<>source THEN RAISE EXCEPTION 'source lost'; END IF;
    IF EXISTS(SELECT 1 FROM pg_class c WHERE c.oid IN ('public.documents'::regclass,'public.document_versions'::regclass,'public.document_chunks'::regclass,'public.document_imports'::regclass) AND NOT c.relrowsecurity)
        OR has_function_privilege('anon','public.document_operation(uuid,uuid[],text,jsonb)','EXECUTE')
        OR has_function_privilege('authenticated','public.document_operation(uuid,uuid[],text,jsonb)','EXECUTE')
        OR has_table_privilege('anon','public.document_chunks','SELECT') THEN RAISE EXCEPTION 'public document access'; END IF;
    -- A late chunk failure must roll back document, version, earlier chunks and import identity.
    failed:=false;
    BEGIN
        PERFORM public.document_operation(tenant,ARRAY[room],'import',payload||jsonb_build_object('source_key','rollback',
            'import_id',gen_random_uuid(),'expected_version',0,'request_hash','rollback',
            'chunks',(payload->'chunks')||jsonb_build_array((payload->'chunks'->0)||'{"position":1,"text":"wrong slice"}')));
    EXCEPTION WHEN invalid_parameter_value THEN failed:=true; END;
    IF NOT failed OR EXISTS(SELECT 1 FROM public.documents WHERE tenant_id=tenant AND source_key='rollback') THEN RAISE EXCEPTION 'import was not atomic'; END IF;
    UPDATE public.rooms SET enabled=false WHERE id=room;
    failed:=false;
    BEGIN PERFORM public.document_operation(tenant,ARRAY[room],'documents','{"limit":20,"offset":0}');
    EXCEPTION WHEN insufficient_privilege THEN failed:=true; END;
    IF NOT failed THEN RAISE EXCEPTION 'disabled scope accepted'; END IF;
END $$;
ROLLBACK;
SELECT 'passed' AS document_contracts;
