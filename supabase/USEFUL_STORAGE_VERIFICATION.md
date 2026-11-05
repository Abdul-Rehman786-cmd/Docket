# Useful storage and local embeddings — verification record

Updated 2026-11-05. Implementation is local; hosted schema deployment and full
Discord verification are pending approval. Fresh disposable database execution
is blocked by the declined PGlite test-tool installation. No hosted reset or
migration-history rewrite has been performed.

## Existing work checked and reused

The actual listener -> authenticated `/messages` -> durable journal -> private
Switch Codex -> validated decision -> atomic repository -> optional query/reply
path was inspected. The previous strict parser, action consistency checks,
SQLite admission commit, bounded recovery, saved extraction/counts, source
metadata, content-based item keys and silent store-only policy were retained.
Existing authentication, origin/thread correlation, private-room isolation,
viewer behavior, independent listener heartbeats and own-message guards remain.

Earlier defects fixed in the preceding work were silent invalid output -> none,
process-local accepted jobs, source completion before child writes, duplicate
summaries, dropped metadata, automatic room provisioning and double-wrapped
synthesis prompts. Those are verified failure mechanisms; the precise historical
zero-row incident cannot be attributed to a specific model response or restart.
Current live reads are no longer zero-row evidence.

What was missing for this task: the atomic RPC was not installed, no model was
loaded, vector columns had no generation/retry/version pipeline, and questions
used lexical reads only. These gaps now have local implementation, not a claim
that hosted deployment has happened.

## Consolidation and hosted difference

There is exactly one local migration:
`migrations/20261104000100_initial_schema.sql`. The user explicitly authorized
editing this development baseline locally. The prior untracked
`20261105092041_atomic_message_storage.sql` function and permissions were copied
exactly into it, verified for exact inclusion, then the redundant file was removed.
The only connected project's migration list was empty and its live function list
had no `store_message_extraction`; no applied hosted entry was removed. Unknown
external environments require their own history check.

The consolidated baseline now includes atomic storage, durable embedding state
on memory rows, hash/invalidation triggers, guarded claim/finish functions,
queue/lease indexes, completed/current-recipe vector search, and backend-only
function permissions. Reset handling explicitly drops known RPC signatures
before tables; it uses no unrestricted CASCADE. It preserves existing composite
scope/source FKs, extraction deduplication, approval defaults, vector(384) storage,
RLS and table grants. The fresh/reset test runner is ready, but not executed.

The hosted database remains unchanged. Read-only inspection showed 3 source
messages, 6 memories, no vectors, and no embedding job/hash/recipe columns. The
old search function and update-only vector invalidation trigger remain there.
The hosted non-destructive upgrade is concrete in
[DEPLOYMENT_PROPOSAL.md](DEPLOYMENT_PROPOSAL.md). `python -m app.schema_upgrade`
prints the exact transactional delta derived from the sole baseline, without
connecting or applying it. Do not execute the baseline or `db push` on this host.
The six existing memory texts need hash backfill and embedding jobs; no source,
commitment, decision or useful text needs deletion/reconstruction. Any legacy
unversioned vector appearing before deployment must be regenerated deliberately.

## Local model and embedding design

Public model: `Supabase/gte-small`, revision
`93b36ff09519291b77d6000d2e86bd8565378086`, FP32 ONNX. SHA-256 checksums pin the
weights and tokenizer. The ignored local asset cache is populated through
`python -m app.embedding_setup`, never implicitly by runtime inference. Model
setup uses no account/token and sends no conversation. Runtime uses one lazy
CPU session and one inference executor per process. The exact repository
tokenizer supplies WordPiece normalization and special tokens. Right padding
is masked during mean pooling; embeddings are L2-normalized. Stored memories
and search questions share the same revision and preprocessing recipe.

Inputs are bounded to eight items (the worker uses single-item inference within
each at-most-eight-row claim), with at most one inference operation at a time.
Each vector must contain 384 finite values and have unit nonzero norm. The
512-token limit includes special tokens; truncation is disabled. Full useful
text persists first. Oversized text is explicitly failed as `input_too_long`,
not shortened; deliberate chunking is not implemented. Oversized questions fall
back to labelled keyword results. Native inference runs off the HTTP event loop.

Committed memory rows are the durable embedding jobs: pending/processing/
completed/failed, attempts, retry time, five-minute lease, claim token, content
hash and vector hash. Failure never removes useful evidence. Three automatic
attempts and thirty-second backoff bound retries. Expired leases recover; an
exhausted expired claim becomes failed. Content edits clear the vector/token,
compute a new hash and reset retry state. Completion requires the current hash
and token, rejecting old workers even if text is edited back. Unchanged source
replays do not re-embed a completed memory.

Semantic search filters trusted tenant/room, enabled mappings, active status,
completed/current hash and the pinned recipe before ranking/limiting. Matches
retain source UUIDs. Linked relational owners/deadlines/approval/task statuses
and source identifiers are fetched separately. The synthesis prompt requests
`[source:<message_id>]` citations. Explicit `keyword_fallback` metadata separates
lexical results from vector matches; scope failures are not hidden as fallback.

There is no HNSW index for the six-row observed corpus. Exact cosine search is
appropriate. Any future HNSW index must match `<=>` with vector_cosine_ops and
be justified with representative scope/latency/recall tests. No large-corpus
capacity or hosted compute claim is made. No reminder, document, recording,
new messaging, external model-provider or frontend redesign work was added.

## Dependencies and changed components

Added optional `requirements-embeddings.txt`: NumPy 2.3.4, ONNX Runtime CPU
1.23.2, Tokenizers 0.22.2, after checking official model/library docs and Windows
CPython 3.13 wheel compatibility. `requirements-local.lock` records the tested
virtual environment and transitive dependencies (Tokenizers also installs the
Hub client, though model setup uses the existing HTTP library directly).
The project's missing pip/test environment was repaired; `pip check` passes.

- `core/embedding_model.py`: pinned assets, inference, tokenizer/pooling and vector validation.
- `embedding_setup.py`: explicit public download/checksum verification.
- `services/embedding_service.py`: independent durable embedding worker.
- `repositories/embedding_repository.py`: scoped claims/completion, semantic search and linked source/structured reads.
- `services/retrieval_service.py`: semantic retrieval and explicit lexical fallback.
- `main.py`, `core/config.py`, `.env.example`: worker lifecycle and optional enable flag.
- `services/ai_service.py`, `repositories/record_repository.py`: citation-aware synthesis and lexical source references.
- `schema_upgrade.py`: read-only, non-destructive hosted SQL proposal generator.
- `verify_embedding_fixture.py`: opt-in post-approval real database/model fixture with exact-scope cleanup.
- Core migration: consolidation, embedding state/functions/indexes/invalidation/security.
- `tests/test_local_embeddings.py`, `test_embedding_repository.py`: inference, recovery, scope and upgrade safety tests.
- `supabase/tests/embedding_jobs.sql`, `fresh-schema.mjs`, `package.json`: disposable database execution, stale/retry/search assertions.
- README, architecture, deployment proposal and this report: accurate setup, interfaces, evidence and deployment gates.

## Evidence actually obtained

**Real local CPU inference:** the pinned model assets were downloaded and verified.
The final targeted model/scope/proposal suite passed: 16 tests, including the
real weights/tokenizer/ONNX test and unit tests. The real test verifies
384-dimensional normalized output, padding invariance, model reuse, explicit
oversize rejection, and higher similarity for the paraphrased question than an
unrelated fact. A separate measured check produced:

| Measurement | Actual result |
| --- | ---: |
| All three output dimensions | 384 |
| Vector norms | 1.0 within floating-point precision |
| FastAPI fact / paraphrased API-framework question cosine | 0.8807584221 |
| FastAPI fact / unrelated lunch fact cosine | 0.7485604503 |

This proves real vector generation locally, not persistence in Supabase or a
calibrated relevance threshold. No similarity threshold or large-corpus quality
claim was inferred from three examples.

**Offline backend regressions:** Codex/database transports are mocked; journals
are real disposable SQLite files. The final full run with RUN_LOCAL_MODEL_TESTS=1
passed 183 tests with one library deprecation warning. Five offline browser
lifecycle tests also passed. Dependency checks, Python/JavaScript syntax checks
and git diff --check passed; the configured-secret scan found no matches.
There is one core migration file. Tests cover atomic RPC acknowledgement, invalid extraction, duplicate
admission, restart recovery, bounded failures, stale/failing embedding work,
vector validation, semantic source references, scope rejection, explicit fallback,
existing communication/viewer behavior and non-destructive proposal safeguards.

**Live historical schema fixture, preceding task:** 1 message, 1 commitment,
1 decision, 3 memories were written within one transaction. Defaults/scope were
asserted; rollback left 0 fixture messages, rooms and tenants. This verifies the
older schema accepts those records, not the new atomic RPC or real vector storage.
No new hosted write was performed for this consolidation task.

**Not executed:** fresh disposable PostgreSQL/pgvector baseline creation/reset,
new RPC SQL fixtures, real model-vector persistence/search in the hosted project,
and full live Discord ingestion/paraphrased answer. The PGlite installation was
declined; PostgreSQL/Docker are not otherwise installed here. The checked-in
runner is prepared and pinned, but cannot be reported as passed. Remote schema
changes remain separately unapproved.

## Remaining gates and exact live steps

1. Approve/install the disposable schema-test dependencies, then run the fresh
   baseline/reset and SQL fixtures locally (`npm install` then `npm test` in
   `supabase/tests/`). No project credentials or remote access are used by it.
2. Review/approve the non-destructive hosted delta in DEPLOYMENT_PROPOSAL.md.
   Do not mark the reset baseline or removed migration applied artificially.
3. After that approved installation, run the opt-in actual database/model fixture:
   `python -m app.verify_embedding_fixture --run-approved-fixture`. It creates a
   uniquely scoped source/memory, checks replay counts, stores a real vector,
   retrieves it with a paraphrase, verifies source references and cleans only
   that fixture scope. It sends no external chat and has not run yet.
4. An authorized human posts `TM-live-vector-20261105: the project knowledge API
   uses FastAPI.` in the existing configured test room. Verify exact source and
   linked memory/current-hash vector, then ask `Which framework powers the project
   knowledge service?`. Confirm semantic retrieval, traceable source citation,
   correct origin reply and silent useful-message storage. No external test
   message has been sent by this agent.
5. Clean only that uniquely identified human fixture's linked records after
   verifying source UUID and tenant/room. Never delete unrelated team data.

The application still requires persistent private `.local` storage and one API
worker for ingestion, plus one listener. Explicit viewer questions remain
ephemeral. Reply delivery remains best effort after durable storage; there is no
reply outbox. Existing useful records and privileged backend access boundaries
are preserved while deployment remains pending.

Official references checked: [model/pooling/limits](https://huggingface.co/Supabase/gte-small),
[ONNX Runtime CPU](https://onnxruntime.ai/docs/get-started/with-python.html),
[Tokenizer API](https://huggingface.co/docs/tokenizers/python/latest/api/reference.html),
[Supabase functions](https://supabase.com/docs/guides/database/functions),
[local PostgreSQL test engine](https://pglite.dev/examples).
