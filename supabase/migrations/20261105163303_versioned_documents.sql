-- Additive document module. Never replay the destructive core baseline remotely.
BEGIN;
CREATE TABLE public.documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    room_id uuid NOT NULL,
    source_key text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 200),
    source_type text NOT NULL CHECK (source_type IN ('markdown','text')),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    created_by text NOT NULL,
    latest_version integer NOT NULL DEFAULT 0 CHECK (latest_version >= 0),
    published_version_id uuid,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,room_id,source_key),
    UNIQUE (tenant_id,room_id,id),
    FOREIGN KEY (tenant_id,room_id) REFERENCES public.rooms(tenant_id,id) ON DELETE RESTRICT
);
CREATE TABLE public.document_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    room_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_number integer NOT NULL CHECK (version_number > 0),
    title text NOT NULL,
    filename text NOT NULL,
    source_text text NOT NULL CHECK (octet_length(source_text) BETWEEN 1 AND 262144),
    content_hash text NOT NULL CHECK (content_hash = encode(sha256(convert_to(source_text,'UTF8')),'hex')),
    recipe text NOT NULL,
    model_recipe text NOT NULL,
    status text NOT NULL DEFAULT 'processing' CHECK (status IN ('processing','completed','failed')),
    error text CHECK (error IN ('model_not_installed','model_checksum_failed','dependencies_missing',
        'invalid_vector','invalid_input','input_too_long','inference_failed','lease_exhausted','version_superseded')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id,version_number),
    UNIQUE (tenant_id,room_id,document_id,id),
    FOREIGN KEY (tenant_id,room_id,document_id) REFERENCES public.documents(tenant_id,room_id,id) ON DELETE RESTRICT
);
ALTER TABLE public.documents ADD CONSTRAINT documents_published_version_fk
    FOREIGN KEY (tenant_id,room_id,id,published_version_id)
    REFERENCES public.document_versions(tenant_id,room_id,document_id,id) ON DELETE RESTRICT;
CREATE TABLE public.document_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    room_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    text text NOT NULL CHECK (length(text)>0),
    content_hash text NOT NULL CHECK (content_hash=encode(sha256(convert_to(text,'UTF8')),'hex')),
    start_char integer NOT NULL CHECK (start_char>=0),
    end_char integer NOT NULL CHECK (end_char>start_char),
    start_line integer NOT NULL CHECK (start_line>0),
    end_line integer NOT NULL CHECK (end_line>=start_line),
    section text,
    model_recipe text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    error text CHECK (error IN ('model_not_installed','model_checksum_failed','dependencies_missing',
        'invalid_vector','invalid_input','input_too_long','inference_failed','lease_exhausted','version_superseded')),
    retry_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    claim_token uuid,
    embedding extensions.vector(384),
    embedding_content_hash text,
    CHECK ((status='completed') = (embedding IS NOT NULL)),
    CHECK (status<>'completed' OR embedding_content_hash IS NOT DISTINCT FROM content_hash),
    UNIQUE (version_id,position),
    FOREIGN KEY (tenant_id,room_id,document_id,version_id)
        REFERENCES public.document_versions(tenant_id,room_id,document_id,id) ON DELETE RESTRICT
);
CREATE TABLE public.document_imports (
    tenant_id uuid NOT NULL,
    room_id uuid NOT NULL,
    import_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    request_hash text NOT NULL,
    PRIMARY KEY (tenant_id,import_id),
    FOREIGN KEY (tenant_id,room_id,document_id,version_id)
        REFERENCES public.document_versions(tenant_id,room_id,document_id,id) ON DELETE RESTRICT
);
CREATE INDEX document_chunks_queue ON public.document_chunks(retry_at)
    WHERE status IN ('pending','failed');
CREATE INDEX document_chunks_lease ON public.document_chunks(lease_until) WHERE status='processing';
CREATE INDEX document_chunks_scope ON public.document_chunks(tenant_id,room_id,version_id);
-- Exact cosine search for the bounded initial corpus. No unbenchmarked ANN claim.
ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document_imports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.documents,public.document_versions,public.document_chunks,public.document_imports FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.documents,public.document_versions,public.document_chunks,public.document_imports TO service_role;

CREATE FUNCTION public.document_operation(p_tenant_id uuid,p_room_ids uuid[],p_operation text,p_data jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,extensions AS $$
DECLARE
    d public.documents%ROWTYPE;
    v public.document_versions%ROWTYPE;
    c public.document_chunks%ROWTYPE;
    prior public.document_imports%ROWTYPE;
    piece jsonb;
    result jsonb;
    duplicate boolean := false;
    document_uuid uuid;
    vector_value extensions.vector(384);
    error_value text;
BEGIN
    IF p_tenant_id IS NULL OR coalesce(cardinality(p_room_ids),0)=0 OR
        EXISTS (SELECT 1 FROM unnest(p_room_ids) room WHERE NOT EXISTS (
            SELECT 1 FROM public.rooms r WHERE r.id=room AND r.tenant_id=p_tenant_id AND r.enabled)) THEN
        RAISE EXCEPTION 'document_scope_denied' USING ERRCODE='42501';
    END IF;
    IF p_operation='import_status' THEN
        SELECT * INTO prior FROM public.document_imports WHERE tenant_id=p_tenant_id AND room_id=ANY(p_room_ids)
            AND import_id=(p_data->>'import_id')::uuid;
        IF NOT FOUND THEN RAISE EXCEPTION 'document_not_found' USING ERRCODE='P0002'; END IF;
        SELECT * INTO v FROM public.document_versions WHERE id=prior.version_id;
        RETURN jsonb_build_object('import_id',prior.import_id,'document_id',prior.document_id,'version_id',v.id,
            'version_number',v.version_number,'status',v.status,'duplicate',true);
    ELSIF p_operation='import' THEN
        -- Serialize both idempotency identity and stable source identity, in fixed order.
        PERFORM pg_advisory_xact_lock(hashtextextended(p_tenant_id::text||':'||(p_data->>'import_id'),0));
        PERFORM pg_advisory_xact_lock(hashtextextended(p_tenant_id::text||':'||(p_data->>'room_id')||':'||(p_data->>'source_key'),1));
        SELECT * INTO prior FROM public.document_imports WHERE tenant_id=p_tenant_id AND import_id=(p_data->>'import_id')::uuid;
        IF FOUND THEN
            IF prior.request_hash<>p_data->>'request_hash' THEN
                RAISE EXCEPTION 'document_version_conflict' USING ERRCODE='40001';
            END IF;
            IF NOT prior.room_id=ANY(p_room_ids) THEN RAISE EXCEPTION 'document_scope_denied' USING ERRCODE='42501'; END IF;
            SELECT * INTO d FROM public.documents WHERE id=prior.document_id;
            SELECT * INTO v FROM public.document_versions WHERE id=prior.version_id;
            duplicate:=true;
        ELSE
            IF NOT (p_data->>'room_id')::uuid=ANY(p_room_ids) OR jsonb_array_length(p_data->'chunks') NOT BETWEEN 1 AND 1024 THEN
                RAISE EXCEPTION 'document_invalid_import' USING ERRCODE='22023';
            END IF;
            SELECT * INTO d FROM public.documents WHERE tenant_id=p_tenant_id AND room_id=(p_data->>'room_id')::uuid
                AND source_key=p_data->>'source_key' FOR UPDATE;
            IF coalesce(d.latest_version,0)<>(p_data->>'expected_version')::integer OR d.active=false
                OR (d.id IS NOT NULL AND d.source_type<>p_data->>'source_type') THEN
                RAISE EXCEPTION 'document_version_conflict' USING ERRCODE='40001';
            END IF;
            IF d.id IS NULL THEN
                INSERT INTO public.documents(tenant_id,room_id,source_key,source_type,title,created_by)
                VALUES(p_tenant_id,(p_data->>'room_id')::uuid,p_data->>'source_key',p_data->>'source_type',p_data->>'title',p_data->>'actor') RETURNING * INTO d;
            END IF;
            SELECT * INTO v FROM public.document_versions WHERE document_id=d.id AND version_number=d.latest_version;
            duplicate:=v.id IS NOT NULL AND v.content_hash=p_data->>'content_hash' AND v.recipe=p_data->>'recipe'
                AND v.model_recipe=p_data->>'model_recipe' AND v.title=p_data->>'title';
            IF NOT duplicate THEN
                UPDATE public.document_chunks x SET status='failed',error='version_superseded',claim_token=NULL,lease_until=NULL
                    FROM public.document_versions z WHERE x.version_id=z.id AND z.document_id=d.id AND z.status<>'completed'
                    AND x.status<>'completed';
                UPDATE public.document_versions SET status='failed',error='version_superseded'
                    WHERE document_id=d.id AND status<>'completed';
                INSERT INTO public.document_versions(tenant_id,room_id,document_id,version_number,title,filename,source_text,content_hash,recipe,model_recipe)
                VALUES(p_tenant_id,d.room_id,d.id,d.latest_version+1,p_data->>'title',p_data->>'filename',p_data->>'source_text',
                    p_data->>'content_hash',p_data->>'recipe',p_data->>'model_recipe') RETURNING * INTO v;
                FOR piece IN SELECT value FROM jsonb_array_elements(p_data->'chunks') LOOP
                    -- Locations must describe an exact slice of the preserved source.
                    IF substring(v.source_text FROM (piece->>'start_char')::integer+1
                        FOR (piece->>'end_char')::integer-(piece->>'start_char')::integer)<>piece->>'text' THEN
                        RAISE EXCEPTION 'document_invalid_chunk' USING ERRCODE='22023';
                    END IF;
                    INSERT INTO public.document_chunks(tenant_id,room_id,document_id,version_id,position,text,content_hash,
                        start_char,end_char,start_line,end_line,section,model_recipe)
                    VALUES(p_tenant_id,d.room_id,d.id,v.id,(piece->>'position')::integer,piece->>'text',piece->>'content_hash',
                        (piece->>'start_char')::integer,(piece->>'end_char')::integer,(piece->>'start_line')::integer,
                        (piece->>'end_line')::integer,piece->>'section',v.model_recipe);
                END LOOP;
                UPDATE public.documents SET latest_version=v.version_number WHERE id=d.id RETURNING * INTO d;
            END IF;
            INSERT INTO public.document_imports VALUES(p_tenant_id,d.room_id,(p_data->>'import_id')::uuid,d.id,v.id,p_data->>'request_hash');
        END IF;
        RETURN jsonb_build_object('import_id',p_data->>'import_id','document_id',d.id,'version_id',v.id,
            'version_number',v.version_number,'status',v.status,'duplicate',duplicate);
    ELSIF p_operation='documents' THEN
        SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') INTO result FROM (
            SELECT * FROM public.documents WHERE tenant_id=p_tenant_id AND room_id=ANY(p_room_ids)
            ORDER BY id LIMIT least(greatest((p_data->>'limit')::integer,1),50) OFFSET least(greatest((p_data->>'offset')::integer,0),10000)) q;
        RETURN result;
    ELSIF p_operation='claim' THEN
        -- Lock documents before chunks, just as finish/import/archive do.
        FOR d IN SELECT * FROM public.documents WHERE tenant_id=p_tenant_id AND room_id=ANY(p_room_ids) AND active
            ORDER BY id FOR UPDATE SKIP LOCKED LOOP
            UPDATE public.document_chunks x SET status='failed',error='lease_exhausted',claim_token=NULL
                FROM public.document_versions z WHERE x.version_id=z.id AND z.document_id=d.id AND z.version_number=d.latest_version
                AND x.status='processing' AND x.attempts>=3 AND x.lease_until<=now();
            UPDATE public.document_versions z SET status='failed',error='lease_exhausted' WHERE z.document_id=d.id
                AND z.version_number=d.latest_version AND EXISTS(SELECT 1 FROM public.document_chunks x WHERE x.version_id=z.id AND x.error='lease_exhausted');
        END LOOP;
        WITH picked AS (
            SELECT x.id FROM public.document_chunks x JOIN public.document_versions z ON z.id=x.version_id
            JOIN public.documents a ON a.id=x.document_id
            WHERE a.tenant_id=p_tenant_id AND a.room_id=ANY(p_room_ids) AND a.active
                AND z.version_number=a.latest_version AND x.attempts<3
                AND ((x.status IN ('pending','failed') AND x.retry_at<=now()) OR (x.status='processing' AND x.lease_until<=now()))
            ORDER BY x.id LIMIT 4 FOR UPDATE OF x SKIP LOCKED
        ), updated AS (
            UPDATE public.document_chunks x SET status='processing',attempts=x.attempts+1,claim_token=gen_random_uuid(),
                lease_until=now()+interval '5 minutes',error=NULL FROM picked WHERE x.id=picked.id RETURNING x.*
        ) SELECT coalesce(jsonb_agg(to_jsonb(updated)-'embedding'),'[]') INTO result FROM updated;
        RETURN result;
    ELSIF p_operation='finish' THEN
        SELECT document_id INTO document_uuid FROM public.document_chunks WHERE id=(p_data->>'id')::uuid
            AND tenant_id=p_tenant_id AND room_id=ANY(p_room_ids);
        IF document_uuid IS NULL THEN RETURN 'false'; END IF;
        SELECT * INTO d FROM public.documents WHERE id=document_uuid FOR UPDATE;
        SELECT * INTO c FROM public.document_chunks WHERE id=(p_data->>'id')::uuid FOR UPDATE;
        SELECT * INTO v FROM public.document_versions WHERE id=c.version_id;
        IF NOT d.active OR d.latest_version<>v.version_number OR c.status<>'processing' OR c.lease_until<=now()
            OR c.claim_token IS DISTINCT FROM (p_data->>'claim_token')::uuid OR c.content_hash<>p_data->>'content_hash'
            OR c.model_recipe<>p_data->>'model_recipe' THEN RETURN 'false'; END IF;
        error_value:=p_data->>'error';
        IF error_value IS NULL THEN
            vector_value:=(p_data->>'vector')::extensions.vector(384);
            IF vector_value IS NULL OR abs(extensions.vector_norm(vector_value)-1)>0.001 THEN
                RAISE EXCEPTION 'invalid_vector' USING ERRCODE='22023';
            END IF;
        ELSIF error_value NOT IN ('model_not_installed','model_checksum_failed','dependencies_missing',
            'invalid_vector','invalid_input','input_too_long','inference_failed') THEN
            RAISE EXCEPTION 'invalid_error' USING ERRCODE='22023';
        END IF;
        UPDATE public.document_chunks SET status=CASE WHEN error_value IS NULL THEN 'completed' ELSE 'failed' END,
            error=error_value,embedding=vector_value,embedding_content_hash=CASE WHEN error_value IS NULL THEN content_hash END,
            retry_at=now()+interval '30 seconds',claim_token=NULL,lease_until=NULL WHERE id=c.id;
        IF error_value IS NOT NULL AND c.attempts>=3 THEN
            UPDATE public.document_versions SET status='failed',error=error_value WHERE id=v.id;
        END IF;
        IF NOT EXISTS(SELECT 1 FROM public.document_chunks WHERE version_id=v.id AND status<>'completed') THEN
            UPDATE public.document_versions SET status='completed',error=NULL WHERE id=v.id;
            UPDATE public.documents SET published_version_id=v.id,title=v.title WHERE id=d.id;
        END IF;
        RETURN 'true';
    ELSIF p_operation='search' THEN
        vector_value:=(p_data->>'vector')::extensions.vector(384);
        IF vector_value IS NULL OR abs(extensions.vector_norm(vector_value)-1)>0.001 THEN RAISE EXCEPTION 'invalid_vector' USING ERRCODE='22023'; END IF;
        SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') INTO result FROM (
            SELECT a.id AS document_id,z.id AS version_id,z.version_number,x.id AS chunk_id,z.title,x.section,
                x.start_line,x.end_line,x.text,1-(x.embedding <=> vector_value) AS similarity
            FROM public.document_chunks x JOIN public.documents a ON a.id=x.document_id
            JOIN public.document_versions z ON z.id=x.version_id
            WHERE a.tenant_id=p_tenant_id AND a.room_id=ANY(p_room_ids) AND a.active
                AND a.published_version_id=z.id AND z.status='completed' AND x.status='completed'
                AND x.embedding_content_hash=x.content_hash AND x.model_recipe=p_data->>'model_recipe'
                AND (p_data->>'document_id' IS NULL OR a.id=(p_data->>'document_id')::uuid)
            ORDER BY x.embedding <=> vector_value,x.id LIMIT least(greatest((p_data->>'limit')::integer,1),20)
        ) q;
        RETURN result;
    END IF;
    SELECT * INTO d FROM public.documents WHERE id=(p_data->>'document_id')::uuid AND tenant_id=p_tenant_id
        AND room_id=ANY(p_room_ids) FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'document_not_found' USING ERRCODE='P0002'; END IF;
    IF p_operation='document' THEN RETURN to_jsonb(d);
    ELSIF p_operation='archive' THEN
        IF d.latest_version<>(p_data->>'expected_version')::integer THEN RAISE EXCEPTION 'document_version_conflict' USING ERRCODE='40001'; END IF;
        UPDATE public.documents SET active=false WHERE id=d.id RETURNING * INTO d;
        RETURN to_jsonb(d);
    ELSIF p_operation='versions' THEN
        SELECT coalesce(jsonb_agg(to_jsonb(q)),'[]') INTO result FROM (
            SELECT * FROM public.document_versions WHERE document_id=d.id ORDER BY version_number
            LIMIT least(greatest((p_data->>'limit')::integer,1),50) OFFSET least(greatest((p_data->>'offset')::integer,0),10000)) q;
        RETURN result;
    END IF;
    SELECT * INTO v FROM public.document_versions WHERE id=(p_data->>'version_id')::uuid AND document_id=d.id;
    IF NOT FOUND THEN RAISE EXCEPTION 'document_not_found' USING ERRCODE='P0002'; END IF;
    IF p_operation='version' THEN RETURN to_jsonb(v);
    ELSIF p_operation='chunk' THEN
        SELECT * INTO c FROM public.document_chunks WHERE id=(p_data->>'chunk_id')::uuid AND version_id=v.id;
        IF NOT FOUND THEN RAISE EXCEPTION 'document_not_found' USING ERRCODE='P0002'; END IF;
        RETURN to_jsonb(c)-'embedding'-'claim_token';
    ELSIF p_operation='chunks' THEN
        SELECT coalesce(jsonb_agg(to_jsonb(q)-'embedding'-'claim_token') ,'[]') INTO result FROM (
            SELECT * FROM public.document_chunks WHERE version_id=v.id ORDER BY position
            LIMIT least(greatest((p_data->>'limit')::integer,1),50) OFFSET least(greatest((p_data->>'offset')::integer,0),10000)) q;
        RETURN result;
    END IF;
    RAISE EXCEPTION 'document_invalid_operation' USING ERRCODE='22023';
END;
$$;
REVOKE ALL ON FUNCTION public.document_operation(uuid,uuid[],text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.document_operation(uuid,uuid[],text,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
