-- READ ONLY catalog review for an authorized database operator. No application data or secrets.
-- No schema changes, migrations, fixtures, messages, or history reconciliation.
SELECT proname,pg_get_function_identity_arguments(p.oid) AS arguments,
       NOT prosecdef AS security_invoker,proconfig,
       has_function_privilege('service_role',p.oid,'EXECUTE') AS backend_execute,
       has_function_privilege('anon',p.oid,'EXECUTE') AS anon_execute,
       has_function_privilege('authenticated',p.oid,'EXECUTE') AS authenticated_execute,
       encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex') AS definition_sha256
FROM pg_proc p WHERE pronamespace='public'::regnamespace AND proname IN
('store_message_extraction','claim_memory_embeddings','finish_memory_embedding','clear_stale_memory_embedding',
'read_commitment_lifecycle','apply_commitment_action','apply_commitment_chat_action','prepare_commitment_chat',
'read_reminder_work','persist_reminder_intention','claim_reminder_deliveries','read_reminder_dispatch',
'begin_reminder_dispatch','finish_reminder_delivery','defer_reminder_delivery') ORDER BY proname;

SELECT table_name,column_name,data_type,is_nullable FROM information_schema.columns
WHERE table_schema='public' AND table_name IN('commitments','memories','commitment_action_events','commitment_action_receipts',
'commitment_chat_policies','commitment_chat_identities','commitment_chat_sources','reminder_deliveries') ORDER BY table_name,ordinal_position;

SELECT c.relname AS table_name,c.relrowsecurity AS rls_enabled,
       has_table_privilege('anon',c.oid,'SELECT,INSERT,UPDATE,DELETE') AS anon_any_access,
       has_table_privilege('authenticated',c.oid,'SELECT,INSERT,UPDATE,DELETE') AS authenticated_any_access
FROM pg_class c WHERE c.relnamespace='public'::regnamespace AND c.relname IN
('commitments','room_members','reminder_deliveries','commitment_chat_policies','commitment_chat_identities','commitment_chat_sources',
'commitment_action_events','commitment_action_receipts');

SELECT conrelid::regclass AS table_name,conname,pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE conrelid IN(to_regclass('public.reminder_deliveries'),to_regclass('public.commitments'),
to_regclass('public.commitment_chat_policies')) ORDER BY conrelid,conname;
SELECT tgrelid::regclass AS table_name,tgname,tgenabled,pg_get_triggerdef(oid) AS definition
FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN(to_regclass('public.memories'),to_regclass('public.commitments'),
to_regclass('public.commitment_chat_policies')) ORDER BY tgrelid,tgname;
