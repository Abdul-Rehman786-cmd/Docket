# FastAPI cloud storage — checked 2026-11-05

Status: the configured cloud connection is verified, but the required atomic
storage function is still absent. No live FastAPI write is claimed. A smaller,
storage-only deployment is prepared and requires approval.

## Actual findings

- The backend uses the hosted project, not FastAPI's localhost address. Read-only
  checks through the backend's own credential succeed; this is independent of
  the Supabase management connector.
- The configured credential is a new opaque Supabase **secret** key. It was
  stored in ignored `backend/.env` as `SUPABASE_SECRET_KEY`, a supported alias.
  That existing credential was copied to the requested canonical variable
  `SUPABASE_SERVICE_ROLE_KEY`; no cloud credential was rotated or printed.
- `ADMIN_API_TOKEN` was missing. A separate random local admin token was written
  only to ignored `backend/.env`. The existing ingest token was preserved. All
  three credentials are now present and distinct. The admin token had blocked
  authenticated status checks; it was not the cause of missing ingestion writes.
- Real FastAPI status route, exercised in-process against the actual cloud:
  no header -> 401; ingest token used as admin -> 401; correct admin -> 200,
  `connected`. No private messages were returned by this check.
- The private Codex evaluation settings are configured and the AI room remains
  separate from input rooms. No Switch/Discord test message was sent.
- The configured tenant exists. One of two configured input rooms has an enabled
  matching database mapping; the other is unprovisioned. It must not be silently
  created or reassigned by model output. Only trusted onboarding should provision
  that room. This is a separate scope blocker for that room.
- The live `store_message_extraction(uuid,text,jsonb,jsonb)` function does not
  exist. PostgREST's schema description also confirms its absence. This is the
  immediate blocker for the application's atomic write path, even though reads
  succeed. The existing repository correctly reports write failure rather than
  pretending storage completed.

Read-only live counts: 3 messages, 3 commitments, 0 decisions, 6 memories.
All four data tables still have RLS enabled. Migration history is still empty.
Existing data was not altered by this task's checks.
Live schema checks also confirmed the source/item uniqueness constraints, all
three composite child-source foreign keys, pending/not_started defaults and
the absence of anonymous/authenticated INSERT privileges.

## What approval means in this application

A validated `store_only` or `store_and_query` extraction makes useful information
**eligible for persistence**. There is no implemented policy requiring human
approval before cloud persistence. Human approval is a separate relational state:
new commitments and decisions remain `approval_status='pending'`; commitments
also remain `task_status='not_started'`. Extraction cannot supply approval/status
fields or choose tenant/room/table destinations. Chatter, invalid extraction and
unauthorized scope are not stored as useful knowledge. No human approval is
fabricated, and this task adds no new approval workflow.

## Changes made

- `backend/app/core/supabase_auth.py`: one credential/header policy for all database
  operations. New secret keys use `apikey` without pretending to be bearer JWTs;
  legacy service-role JWTs preserve their bearer header. Public and invalid JWT
  credentials are rejected locally. Supabase still authenticates every actual key.
- `core/config.py`: removed `SUPABASE_ANON_KEY` as a privileged credential alias.
- `repositories/record_repository.py`: reuses the header policy and adds a
  read-only RPC-availability check, separate from a successful connection check.
- `core/message_logging.py`, `services/ai_service.py`, `services/message_service.py`:
  source identifiers use credential-aware redaction separately from the broad
  chat-token heuristic. Legitimate UUID/opaque sender and platform IDs survive;
  known secrets, explicit credential formats and URLs remain protected. Unsafe
  source message IDs are rejected rather than stored under a fabricated ID.
  A redaction-configuration failure is retryable before acknowledgement.
- `schema_upgrade.py --storage-only`: derives the exact missing RPC and service-role
  grants from the consolidated baseline, and requests PostgREST schema reload.
  It never connects or applies changes.
- `verify_cloud_storage.py`: an opt-in actual FastAPI HTTP -> durable journal ->
  validated parser/service/repository -> real Supabase fixture. Only its model
  response is simulated, labelled explicitly; no Discord/Codex call is made.
  Embeddings are disabled only inside this isolated fixture process, not made a
  storage prerequisite or changed in the real application configuration.
- `tests/test_cloud_storage.py`: offline key-type, public-key rejection, read-only
  schema preflight, fixture no-write guard and storage-only proposal tests.
- Ignored `backend/.env`: canonical database variable and missing admin token,
  with existing values preserved. Documentation records these facts without keys.

## Smallest hosted change awaiting approval

From `backend/`, `python -m app.schema_upgrade --storage-only` prints the exact
proposal for review. It installs only:

```text
CREATE OR REPLACE FUNCTION public.store_message_extraction(uuid,text,jsonb,jsonb)
REVOKE execution FROM PUBLIC, anon, authenticated
GRANT execution TO service_role
NOTIFY PostgREST to reload its schema
```

The function uses the already-deployed tables and columns, a trusted enabled
tenant/room mapping, a transaction advisory lock and existing uniqueness/FKs.
Source, commitments, decisions and memories commit atomically. Only then is the
source marked completed. Completed source replays return existing counts without
overwriting human approvals. No table reset, ALTER TABLE, embedding backfill,
RLS relaxation or migration-history rewrite is included. Embedding support stays
in code and the larger proposal remains separate; useful text does not depend
on embedding generation.

No remote schema change has been approved or applied in this task. Never run the
destructive baseline or an indiscriminate `db push`. After approval, record only
the actual storage-function installation, not a fictitious applied baseline.
If installation fails, its transaction rolls back. If cloud writes subsequently
fail, existing durable jobs retain redacted input/extraction and bounded retries;
after repairing the cause, reset only selected exhausted jobs with the existing
`app.ingestion_jobs` command while the API is stopped. Do not delete their source
or clear completed jobs.

## Verification state

Connection/authentication and room checks above are **live**. Codex/database
behavior in automated storage regressions is mocked; SQLite journals are real.
Executed: 194 backend tests passed, one opt-in real-model test skipped, and five
browser lifecycle tests passed. There was one existing library deprecation
warning. Dependency checks and syntax/diff checks passed. The new key-type,
approval/default, schema-preflight, no-write fixture guard and existing ingestion,
failure, replay/restart, viewer/reply regressions are included. No real cloud
write fixture has run in this task; no fixture rows were created.
After the full run, 50 focused storage/redaction tests also passed, including
the added retryable-redaction-configuration failure test.

The new fixture refuses to create any data while the RPC is absent. After the
approved installation, run:

```powershell
python -m app.verify_cloud_storage --run-approved-fixture
```

It creates a uniquely generated disposable tenant/room, posts a synthetic Switch
payload through the real authenticated `/messages` handler, uses one explicitly
simulated structured extraction, waits for real processing and repeats the HTTP
submission. It independently reads the actual cloud rows and verifies:

- 1 source message, 1 commitment, 1 decision, 3 memories.
- No duplicate rows on replay and one durable processing attempt.
- Exact fixture metadata/scope/source links and completed source state.
- Pending human approvals, not_started task, unresolved owner/deadline.
- Cleanup restricted to the fixture's exact tenant/room and generated IDs, with
  zero fixture records remaining. It never touches the real ingestion journal.

Those are **expected** fixture counts, not verified live-write results yet. The
missing RPC prevents the test from starting; no fixture records were created.
The existing SQL rollback fixture `tests/useful_storage.sql` should also run
after approval to demonstrate late child-write rollback on the actual database.

For an actual Discord-origin test, a human can post this harmless marker in the
existing **mapped, authorized** test room after deployment:
`TM-cloud-live-20261105: the fixture planning meeting is Friday.`
Then verify its exact source and linked useful memory in cloud Supabase, pending
approval where applicable, and silent store-only handling. Do not use the
unprovisioned room until trusted onboarding resolves its mapping. This message
has not been sent, and a complete Discord-origin storage flow remains unverified.

Official key behavior checked through Supabase documentation:
[backend secret-key migration](https://supabase.com/docs/guides/getting-started/migrating-to-new-api-keys).
