> Local format/consent revision (2026-11-06): PDF, DOCX and PPTX extraction,
> channel-only replies and simple consent are implemented locally. The approved
> binary-format constraint migration is applied; service reload and live binary
> verification remain outstanding.
> [Current limits, citation semantics and rollout](DISCORD_DOCUMENT_INGESTION.md)
> supersede the original Markdown/text-only limits below.

# Versioned document ingestion

Document discovery and cited questions now reuse this module through the existing
authenticated Discord/Switch pipeline. See [document chat](DOCUMENT_CHAT.md) for
commands, sender-scoped selection, bounded retrieval, verification and rollout.
The standalone HTTP document API below retains its existing authentication.

The optional consent-based Discord collection adapter is now implemented locally
and disabled pending trusted setup/live approval. It reuses this deployed document
module; see [Discord attachment setup and verification](DISCORD_DOCUMENT_INGESTION.md).

Implemented and deployed, 2026-11-05. Hosted upload, actual vectors, automatic
publication, semantic citations and failure safety passed isolated verification.
The backend feature is enabled; its source-code default remains disabled for new
installations. See [deployment evidence](DOCUMENT_DEPLOYMENT_VERIFICATION.md).
Message and commitment records and the ingestion journal remain separate.

## Access, input and contracts

All `/api/v1/documents` routes require existing `X-Admin-Token` authentication.
This is a restricted development-admin facility, not individual-user upload
authorization. Never put that token in a URL or mobile application. Required
`room_id` query parameters identify a Switch room. The backend resolves its
configured tenant, allow-list and enabled database mapping. Callers cannot choose
a tenant or importing actor. Recorded actor is `development-admin`, not a human.
Every database operation rechecks enabled tenant/room scope.

Upload raw UTF-8 `.md`/`.txt` bytes, maximum 262,144 bytes, using
`application/octet-stream` or `text/plain; charset=utf-8`. Metadata query fields:
`import_id` UUID, `source_key` (stable source identity), `filename`, `title`,
`expected_version`, and `room_id`. Strings are limited to 200 characters.
Filenames are metadata, never server paths. A client reads a local file and
uploads bytes; the server never follows links, downloads embedded resources,
executes instructions/frontmatter, or renders trusted HTML. Source text and line
endings are preserved. Invalid UTF-8, binary NULs, empty input, unsupported suffixes,
excessive size and excessive processing are rejected. No document text is logged.
Two imports may process concurrently; further admissions receive `429`.

| Interface | Contract |
| --- | --- |
| `models/documents.py` | Trusted scope and explicit import, document, version, chunk, search and citation models |
| `DocumentRepository.call(scope, operation, data)` | Persistence boundary shared by in-memory tests and Supabase RPC adapter |
| `DocumentService.import_bytes(scope, metadata, raw)` | Validate, chunk and atomically persist; return stable identifiers and processing state |
| `chunk_text(text, tokenizer)` / `local_chunks(text)` | Exact slices, SHA-256 hashes, character offsets and one-based inclusive line locations |
| `DocumentWorker.process_once(scope)` | Claim <=4 document chunks; finish with lease token, content hash and model-recipe guards |
| `DocumentService.search(scope, DocumentSearch)` | Dedicated published-document retrieval adapter with machine-verifiable source references |

Repository operations: `import`, `import_status`, `documents`, `document`,
`versions`, `version`, `chunks`, `chunk`, `archive`, `claim`, `finish`, `search`.
The chat-only `catalog` adapter operation adds a scoped metadata read; it is not
a new SQL RPC operation.
Runtime uses `SupabaseDocumentRepository`. `MemoryDocumentRepository` is for tests
only and does not acknowledge durable production storage.

Reused components: admin auth, trusted room resolution, privileged-key headers,
HTTP pool, pinned actual tokenizer, 384-D vector validation, loaded local model,
model lock and single inference executor. No new dependency or second model
session is added. The adapter remains independently callable through the search
API. The document chat service also uses it for explicit document questions;
general room-memory retrieval remains separate.

## Identity, chunking, recovery and publication

Documents are unique by `(tenant_id, room_id, source_key)`. Source format stays
stable. Imports use idempotency UUIDs and request hashes; reusing a UUID with
different parameters returns `409`. `expected_version` is the latest **accepted**
number, initially zero. Concurrent imports with the same expected number cannot
silently overwrite each other. Exact request replay returns the original version.
Unchanged latest content/hash/recipe/title reuses its version and chunks. Changed
content creates a numbered version. Returning to older content creates a new
version; identical text in different documents retains independent provenance.
Title changes are versioned; filename-only changes do not rewrite history.
Recipe changes deliberately create a new version on reimport.

One transaction persists source, version, chunks/jobs and import identity.
HTTP `202` means recoverable acceptance, **not** publication. Source survives
embedding failure and application restart.

Recipe `source-slices:v1:target448:overlap32:paragraph-heading` targets 448 actual
tokens, including special tokens, with a hard 512-token model limit and at most
32 source-token overlap. Paragraph/heading boundaries are preferred where practical.
No heading/context is added to the embedding input: supporting text is exactly
the embedded source slice. Oversized paragraphs split deliberately. Section labels
are bounded metadata; complete headings remain in source. Maximum 1,024 chunks
and 64,000 characters per inference input. Pathological whitespace that cannot
form a bounded nonempty input fails explicitly before acceptance, never silently
losing source text. Embeddings use the existing pinned FP32 ONNX, mean-pooling,
L2-normalization recipe from `core/embedding_model.py`.

Chunk jobs have five-minute leases, unique claim tokens, three attempts and
thirty-second backoff. Expired leases recover with new tokens; exhausted claims
report `lease_exhausted`. Failures use allow-listed categories. Only the latest
accepted version claims or finishes work. New imports mark unfinished older
versions `failed/version_superseded`; historical completed chunks remain intact.
Stale tokens, hashes, model recipes, versions and expired leases cannot publish.

After all chunks complete, the latest version is published atomically. The previous
published version stays searchable during replacement and failure. History and
source are retained indefinitely in this first release. Archive excludes retrieval
without deleting evidence. There is no permanent-deletion endpoint. Foreign keys
use `ON DELETE RESTRICT`; any separately authorized deletion must clear the
publication pointer, then explicitly handle imports, chunks, versions and documents.

## API and citation verification

Paths below start `/api/v1/documents`. All require admin auth and authorized
`room_id`. Lists use `limit` 1–50 (default 20), `offset` 0–10,000 and stable ordering.
Private responses use `Cache-Control: no-store`.

| Method/path | Result |
| --- | --- |
| `POST /imports` | Raw bytes; `202` import/document/version IDs, duplicate flag, processing status |
| `GET /imports/{import_id}` | Durable import status |
| `GET` on the prefix | Scoped document list |
| `GET /{document_id}` | Identity, latest accepted number, published pointer and active status |
| `GET /{document_id}/versions` | Historical versions and source |
| `GET /{document_id}/versions/{version_id}` | Source/hash/recipe, processing status, safe error |
| `GET /{document_id}/versions/{version_id}/chunks` | Exact slices and job state |
| `GET /{document_id}/versions/{version_id}/chunks/{chunk_id}` | Inspect a citation's supporting text |
| `POST /{document_id}/archive?expected_version=N` | Compare version, then archive |
| `POST /search` | JSON `query`, optional `document_id`, `limit` 1–20 |

Errors: `401`/`403` for authentication/scope, `404` for inaccessible identities,
`409` for stale versions/idempotency conflicts, `413` for size, `415` for format,
`422` for invalid input, `429` for saturated import admission, `503` for disabled
or unavailable processing.

Search filters tenant, enabled room, optional document ID, active status,
published version, completed state, content hash and model recipe **before**
ranking/limiting. Results contain exact text, title, section, line range, score,
`source_type="document"` and citation
`document:{document_id}:{version_id}:{chunk_id}`. This cannot be confused with a
message UUID citation. Verify identifiers through chunk inspection and compare
supporting text; a model-generated identifier alone is not provenance proof.
Exact cosine search uses `<=>`. No ANN index or production-scale claim is made.
Benchmark realistic scoped latency/recall before adding HNSW `vector_cosine_ops`.

## Deployment contract and completed execution

Read-only hosted inspection found existing memory embedding functions but no
document/version/chunk tables. The additive migration is
`migrations/history/20261105163303_versioned_documents.sql`: four new tables, composite
scope/source constraints, queue/scope indexes and backend-only `document_operation`
RPC. It changes no existing message or commitment object. All document tables
enable RLS and revoke `PUBLIC`/`anon`/`authenticated` privileges. Backend
`service_role` gets access. The RPC is `SECURITY INVOKER`, has fixed search path
and backend-only execution. No public policies are introduced.

1. Review this migration and current hosted state. Coordinate with commitment
   work; this workspace currently contains no commitment lifecycle migration.
2. Obtain approval for this migration only. Never replay the destructive core
   baseline, run indiscriminate `db push`, rewrite migration history or change
   paid resources. Record only the actual additive deployment.
3. After separate approval, run from `backend/`:
   `python -m app.verify_document_fixture --run-approved-fixture`.
   The harness preflights all document tables and RPC before writing. It exercises
   actual authenticated FastAPI handlers, mapping checks, local vectors,
   paraphrase search, citations, unchanged dedupe and safe replacement. It cleans
   only its generated tenant/room and reports cleanup failure safely. It never
   sends Switch/Discord messages or opens the message ingestion journal.
4. Only after verification, set `DOCUMENT_INGESTION_ENABLED=true` in ignored
   backend configuration and restart FastAPI. Keep `LOCAL_EMBEDDINGS_ENABLED=true`
   and pinned model assets installed. No credentials belong in client config.

These deployment steps were completed with approval in the configured project;
see the execution record above. New environments still require their own review,
approval and verification. Future changes to applied SQL require new migrations.

## Verification and limitations

Executed: domain/API tests with fake embeddings and a controllable clock; real
local tokenizer/model import → publication → paraphrased search → exact citation
verification using the in-memory repository; disposable PostgreSQL/pgvector
schema, transaction, lease, isolation and permission tests. Missing hosted schema
preflight is tested to create zero rows. Existing message-storage and embedding
SQL suites also pass with the additive document schema installed.

Final results: **223 backend tests passed**, including both opt-in real-model
tests (one existing test-library deprecation warning). The document suite accounts
for 21 tests. `node document-schema.mjs` passed the document contract suite and
both existing useful-storage/embedding SQL suites. Syntax and diff checks pass.

Two broader baseline checks failed in unchanged files: `fresh-schema.mjs` stops
inside `database_audit.sql` on a message/commitment `ON DELETE RESTRICT` fixture
cleanup error, before loading any document migration. All five viewer JavaScript
tests fail because their DOM mocks lack existing viewer controls (`classList`
and `appendChild`). No document module code runs in that viewer harness. These
separate audit/viewer files were not modified for this task; those checks are
not reported as passing.

Run backend tests with `python -m pytest -q`; set `RUN_LOCAL_MODEL_TESTS=1` for
actual ONNX/tokenizer tests. From repository root run
`npm run test:documents --prefix supabase/tests` and
`npm test --prefix supabase/tests`. Disposable runners never load credentials or
connect remotely.

Hosted document writes/search and automatic processing are now verified and
enabled; current counts/results supersede the earlier local-only totals in this
document. No frontend upload UI, public individual-user routes, providers,
PDF/OCR, edits/approval, conflict detection, automatic Discord document synthesis,
commitment lifecycle changes, permanent deletion, retention cleanup, manual
failed-job retry API or throughput/corpus benchmark is included.
