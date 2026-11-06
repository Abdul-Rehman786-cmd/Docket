-- Add format metadata without changing document text/chunk storage or the atomic RPC.
BEGIN;
ALTER TABLE public.documents DROP CONSTRAINT documents_source_type_check;
ALTER TABLE public.documents ADD CONSTRAINT documents_source_type_check
    CHECK (source_type IN ('markdown','text','pdf','docx','pptx'));
COMMIT;
