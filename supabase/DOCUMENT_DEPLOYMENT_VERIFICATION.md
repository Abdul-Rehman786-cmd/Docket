# Hosted document deployment and verification

Verified 2026-11-05 against the backend-configured Supabase project
`yrgeraztwkzuaekynwnp`. The document module is deployed, enabled and verified.
Routine authorized imports and updates process automatically; no per-document
approval step was added.

## Approved schema deployment

Read-only preflight found zero document tables and no `document_operation` RPC.
The `extensions.vector` dependency and composite room-scope constraint were
present. Existing data was 13 messages, 12 commitments, 13 memories and 11 memory
vectors. Supabase migration history was empty; the destructive baseline was not
replayed or falsely marked applied.

The user authorized proceeding and requested that local migration SQL remain
aligned with hosted changes. The exact reviewed additive SQL was applied through
Supabase's migration tool, which recorded:

- Version: `20261105163303`; name: `versioned_documents`.
- Local source: [20261105163303_versioned_documents.sql](migrations/history/20261105163303_versioned_documents.sql).
- Approved proposal originally had filename `20261105153432_versioned_documents.sql`.
  It was renamed locally to match the genuine remote execution version. No SQL
  content or applied history was rewritten.
- SQL file SHA-256, before and after filename alignment:
  `ada57ff8b493c0c5cd012fae07284fcfaff0475bc54536e4f9a450f904460220`.
- Normalized function-body SHA-256 matches locally and remotely:
  `bc4f408149698cb8ac62e51d45963bcfc84e75a787d79d9c65bfb56a5a278a57`.

Added only `documents`, `document_versions`, `document_chunks`, `document_imports`,
their composite constraints and queue/scope indexes, and `document_operation`.
Processing state is on document chunks; no shared message jobs are repurposed.
Source updates append versions through the RPC rather than source-edit triggers.
Every table has RLS enabled; `anon` and `authenticated` lack table reads and RPC
execution. The RPC is `SECURITY INVOKER`, with fixed search path. Existing
messages, commitments, memory embeddings and room access controls were preserved.

The post-deployment advisor reports 12 informational RLS-without-policy findings,
consistent with backend-only tables. The two pre-existing warnings about public
execution of `public.rls_auto_enable()` remain outside this deployment:
[anonymous execution guidance](https://supabase.com/docs/guides/database/database-linter?lint=0028_anon_security_definer_function_executable),
[authenticated execution guidance](https://supabase.com/docs/guides/database/database-linter?lint=0029_authenticated_security_definer_function_executable).
No new document SECURITY DEFINER function was introduced.

## Real hosted fixture evidence

The opt-in `app.verify_document_fixture` exercised the actual restricted FastAPI
upload/import, search, source-inspection and archive routes through ASGI transport.
Only fixture backend configuration/repository settings were substituted. Admin
authentication and the real read-only room authorization dependency ran; document
handlers, services, tokenizer, ONNX inference, repository, cloud writes, vector
search and publication were real. This was not a direct-insert ingestion proof
or keyword fallback. Generated tenant/room setup was solely fixture infrastructure;
existing mappings were not changed.

Two uniquely generated fixture runs passed and cleaned up. The final run:

- Marker: `TM-document-fixture-03048bbf-8c9f-4027-b0e2-49c09a4f1172`.
- Tenant: `4b529f43-4920-4bfa-8687-74c1f310d93d`.
- Room: `6a058eb4-9585-4eaa-b1c1-59eebec829c7`.
- Maximum fixture records: **1 document, 4 logical versions, 4 chunks**.
- **3 completed actual 384-dimensional normalized vectors**; the remaining
  chunk belongs to the deliberately failed/superseded version.
- Paraphrase cosine similarity: **0.838061009703252**.

Verified behavior:

1. An authenticated raw Markdown upload containing a unique harmless fixture
   fact returned `202`; original source, SHA-256 hash and version were persisted.
   Chunk text matched its exact source character slice; real-tokenizer inputs
   fit 512 tokens and line locations matched source.
2. The background worker automatically claimed jobs, generated actual local
   vectors and published the completed version. Successful phases did not call
   `process_once` manually or inject fake vectors.
3. A paraphrased question returned the published passage. Its
   `document:document_id:version_id:chunk_id` citation resolved through the actual
   inspection API to the exact supporting text and correct identifiers.
4. Unchanged upload created no duplicate version or chunk. A changed upload
   created version 2; version 1 remained searchable until automatic replacement.
   A newly constructed worker recovered that persisted pending version.
5. A controlled embedding failure in version 3 preserved version 2 publication.
   Version 4 superseded unfinished work; stale completion was rejected and
   could not overwrite the latest version. The real worker automatically
   embedded and published version 4.
6. Invalid admin credentials and unauthorized room requests were rejected.
   Cross-tenant database scope access was rejected before reading/ranking.
7. Archive excluded the document from search while all four source versions
   remained available for authorized evidence inspection.
8. Cleanup cleared only the generated fixture publication pointer, then removed
   exact-scope imports/chunks/versions/document and generated room/tenant. Both
   runs independently confirmed cleanup.

After cleanup, independent cloud counts were **0 documents, 0 versions, 0 chunks,
0 import records**. Existing counts remained 13 messages, 12 commitments,
13 memories and 11 memory vectors.

## Enabled backend and safe restart

Ignored backend configuration now has `DOCUMENT_INGESTION_ENABLED=true` and
`LOCAL_EMBEDDINGS_ENABLED=true`. No credential values were changed or printed.
Pinned local model assets passed checksum verification.

Before restart, API processing counts, running ingestion jobs and active memory
jobs were all zero. The idle API was restarted; no Switch listener was started
or replaced. Journal and listener-checkpoint hashes remained unchanged. Existing
tenant and room records had identical hashes before and after restart.

An existing startup auto-provision routine would upsert tenant/room mappings on
restart. Its invocation was removed to satisfy the no-mapping-change requirement.
Document authorization now uses its own read-only mapping resolver, independent
of a legacy message resolver that can create missing mappings. New tests prove
missing, mismatched and disabled document scopes perform GET-only checks and
never provision records. No commitment repository or ingestion journal was edited.

After restart:

- `/health`: `200`.
- Unauthenticated `/api/v1/documents`: `401`.
- Authorized configured-room document list: `200`, empty after fixture cleanup,
  with `Cache-Control: no-store`.
- Actual API log contains `Document worker state=started` and application startup
  completion, with zero document retry warnings or tracebacks observed.
- Worker restart recovery was separately verified against durable pending cloud
  fixture versions. No real team document was modified to test recovery.

## Tests and remaining limitations

- **227 backend tests passed**, including actual local-model tests, available
  message/commitment/retrieval regressions, API/viewer tests, automatic worker
  recovery and read-only document authorization. One existing test-library
  deprecation warning remains.
- `node document-schema.mjs`: document migration/contracts passed, plus existing
  useful-storage and memory-embedding SQL suites in disposable PostgreSQL/pgvector.
- Two real hosted fixture runs passed, as detailed above. These are distinct
  from mocked domain tests and disposable SQL tests.
- Five standalone viewer JavaScript tests still fail in unchanged DOM mocks
  (`classList`/`appendChild`). The broader baseline SQL audit still fails its
  unchanged message/commitment `ON DELETE RESTRICT` cleanup fixture, before any
  document migration is loaded. These checks were rerun and are not called passes.

No Discord messages, Switch agent/membership edits, existing mapping changes,
paid-resource changes or real team-task updates were performed. The existing
upload API remains the collection mechanism. Individual-user authentication,
watched folders/providers, PDF/OCR, automatic document context in Discord answers,
permanent deletion, retention automation and large-corpus benchmarks remain
outside this module. Existing core migration-history reconciliation is still
separate; only the document migration execution was recorded here.
