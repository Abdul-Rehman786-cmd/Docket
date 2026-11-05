# Hosted storage/embedding upgrade proposal — approval required

The sole local core migration is `migrations/20261104000100_initial_schema.sql`.
It is a destructive **fresh development baseline**, never a hosted upgrade.
The user authorized local consolidation only. No remote DDL or migration-history
change has been performed for this task.

## Inspected live difference

The connected configured project is `team-memory-agent`. Its migration list is
empty; the foundation was installed through SQL Editor, not recorded migration
history. `store_message_extraction` is absent. The existing four-argument
`search_memories(uuid,uuid,vector,integer)` function searches active non-null
vectors, and the old content-update trigger only clears the vector.

Live read-only observation: 3 messages, 6 memories, 0 vectors. Memory rows lack
content hashes, recipe identity and embedding job state. The existing eight
application tables, source metadata, composite scope FKs, extraction identities,
384-dimensional vector column and backend-only grants remain suitable.

The untracked additive storage file was absent from the only connected project's
migration history and function definitions. Its exact function/grant block was
verified copied into the core baseline before removing the redundant file.
No previously applied migration was deleted from deployed history. Unknown
environments outside this workspace/connection cannot be attested; check those
before reusing this baseline elsewhere.

## Exact non-destructive change set

`python -m app.schema_upgrade` from `backend/` prints the exact SQL proposal,
derived from the consolidated baseline. It does not connect to any database or
read credentials. Review [the generator](../backend/app/schema_upgrade.py) and
the referenced definitions in the baseline. It emits one transaction containing:

1. `ALTER TABLE memories ADD COLUMN IF NOT EXISTS` for `content_hash`, pinned
   `embedding_recipe`, `embedding_content_hash`, status, attempts, retry time,
   lease, claim token, completion time and sanitized error category.
2. Backfill exact UTF-8 SHA-256 hashes for existing memory text. Leave all
   messages, extracted records, source IDs, approvals and memory text unchanged.
   Unversioned existing vectors, if any appear before approval, are invalidated
   and requeued; their provenance cannot be asserted. At inspection there were
   no vectors to invalidate. Existing six memory rows become pending jobs.
3. Set the hash column NOT NULL and add the named vector/state/hash consistency
   constraints. Add partial pending/retry and expired-lease indexes.
4. Replace `clear_stale_memory_embedding()` and its trigger with insert/update
   hashing and content-edit invalidation/reset behavior.
5. Create/replace `store_message_extraction`, `claim_memory_embeddings`,
   `finish_memory_embedding`, and the existing search signature. Completion
   checks claim token and exact current hash; search checks completed/current
   recipe/hash and active scope before limiting results.
6. Revoke all four RPCs from PUBLIC/anon/authenticated and grant only service_role.
   Functions remain SECURITY INVOKER with an empty search path. Table privileges
   and RLS are not widened.

There is no DROP TABLE, TRUNCATE, DELETE, CASCADE, project recreation, foundation
replay, or removal of useful data in this proposal. A single memory trigger is
dropped/recreated transactionally. The recipe pins revision
`93b36ff09519291b77d6000d2e86bd8565378086`, FP32 ONNX, masked mean pooling and L2.

## Preconditions and migration history

- Fresh disposable PostgreSQL/pgvector creation/reset and the SQL fixture suites
  must pass before hosted deployment. The PGlite test-engine installation was
  declined in this session, so this is currently an explicit gate, not a passed
  check. The test runner and exact pinned dependencies are ready under `tests/`.
- Confirm this live difference again immediately before applying. Preserve a
  scoped backup/export according to the existing host's procedure; do not create
  paid infrastructure. Ensure `.env` credentials stay backend-only.
- Install the local model and pinned dependencies first. The current workspace
  has verified model assets and a passing real CPU-model test.
- After approval, apply **only the printed non-destructive delta**. Never run
  the consolidated baseline or an indiscriminate `supabase db push` here.
- Do not mark `20261104000100` or the removed additive version applied: that
  would claim a reset/schema verification that never happened. If the approved
  deployment tool records the actual non-destructive upgrade under a new hosted
  operation/version, record that genuine execution only, preserving existing
  history. No extra local migration file is required or proposed. CLI baseline
  reconciliation remains a separate reviewed action.

## Verification after approval

1. Stop/drain the API during the transaction; keep the listener's checkpoint.
   Unacknowledged Switch events can replay. Do not start another competing listener.
2. Apply the approved delta, inspect columns/functions/grants, and run
   `tests/useful_storage.sql` and `tests/embedding_jobs.sql`. Their uniquely
   identified synthetic fixtures roll back. They verify atomicity, replay,
   source metadata, defaults, scopes, stale claims, retry bounds and vector search.
   These SQL vectors are synthetic test axes, not real model evidence.
3. From `backend/`, run `python -m app.verify_embedding_fixture --run-approved-fixture`.
   This opt-in harness creates a unique disposable tenant/room/source, calls the
   actual atomic repository, generates/stores a real local vector, retrieves it
   with a paraphrase and validates source references. It posts no chat. Cleanup
   is restricted to that fixture's generated scope and is verified. Any failure
   is reported without credentials or raw remote error bodies.
4. Restart the API and the one existing listener. Review sanitized ingestion and
   embedding counts/states. Exhausted ingestion jobs from the earlier missing
   function can be reset individually with the existing `app.ingestion_jobs`
   command while the API is stopped; never reset completed jobs.
5. An authorized human posts in the existing test room:
   `TM-live-vector-20261105: the project knowledge API uses FastAPI.`
   Wait for its exact source, useful memory and completed current-hash vector.
   Then ask `Which framework powers the project knowledge service?` in that
   room. Confirm semantic retrieval, a traceable `[source:<UUID>]` citation,
   correct origin reply routing and no unsolicited store-only reply.
   This external test has not been sent and requires user participation or
   explicit posting authorization.

## Failure and recovery

An error during deployment rolls back its transaction. Do not use a reset as
recovery. If runtime inference fails, useful records remain committed, retry
state remains in Supabase, and keyword fallback is labelled explicitly. Disable
`LOCAL_EMBEDDINGS_ENABLED` temporarily if needed; storage is independent.

Expired five-minute leases recover after restart. Three failed attempts stop
automatically. An operator can requeue selected failed, unembedded records after
fixing dependencies/model configuration by resetting their attempts/status/retry
time under an exact tenant, room and memory-ID predicate. Content edits also
invalidate/reset jobs, but never edit evidence merely to trigger a retry.
Oversized text is deliberately left `failed/input_too_long`; deliberate chunking
requires a later design. No memory or source should be deleted to repair a vector.

No HNSW index or hosted compute change is proposed for six memories. Exact cosine
search is appropriate. Any future HNSW index must use `vector_cosine_ops` for
`<=>` and be justified by representative scope/recall/latency tests, not a claim
of 100k-1M capacity.
