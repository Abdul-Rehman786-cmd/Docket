# Database setup and deployment

**Fresh/disposable databases only:** the baseline resets Team Memory tables.
Never replay it against existing hosted team data. Runtime settings are separate
from schema deployment, and remote changes still require explicit approval.

This guide combines setup, migration-history rules and the earlier hosted upgrade
proposal. PostgreSQL has one migration file: the consolidated initial schema.
Seven redundant module archives were removed after checking their objects against
a disposable database. Their source remains in Git history. The separate SQLite
profile upgrade and SQL test/read-only audit scripts are not PostgreSQL setup steps.
The proposal remains historical evidence: do not treat it as approval
or assume its recorded preflight matches today's project. Use
[architecture.md](../architecture.md) for the main architecture and
[verification history](VERIFICATION.md) for recorded checks.

## Contents

- [Fresh database setup](#database-setup)
- [Historical migrations and existing installations](#migration-history)
- [Historical hosted upgrade proposal](#deployment-proposal)

<a id="database-setup"></a>
## Fresh database setup

Consolidated from `supabase/README.md`.

<a id="database-setup-single-script-supabase-setup"></a>
### Single-script Supabase setup

For a new, empty database, run the complete contents of:

[`migrations/20261104000100_initial_schema.sql`](../supabase/migrations/20261104000100_initial_schema.sql)

Run it once. It contains all currently implemented database modules in one
transaction: core scope/storage, memory embeddings, commitment lifecycle/audit,
chat actions, reminders, versioned documents (Markdown/text/PDF/DOCX/PPTX), simple
capture and person profiles. It uses no file includes, external scripts or secrets.
Runtime feature activation and backend credentials are configured separately.

The SQL needs PostgreSQL/pgvector/pgcrypto and the usual Supabase `anon`,
`authenticated`, `service_role` roles. The disposable test harness creates those
roles and extensions locally; a Supabase project supplies its standard roles.

<a id="database-setup-existing-installations"></a>
#### Existing installations

**Do not run this script on an existing hosted database.** It intentionally resets
Team Memory tables and data, including document/profile/audit data. Consolidation
was performed locally; the connected hosted project's data, schema and migration
history were not changed. Existing deployments require reviewed additive upgrades,
not this reset. Never run the archived module scripts after a fresh installation.

Earlier module SQL is retained in Git history for audit/reconciliation, including
proposals that were not deployed everywhere. It is no longer duplicated in the
working tree; all definitions are included in the initial schema.
SQLite scripts remain separate because they target a different database engine.

<a id="database-setup-verification"></a>
#### Verification

The standalone script is exercised in disposable PostgreSQL/PGlite by the existing
fresh/reset, lifecycle, chat, reminders, documents, storage and embedding suites.
Backend SQL-backed profile/capture tests load that same one script. No production
credentials or hosted database connections are used by these test harnesses.


Verified 2026-11-06: the standalone script creates all 21 project tables with RLS
and no direct anonymous/authenticated table access; fresh creation and a second
explicit disposable reset both pass. SQL audit, useful-storage, embedding, lifecycle,
chat, reminder and document/format suites pass. The complete backend suite passes
571 tests with real local inference enabled. Earlier audit fixtures were updated
to check versioned reminder occurrence uniqueness and delete only an unreferenced
memory, preserving the current source-evidence restriction.

<a id="migration-history"></a>
## Historical migrations and existing installations

Consolidated from `supabase/migrations/history/README.md`.

<a id="migration-history-historical-module-migrations"></a>
### Historical module migrations

Earlier additive proposals/applied upgrades have been folded into the initial
schema and removed from the working tree. Their original bytes remain in Git
history for review and reconciliation. They are not an active migration chain.

For a fresh database, run only `../20261104000100_initial_schema.sql`. It contains
their definitions and the final document formats in one transaction. Do not run
these module scripts afterward: their objects already exist.

Existing hosted databases retain their existing migration history. Never replay
the destructive consolidated baseline there. Any future hosted upgrade needs a
separate reviewed additive migration and explicit approval. No hosted schema or
migration-history records were modified by the local consolidation.

<a id="deployment-proposal"></a>
## Historical hosted upgrade proposal

Consolidated from `supabase/DEPLOYMENT_PROPOSAL.md`.

<a id="deployment-proposal-hosted-storageembedding-upgrade-proposal-approval-required"></a>
### Hosted storage/embedding upgrade proposal — approval required

For the current **cloud text-storage** task, the smaller proposal in
[CLOUD_STORAGE_STATUS.md](VERIFICATION.md#cloud-storage) supersedes this full upgrade.
`python -m app.schema_upgrade --storage-only` installs only the missing atomic
storage function and its backend grants. The table/embedding backfill below is
not a prerequisite for saving useful text and has not been authorized.

The sole local core migration is `migrations/20261104000100_initial_schema.sql`.
It is a destructive **fresh development baseline**, never a hosted upgrade.
The user authorized local consolidation only. No remote DDL or migration-history
change has been performed for this task.

<a id="deployment-proposal-inspected-live-difference"></a>
#### Inspected live difference

**Latest read-only observation (2026-11-05):** storage and embedding functions/columns are now exposed by the configured hosted PostgREST API. The historical absence findings below are superseded by [REMINDER_VERIFICATION.md](VERIFICATION.md#reminders). Lifecycle/chat/reminder functions and columns remain absent from the exposed metadata. Review exact hosted definitions/grants with `reminder_deployment_review.sql`, then obtain approval for the missing additive migrations `20261105000100`, `20261105000200`, and `20261105000300` in order. No remote changes were made for this milestone. Do not automatically replay an older storage/embedding upgrade, the core reset or migration-history changes.


Automatic chat management additionally prepares `migrations/history/20261105000200_commitment_chat.sql`, after the lifecycle migration. It requires reviewed sender/member mappings, enabled room policies and a separate private adapter credential. See [automatic chat contracts](OPERATIONS.md#automatic-commitments). It has not been applied remotely and must not be enabled against an unreviewed schema.


The separately implemented commitment lifecycle has its own additive migration,
`migrations/history/20261105000100_commitment_lifecycle.sql`. It requires the full reviewed
embedding recovery upgrade before deployment, plus trusted human authentication
before its routes can be enabled. It has not been applied remotely. See
[COMMITMENT_VERIFICATION.md](VERIFICATION.md#commitment-lifecycle) and
[the lifecycle contract](OPERATIONS.md#commitment-lifecycle). It does not replace or replay
the destructive core baseline, and the smaller text-storage-only proposal is not
sufficient for the lifecycle's embedding recovery guarantees.


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

<a id="deployment-proposal-exact-non-destructive-change-set"></a>
#### Exact non-destructive change set

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

<a id="deployment-proposal-preconditions-and-migration-history"></a>
#### Preconditions and migration history

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

<a id="deployment-proposal-verification-after-approval"></a>
#### Verification after approval

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

<a id="deployment-proposal-failure-and-recovery"></a>
#### Failure and recovery

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
