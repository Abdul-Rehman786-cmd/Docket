# Supabase database audit — November 4, 2026

Audited the connected `team-memory-agent` project using live database queries,
Supabase security/performance advisors, and the repeatable behavior script in
`tests/database_audit.sql`. No remote schema changes were applied. Synthetic test
records were rolled back; final counts were zero in all eight application tables.

## Result

The database foundation passed 33 behavior-test groups, including 66 actual
access-denial attempts under `anon` and `authenticated`. This verifies database
behavior, not FastAPI connectivity or the end-to-end Switch pipeline.

Confirmed:

- pgvector 0.8.2 installed in `extensions`; embeddings use `vector(384)`.
- All eight application tables have RLS enabled, no client policies, and no
  SELECT/INSERT/UPDATE/DELETE privileges for anonymous or authenticated clients.
- `service_role` can insert into every application table and read/update/delete
  test memories. Only the backend role can execute `search_memories`.
- Duplicate message/extraction/reminder keys are rejected.
- Composite foreign keys reject mismatched tenant/room/source references.
- Approval, completion, extraction, and reminder timestamp checks work.
- Vector search ranks identical vectors first and respects tenant/room filters.
- Inactive and unembedded memories are excluded; NULL query vectors return no rows.
- Search limits clamp to between one and fifty results.
- Content edits clear old embeddings; unchanged content preserves them.
- All three `updated_at` triggers work; referenced source deletion is restricted.

## Findings

### Client-executable privileged helper

The existing `public.rls_auto_enable()` event-trigger function is
`SECURITY DEFINER`. Supabase's advisor flags EXECUTE access for both `anon` and
`authenticated`. Direct SQL invocation under both roles completed without error.
Its inspected body enables RLS on tables reported by the DDL event context; this
audit did not demonstrate private-data exposure or execute any DDL through it.

Recommended hardening: review ownership/event-trigger usage and revoke EXECUTE
from PUBLIC, anon, and authenticated in a separate approved migration. Do not
change this helper to SECURITY INVOKER blindly; its DDL automation may require
its existing privileges. This helper is outside the repository's foundation SQL.

Advisor references:

- https://supabase.com/docs/guides/database/database-linter?lint=0028_anon_security_definer_function_executable
- https://supabase.com/docs/guides/database/database-linter?lint=0029_authenticated_security_definer_function_executable

### Two foreign keys lack covering indexes

- `memories (tenant_id, room_id, message_id)`
- `reminder_deliveries (tenant_id, room_id, commitment_id)`

Existing indexes cover only prefixes or use a different leading column. Add
covering indexes in a separate approved migration before the dataset grows.
These are performance findings, not failing integrity constraints.

Reference: https://supabase.com/docs/guides/database/database-linter?lint=0001_unindexed_foreign_keys

### Migration history is not registered

`supabase_migrations.schema_migrations` does not exist. The live foundation and
vector additions were installed manually. Before automated deployment, reconcile
the reviewed local migration with the live schema; do not apply the destructive
foundation reset to mark it applied.

### Onboarding is still required

The final inventory contained no tenants, rooms, or room members. Register the
actual team, trusted Switch room mapping, and approval permissions before real
message ingestion.

### Expected informational advisor results

Eight RLS-without-policy notices match the intentional backend-only model.
Thirteen unused-index notices are expected for a newly initialized database;
they are not a reason to remove the queue/source indexes.

## Limits and backend requirements

- SQL role tests do not verify real API credentials, HTTP access, FastAPI startup,
  or Switch event delivery.
- Synthetic vectors verify ranking/storage, not embedding-model quality.
- No production-sized load or concurrent-worker tests were performed.
- Service-role access bypasses RLS. FastAPI must authorize each room and enforce
  enabled-room and approver checks. Passing FK/search-filter tests does not prove
  user-level authorization.
- FastAPI must synchronize searchable summaries with approval/task/decision
  status. `active` is not automatically coupled to those structured records.
- Queue claims, leases, retries, and reminder delivery are schema support only;
  the audit does not validate a worker implementation or exactly-once messaging.
- Regeneration must avoid applying an old embedding to text edited concurrently;
  backend updates should compare the content/version they embedded.

## Repeat the behavior audit

Run the complete `tests/database_audit.sql` as a database administrator able to
SET ROLE. It performs no schema changes. All fixture writes are contained in a
subtransaction and intentionally rolled back. Unexpected errors abort the test;
the final result must report `passed_groups = 33`.
