-- Disposable fixture only: new formats use the same atomic import/idempotency contract.
BEGIN;
DO $$
DECLARE
    tenant uuid:=gen_random_uuid(); room uuid:=gen_random_uuid(); fmt text;
    source text:='The paper otter carries a green notebook.';
    payload jsonb; result jsonb; repeated jsonb; import_id uuid;
BEGIN
    INSERT INTO public.tenants(id,name) VALUES(tenant,'binary format fixture');
    INSERT INTO public.rooms(id,tenant_id,switch_room_id) VALUES(room,tenant,'binary-fixture-'||room);
    FOREACH fmt IN ARRAY ARRAY['pdf','docx','pptx'] LOOP
        import_id:=gen_random_uuid();
        payload:=jsonb_build_object('import_id',import_id,'source_key',fmt,'filename','otter.'||fmt,'source_type',fmt,
            'title','Otter','expected_version',0,'room_id',room,'actor','fixture','source_text',source,
            'content_hash',encode(sha256(convert_to(source,'UTF8')),'hex'),'recipe','extracted-text:v1',
            'model_recipe','fixture-model','request_hash','fixture-'||fmt,'chunks',jsonb_build_array(jsonb_build_object(
            'position',0,'text',source,'content_hash',encode(sha256(convert_to(source,'UTF8')),'hex'),
            'start_char',0,'end_char',length(source),'start_line',1,'end_line',1,'section',NULL)));
        result:=public.document_operation(tenant,ARRAY[room],'import',payload);
        repeated:=public.document_operation(tenant,ARRAY[room],'import',payload);
        IF repeated->>'document_id'<>result->>'document_id' OR repeated->>'duplicate'<>'true' THEN
            RAISE EXCEPTION 'binary format idempotency failed';
        END IF;
        IF NOT EXISTS(SELECT 1 FROM public.documents WHERE id=(result->>'document_id')::uuid AND source_type=fmt) THEN
            RAISE EXCEPTION 'binary format metadata lost';
        END IF;
    END LOOP;
END $$;
ROLLBACK;
SELECT 'passed' AS document_formats;
