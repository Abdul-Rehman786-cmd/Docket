-- Real PostgreSQL/pgvector behavior. Disposable fixtures; the whole transaction rolls back.
BEGIN;
DO $$
DECLARE
    t uuid:=gen_random_uuid(); r uuid:=gen_random_uuid(); r2 uuid:=gen_random_uuid();
    other_t uuid:=gen_random_uuid(); other_r uuid:=gen_random_uuid();
    msg uuid:=gen_random_uuid(); mem uuid; job record; next_job record; ok boolean;
    marker text:='embedding-fixture-'||gen_random_uuid()::text;
    vector1 extensions.vector(384):=array_prepend(1::real,array_fill(0::real,ARRAY[383]))::extensions.vector;
    matches integer;
BEGIN
    INSERT INTO public.tenants(id,name) VALUES(t,marker),(other_t,marker||'-other');
    INSERT INTO public.rooms(id,tenant_id,switch_room_id) VALUES(r,t,marker),(r2,t,marker||'-room2'),(other_r,other_t,marker||'-other');
    INSERT INTO public.messages(id,tenant_id,room_id,switch_message_id,body) VALUES(msg,t,r,marker,'Useful fixture');
    INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content)
        VALUES(t,r,msg,'fact','fact-1','The backend uses FastAPI') RETURNING id INTO mem;
    IF NOT EXISTS(SELECT 1 FROM public.memories WHERE id=mem AND content_hash=encode(sha256(convert_to(content,'UTF8')),'hex')
        AND embedding_status='pending' AND embedding IS NULL) THEN RAISE EXCEPTION 'pending_job_missing'; END IF;
    IF EXISTS(SELECT 1 FROM public.claim_memory_embeddings(other_t,ARRAY[r],8)) THEN RAISE EXCEPTION 'cross_tenant_claim'; END IF;
    IF EXISTS(SELECT 1 FROM public.claim_memory_embeddings(t,ARRAY[r2],8)) THEN RAISE EXCEPTION 'cross_room_claim'; END IF;
    SELECT * INTO job FROM public.claim_memory_embeddings(t,ARRAY[r],8);
    IF job.id IS DISTINCT FROM mem THEN RAISE EXCEPTION 'claim_missing'; END IF;
    IF EXISTS(SELECT 1 FROM public.claim_memory_embeddings(t,ARRAY[r],8)) THEN RAISE EXCEPTION 'duplicate_claim'; END IF;
    -- Edit between claim and completion. An old token cannot attach its vector.
    UPDATE public.memories SET content='The backend API uses FastAPI and Python' WHERE id=mem;
    ok:=public.finish_memory_embedding(mem,t,r,job.claim_token,job.content_hash,vector1);
    IF ok THEN RAISE EXCEPTION 'stale_vector_accepted'; END IF;
    SELECT * INTO next_job FROM public.claim_memory_embeddings(t,ARRAY[r],8);
    ok:=public.finish_memory_embedding(mem,t,r,next_job.claim_token,next_job.content_hash,NULL,'inference_failed');
    IF NOT ok THEN RAISE EXCEPTION 'failure_not_saved'; END IF;
    IF (SELECT embedding_status FROM public.memories WHERE id=mem)<>'failed' THEN RAISE EXCEPTION 'failure_state_missing'; END IF;
    UPDATE public.memories SET embedding_next_attempt_at=now()-interval '1 second' WHERE id=mem;
    SELECT * INTO job FROM public.claim_memory_embeddings(t,ARRAY[r],8);
    -- Simulate process death and lease expiry. A fresh worker claims the same row.
    UPDATE public.memories SET embedding_lease_until=now()-interval '1 second' WHERE id=mem;
    SELECT * INTO next_job FROM public.claim_memory_embeddings(t,ARRAY[r],8);
    IF next_job.id IS DISTINCT FROM mem OR next_job.claim_token=job.claim_token THEN RAISE EXCEPTION 'lease_recovery_failed'; END IF;
    IF public.finish_memory_embedding(mem,t,r,job.claim_token,job.content_hash,vector1) THEN RAISE EXCEPTION 'old_lease_completed'; END IF;
    IF NOT public.finish_memory_embedding(mem,t,r,next_job.claim_token,next_job.content_hash,vector1) THEN RAISE EXCEPTION 'completion_failed'; END IF;
    IF (SELECT count(*) FROM public.memories WHERE tenant_id=t)<>1 THEN RAISE EXCEPTION 'retry_duplicated_memory'; END IF;
    SELECT count(*) INTO matches FROM public.search_memories(t,r,vector1,5) WHERE message_id=msg;
    IF matches<>1 THEN RAISE EXCEPTION 'source_reference_missing'; END IF;
    IF EXISTS(SELECT 1 FROM public.search_memories(t,r2,vector1,5)) OR EXISTS(SELECT 1 FROM public.search_memories(other_t,r,vector1,5)) THEN
        RAISE EXCEPTION 'search_scope_leak'; END IF;
    UPDATE public.memories SET active=false WHERE id=mem;
    IF EXISTS(SELECT 1 FROM public.search_memories(t,r,vector1,5)) THEN RAISE EXCEPTION 'inactive_match'; END IF;
    UPDATE public.memories SET active=true,content='Edited again, vector must disappear' WHERE id=mem;
    IF EXISTS(SELECT 1 FROM public.search_memories(t,r,vector1,5)) THEN RAISE EXCEPTION 'pending_vector_match'; END IF;
    -- Exhausted crashed jobs become failed, rather than remaining processing forever.
    SELECT * INTO job FROM public.claim_memory_embeddings(t,ARRAY[r],8);
    UPDATE public.memories SET embedding_attempts=3,embedding_lease_until=now()-interval '1 second' WHERE id=mem;
    PERFORM 1 FROM public.claim_memory_embeddings(t,ARRAY[r],8);
    IF (SELECT embedding_status FROM public.memories WHERE id=mem)<>'failed' THEN RAISE EXCEPTION 'exhausted_lease_not_failed'; END IF;
    IF EXISTS(SELECT 1 FROM public.claim_memory_embeddings(t,ARRAY[r],8)) THEN RAISE EXCEPTION 'unbounded_retry'; END IF;
    IF has_function_privilege('anon','public.claim_memory_embeddings(uuid,uuid[],integer)','EXECUTE') OR
       has_function_privilege('authenticated','public.finish_memory_embedding(uuid,uuid,uuid,uuid,text,extensions.vector,text)','EXECUTE') THEN
        RAISE EXCEPTION 'public_embedding_rpc'; END IF;
END $$;
ROLLBACK;
SELECT 'embedding_jobs_scope_staleness_recovery_and_search_passed' AS result;
