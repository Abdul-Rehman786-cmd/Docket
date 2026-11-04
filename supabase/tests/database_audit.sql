-- Database behavior audit. No schema changes; all fixtures roll back.
-- Run as a database administrator able to SET ROLE.
DO $audit$
DECLARE
    t1 uuid; t2 uuid; r1 uuid; r2 uuid; r3 uuid;
    m1 uuid; m2 uuid; m3 uuid;
    c1 uuid; d1 uuid; d2 uuid; memory1 uuid; delivery1 uuid;
    v1 extensions.vector(384) := array_prepend(1::real,array_fill(0::real,ARRAY[383]))::extensions.vector;
    v2 extensions.vector(384) := array_append(array_fill(0::real,ARRAY[383]),1::real)::extensions.vector;
    passed jsonb := '[]'::jsonb;
    found uuid; score double precision; rows_found integer;
    test_role text; test_table text; operation text; denied integer := 0;
    prefix text := 'audit-' || gen_random_uuid()::text;
    started_at timestamptz := transaction_timestamp();
BEGIN
  -- Subtransaction rollback removes only this audit's fixtures.
  BEGIN
    SET LOCAL ROLE service_role;
    INSERT INTO public.tenants(name) VALUES(prefix) RETURNING id INTO t1;
    INSERT INTO public.tenants(name) VALUES(prefix||'-other') RETURNING id INTO t2;
    INSERT INTO public.rooms(tenant_id,switch_room_id) VALUES(t1,prefix||'-room-1') RETURNING id INTO r1;
    INSERT INTO public.rooms(tenant_id,switch_room_id) VALUES(t1,prefix||'-room-2') RETURNING id INTO r2;
    INSERT INTO public.rooms(tenant_id,switch_room_id) VALUES(t2,prefix||'-room-3') RETURNING id INTO r3;
    INSERT INTO public.room_members(tenant_id,room_id,switch_user_id,role) VALUES(t1,r1,prefix,'approver');
    INSERT INTO public.messages(tenant_id,room_id,switch_message_id,body) VALUES(t1,r1,'audit-event-1','Synthetic audit message') RETURNING id INTO m1;
    INSERT INTO public.messages(tenant_id,room_id,switch_message_id,body) VALUES(t1,r2,'audit-event-1','Synthetic audit other room') RETURNING id INTO m2;
    INSERT INTO public.messages(tenant_id,room_id,switch_message_id,body) VALUES(t2,r3,'audit-event-1','Synthetic audit other tenant') RETURNING id INTO m3;
    INSERT INTO public.commitments(tenant_id,room_id,message_id,extraction_key,description,updated_at)
      VALUES(t1,r1,m1,'item-1','Synthetic task','2000-01-01') RETURNING id INTO c1;
    INSERT INTO public.decisions(tenant_id,room_id,message_id,extraction_key,summary,updated_at)
      VALUES(t1,r1,m1,'item-1','Synthetic decision','2000-01-01') RETURNING id INTO d1;
    INSERT INTO public.decisions(tenant_id,room_id,message_id,extraction_key,summary)
      VALUES(t1,r2,m2,'item-1','Synthetic other room decision') RETURNING id INTO d2;
    INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content,embedding)
      VALUES(t1,r1,m1,'fact','fact-1','Synthetic matching fact',v1) RETURNING id INTO memory1;
    INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content,embedding)
      VALUES(t1,r1,m1,'fact','fact-2','Synthetic distant fact',v2),
            (t1,r2,m2,'fact','fact-1','Synthetic other room fact',v1),
            (t2,r3,m3,'fact','fact-1','Synthetic other tenant fact',v1);
    INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content,embedding,active)
      VALUES(t1,r1,m1,'fact','inactive','Synthetic inactive fact',v1,false);
    INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content)
      VALUES(t1,r1,m1,'fact','no-vector','Synthetic pending embedding');
    INSERT INTO public.reminder_deliveries(tenant_id,room_id,commitment_id,scheduled_for,updated_at)
      VALUES(t1,r1,c1,'2099-01-01','2000-01-01') RETURNING id INTO delivery1;
    passed := passed || jsonb_build_array('Service-role inserts across all eight tables');
    BEGIN
        INSERT INTO public.messages(tenant_id,room_id,switch_message_id,body) VALUES(t1,r1,'audit-event-1','duplicate');
        RAISE EXCEPTION 'Expected 23505: Duplicate Switch event rejected';
    EXCEPTION WHEN SQLSTATE '23505' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Duplicate Switch event rejected');
    BEGIN
        INSERT INTO public.messages(tenant_id,room_id,switch_message_id,body) VALUES(t2,r1,'audit-wrong-tenant','test');
        RAISE EXCEPTION 'Expected 23503: Cross-tenant room mapping rejected';
    EXCEPTION WHEN SQLSTATE '23503' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Cross-tenant room mapping rejected');
    BEGIN
        INSERT INTO public.commitments(tenant_id,room_id,message_id,extraction_key,description) VALUES(t1,r2,m1,'cross-room','test');
        RAISE EXCEPTION 'Expected 23503: Cross-room commitment source rejected';
    EXCEPTION WHEN SQLSTATE '23503' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Cross-room commitment source rejected');
    BEGIN
        INSERT INTO public.decisions(tenant_id,room_id,message_id,extraction_key,summary) VALUES(t1,r2,m1,'cross-room','test');
        RAISE EXCEPTION 'Expected 23503: Cross-room decision source rejected';
    EXCEPTION WHEN SQLSTATE '23503' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Cross-room decision source rejected');
    BEGIN
        INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content) VALUES(t1,r2,m1,'fact','cross-room','test');
        RAISE EXCEPTION 'Expected 23503: Cross-room memory source rejected';
    EXCEPTION WHEN SQLSTATE '23503' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Cross-room memory source rejected');
    BEGIN
        INSERT INTO public.commitments(tenant_id,room_id,message_id,extraction_key,description) VALUES(t1,r1,m1,'item-1','test');
        RAISE EXCEPTION 'Expected 23505: Duplicate commitment extraction rejected';
    EXCEPTION WHEN SQLSTATE '23505' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Duplicate commitment extraction rejected');
    BEGIN
        INSERT INTO public.decisions(tenant_id,room_id,message_id,extraction_key,summary) VALUES(t1,r1,m1,'item-1','test');
        RAISE EXCEPTION 'Expected 23505: Duplicate decision extraction rejected';
    EXCEPTION WHEN SQLSTATE '23505' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Duplicate decision extraction rejected');
    BEGIN
        UPDATE public.commitments SET approval_status='invalid' WHERE id=c1;
        RAISE EXCEPTION 'Expected 23514: Invalid approval status rejected';
    EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Invalid approval status rejected');
    BEGIN
        UPDATE public.commitments SET approval_status='approved' WHERE id=c1;
        RAISE EXCEPTION 'Expected 23514: Approval without approver rejected';
    EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Approval without approver rejected');
    BEGIN
        UPDATE public.commitments SET task_status='completed' WHERE id=c1;
        RAISE EXCEPTION 'Expected 23514: Completed task requires completion timestamp';
    EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Completed task requires completion timestamp');
    BEGIN
        UPDATE public.commitments SET completed_at=now() WHERE id=c1;
        RAISE EXCEPTION 'Expected 23514: Completion timestamp requires completed task';
    EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Completion timestamp requires completed task');
    BEGIN
        UPDATE public.decisions SET supersedes_decision_id=d1 WHERE id=d1;
        RAISE EXCEPTION 'Expected 23514: Decision cannot supersede itself';
    EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Decision cannot supersede itself');
    BEGIN
        UPDATE public.decisions SET supersedes_decision_id=d2 WHERE id=d1;
        RAISE EXCEPTION 'Expected 23503: Decision cannot supersede another room decision';
    EXCEPTION WHEN SQLSTATE '23503' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Decision cannot supersede another room decision');
    BEGIN
        INSERT INTO public.reminder_deliveries(tenant_id,room_id,commitment_id,scheduled_for) VALUES(t1,r1,c1,'2099-01-01');
        RAISE EXCEPTION 'Expected 23505: Duplicate reminder schedule rejected';
    EXCEPTION WHEN SQLSTATE '23505' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Duplicate reminder schedule rejected');
    BEGIN
        INSERT INTO public.reminder_deliveries(tenant_id,room_id,commitment_id,scheduled_for) VALUES(t1,r2,c1,'2099-01-02');
        RAISE EXCEPTION 'Expected 23503: Reminder cannot reference another room commitment';
    EXCEPTION WHEN SQLSTATE '23503' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Reminder cannot reference another room commitment');
    BEGIN
        UPDATE public.reminder_deliveries SET status='sent' WHERE id=delivery1;
        RAISE EXCEPTION 'Expected 23514: Sent reminder requires timestamp';
    EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Sent reminder requires timestamp');
    BEGIN
        INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content,embedding) VALUES(t1,r1,m1,'fact','wrong-dims','test',array_fill(1::real,ARRAY[3])::extensions.vector);
        RAISE EXCEPTION 'Expected 22000: Wrong embedding dimensions rejected';
    EXCEPTION WHEN SQLSTATE '22000' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Wrong embedding dimensions rejected');
    BEGIN
        UPDATE public.memories SET embedding_model='other-model' WHERE id=memory1;
        RAISE EXCEPTION 'Expected 23514: Unsupported embedding model rejected';
    EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Unsupported embedding model rejected');
    BEGIN
        INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content) VALUES(t1,r1,m1,'fact','fact-1','test');
        RAISE EXCEPTION 'Expected 23505: Duplicate searchable memory rejected';
    EXCEPTION WHEN SQLSTATE '23505' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Duplicate searchable memory rejected');
    BEGIN
        DELETE FROM public.messages WHERE id=m1;
        RAISE EXCEPTION 'Expected 23503: Source evidence deletion restricted';
    EXCEPTION WHEN SQLSTATE '23503' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Source evidence deletion restricted');
    BEGIN
        UPDATE public.messages SET extraction_status='completed' WHERE id=m1;
        RAISE EXCEPTION 'Expected 23514: Extraction completion requires timestamp';
    EXCEPTION WHEN SQLSTATE '23514' THEN NULL;
    END;
    passed := passed || jsonb_build_array('Extraction completion requires timestamp');
    SELECT id,similarity INTO found,score FROM public.search_memories(t1,r1,v1,1);
    IF found IS DISTINCT FROM memory1 OR abs(score-1)>0.00001 THEN
      RAISE EXCEPTION 'Vector ranking or identical-vector similarity failed';
    END IF;
    passed := passed || jsonb_build_array('Vector ranking and cosine similarity');
    SELECT count(*) INTO rows_found FROM public.search_memories(t1,r1,v1,50);
    IF rows_found <> 2 THEN RAISE EXCEPTION 'Room scope, inactive filtering, or NULL embedding filtering failed'; END IF;
    passed := passed || jsonb_build_array('Search excludes other rooms, tenants, inactive and unembedded memories');
    IF EXISTS(SELECT 1 FROM public.search_memories(t1,r3,v1,5)) THEN RAISE EXCEPTION 'Cross-tenant search failed'; END IF;
    passed := passed || jsonb_build_array('Cross-tenant room search returns no rows');
    IF EXISTS(SELECT 1 FROM public.search_memories(t1,r1,NULL,5)) THEN RAISE EXCEPTION 'NULL query returned results'; END IF;
    passed := passed || jsonb_build_array('NULL query returns no rows');
    SELECT count(*) INTO rows_found FROM public.search_memories(t1,r1,v1,0);
    IF rows_found <> 1 THEN RAISE EXCEPTION 'Minimum limit clamp failed'; END IF;
    passed := passed || jsonb_build_array('Search minimum result limit');
    INSERT INTO public.memories(tenant_id,room_id,message_id,memory_type,extraction_key,content,embedding)
      SELECT t1,r1,m1,'fact','bulk-'||g,'Synthetic bulk fact',v2 FROM generate_series(1,55) g;
    SELECT count(*) INTO rows_found FROM public.search_memories(t1,r1,v1,1000);
    IF rows_found <> 50 THEN RAISE EXCEPTION 'Maximum limit clamp failed'; END IF;
    passed := passed || jsonb_build_array('Search maximum of fifty results');
    UPDATE public.memories SET content=content WHERE id=memory1;
    IF (SELECT embedding IS NULL FROM public.memories WHERE id=memory1) THEN RAISE EXCEPTION 'Unchanged content cleared embedding'; END IF;
    UPDATE public.memories SET content='Synthetic revised fact' WHERE id=memory1;
    IF (SELECT embedding IS NOT NULL FROM public.memories WHERE id=memory1) THEN RAISE EXCEPTION 'Edited content retained embedding'; END IF;
    UPDATE public.memories SET embedding=v1 WHERE id=memory1;
    passed := passed || jsonb_build_array('Embedding preserved for unchanged content, cleared for edits, and replaceable');
    UPDATE public.commitments SET description='Synthetic revised task' WHERE id=c1;
    UPDATE public.decisions SET summary='Synthetic revised decision' WHERE id=d1;
    UPDATE public.reminder_deliveries SET attempts=1 WHERE id=delivery1;
    IF (SELECT updated_at FROM public.commitments WHERE id=c1) IS DISTINCT FROM started_at
       OR (SELECT updated_at FROM public.decisions WHERE id=d1) IS DISTINCT FROM started_at
       OR (SELECT updated_at FROM public.reminder_deliveries WHERE id=delivery1) IS DISTINCT FROM started_at THEN
       RAISE EXCEPTION 'Updated timestamp trigger failed';
    END IF;
    passed := passed || jsonb_build_array('All three updated_at triggers');
    UPDATE public.commitments SET approval_status='approved',approval_by_switch_user_id=prefix,approval_at=now(),
      task_status='completed',completed_at=now() WHERE id=c1;
    UPDATE public.decisions SET approval_status='approved',approval_by_switch_user_id=prefix,approval_at=now() WHERE id=d1;
    UPDATE public.reminder_deliveries SET status='sent',sent_at=now() WHERE id=delivery1;
    UPDATE public.messages SET extraction_status='completed',extraction_completed_at=now() WHERE id=m1;
    passed := passed || jsonb_build_array('Valid approval, task completion, reminder delivery and extraction transitions');
    DELETE FROM public.memories WHERE id=memory1;
    IF EXISTS(SELECT 1 FROM public.memories WHERE id=memory1) THEN RAISE EXCEPTION 'Service-role delete failed'; END IF;
    passed := passed || jsonb_build_array('Service-role read, update and delete');

    RESET ROLE;
    FOREACH test_role IN ARRAY ARRAY['anon','authenticated'] LOOP
      EXECUTE format('SET LOCAL ROLE %I',test_role);
      FOREACH test_table IN ARRAY ARRAY['tenants','rooms','room_members','messages','commitments','decisions','memories','reminder_deliveries'] LOOP
        FOREACH operation IN ARRAY ARRAY['SELECT','INSERT','UPDATE','DELETE'] LOOP
          BEGIN
            CASE operation
              WHEN 'SELECT' THEN EXECUTE format('SELECT 1 FROM public.%I LIMIT 0',test_table);
              WHEN 'INSERT' THEN EXECUTE format('INSERT INTO public.%I DEFAULT VALUES',test_table);
              WHEN 'UPDATE' THEN EXECUTE format('UPDATE public.%I SET created_at=created_at WHERE false',test_table);
              WHEN 'DELETE' THEN EXECUTE format('DELETE FROM public.%I WHERE false',test_table);
            END CASE;
            RAISE EXCEPTION 'Client role unexpectedly allowed: % % %',test_role,operation,test_table;
          EXCEPTION WHEN insufficient_privilege THEN denied := denied+1;
          END;
        END LOOP;
      END LOOP;
      BEGIN
        PERFORM 1 FROM public.search_memories(t1,r1,v1,1);
        RAISE EXCEPTION 'Client role unexpectedly allowed vector RPC: %',test_role;
      EXCEPTION WHEN insufficient_privilege THEN denied := denied+1;
      END;
      RESET ROLE;
    END LOOP;
    IF denied <> 66 THEN RAISE EXCEPTION 'Expected sixty-six access denials, got %',denied; END IF;
    passed := passed || jsonb_build_array('Sixty-six direct client table and vector-RPC access attempts denied');

    -- A deliberate exception rolls back every synthetic insert/update/delete.
    RAISE EXCEPTION USING ERRCODE='ZX001', MESSAGE='Audit fixtures rollback';
  EXCEPTION WHEN SQLSTATE 'ZX001' THEN NULL;
  END;
  PERFORM set_config('team_memory.audit_results',passed::text,true);
END;
$audit$;

SELECT jsonb_array_length(current_setting('team_memory.audit_results')::jsonb) AS passed_groups,
       current_setting('team_memory.audit_results')::jsonb AS passed_checks;

