# Team Memory architecture

## Runtime and trust boundaries

```text
Discord -> Switch input rooms -> non-AI Python listener -> authenticated POST /messages
                                                               |
                                                    durable local ingestion journal
                                                               |
                                             serialized private Switch Codex runtime
                                                               |
                                               validated four-way extraction decision
                                                               |
                                             backend-only atomic Supabase transaction
                                                               |
                                           optional query and grounded answer synthesis
                                                               |
                                       browser viewer / Switch reply to origin room
```

FastAPI and the listener are separate processes. Run one API worker and exactly
one listener. The listener owns the input-room claims and answers heartbeats
independently of HTTP forwarding. Its acknowledged cursor, connection ID and
generation remain in ignored `backend/.local/switch-listener.json`. It advances
the checkpoint only after successful API acknowledgement. A retryable HTTP
failure leaves the event unacknowledged for replay; takeovers and retention gaps
stop with a diagnostic rather than skipping data.

The existing private Codex runtime remains behind Switch. No model provider,
Discord bot, independent AI responder in input rooms, recording control,
scheduler or document integration is introduced. `python -m app.switch_setup`
reuses the existing AI and non-AI identities. Setup has already been performed
for this workspace; it never rotates existing identities on a registration
conflict or widens addressing permissions automatically.

Every model call checks that the internal AI room has no bridge/external channel,
contains only the requester and Codex, and is separate from the input rooms.
It checks AI runtime sessions and input-room membership before sending.
Responses must match the exact internal room, opaque sender and request thread.
The listener uses `SWITCH_REQUESTER_*`; `SWITCH_AGENT_*` continues to identify
the actual AI. Do not attach the managed AI independently to input rooms.

## Admission, deduplication and recovery

`controllers/messages.py` authenticates `X-Ingest-Token` and delegates to
`services/ai_service.py`. `message_service.py` validates the configured tenant
and explicit Switch room allow-list. Unknown scope cannot write or create jobs.
The model never chooses scope. Supabase separately checks an enabled existing
`rooms` mapping for the supplied tenant; ingestion no longer provisions or
reassigns database rooms. Missing mappings require trusted operator provisioning.

Before acknowledging human input, the backend commits a redacted ingestion job
to SQLite at ignored `backend/.local/ingestion.sqlite3` using full synchronous
commits. This is persistent application state, not a secret file. Keep this
folder private, on persistent storage, and back it up with the deployment.
Deleting it loses pending jobs and durable chatter deduplication. Host/disk loss
is outside this single-host recovery guarantee; do not deploy multiple API
processes against the journal. Secrets remain only in ignored local `.env`
files or approved host secret storage, never in the journal.

The durable source reference hashes tenant, Switch room and Switch message ID.
The logical AI request UUID is derived from that reference. Accepted duplicate
HTTP deliveries and replayed Switch events reuse the same job. An admission lock
covers viewer submissions, durable ingestion and recovery; at most four AI jobs
are running/queued. New work returns 429 before acknowledgement when configured
AI capacity is full. A journal failure returns 503 before retaining a preview.
If the private runtime is not configured yet, human input remains durably pending
and a diagnostic lists required configuration variable names only.

Startup and a two-second recovery poll load authorized pending/interrupted jobs.
Failed jobs wait thirty seconds between attempts; there are at most three total
automatic attempts. A validated extraction is redacted and saved before writes;
a retry reuses it. Stored counts are saved before synthesis, avoiding repeated
writes after a later query failure. If a database commit succeeds but its HTTP
response or local checkpoint is lost, the atomic database function recognizes
the source and returns existing counts without overwriting records or approvals.
Completed jobs retain only reference, scope, attempts, status and counts; their
source text, extraction and routing payload are cleared. Failed jobs retain
redacted evidence for recovery. SQLite secure deletion is enabled; this is not
a guarantee against filesystem snapshots or disk forensics.

`python -m app.ingestion_jobs` prints safe references, state, attempts, categories
and stored counts. To retry an exhausted job, stop the API, run
`python -m app.ingestion_jobs --retry MESSAGE_REF`, then restart it. This resets
attempts for the same job and keeps its validated extraction/counts. It cannot
reset completed jobs. Scope changes leave removed rooms unprocessed; only
currently authorized rooms are recovered.

Preview receipts (1,000), browser previews (100), viewer questions/results (100)
and aliases (100) remain bounded and process-local. Explicit viewer requests
are ephemeral; automatic incoming ingestion is durable. A completed automatic
source evaluation can be reused by viewer aliases while its result is retained.
Preview text is capped at 2,000 characters. Ingestion evidence/prompt text uses
the same credential redaction with the full admitted 64,000-character limit.

## Extraction and permitted storage

`models/ai.py` defines `store_only`, `query_only`, `store_and_query`, and `none`.
`ask_switch` sends the decision prompt for every evaluation and the distinct
synthesis prompt directly for answer generation. Chat text and retrieved records
are quoted untrusted data, never executable SQL or routing instructions.

The parser accepts a JSON object (including a JSON fence), validates the action,
rejects unknown fields and blank items, and checks action/item/query consistency.
A valid `none` has no items and no search query. Malformed JSON, invalid/missing
action, invalid fields, empty storage and contradictory output are failures;
they cannot become successful `none` filtering. Diagnostics contain safe
categories, never raw model output. There is no unbounded model repair loop.

Useful facts, commitments and settled decisions are evaluated automatically;
no capture command is required. Suggestions are not confirmed decisions.
Mixed greetings retain useful facts in extracted memory, while the redacted
source preserves the evidence. Owners, deadlines, timezone and agreement must
not be invented. Relative deadlines stay in the description with `due_at=null`
unless an explicit dated/timezoned timestamp is supported. A timestamp supplied
by Switch is preserved; when absent, source `occurred_at` uses API observation
time because the schema requires a value. Missing original platform identifiers
remain null. External Switch/platform identifiers are separate from database UUIDs.

`services/extraction_service.py` builds fixed permitted records with content-based
item keys, deduplicates repeated items, and generates one memory per structured
commitment/decision. The model's separate memories array contains additional
facts only, avoiding duplicated summaries. Committed memory rows independently become durable embedding jobs.

`repositories/record_repository.py` calls the service-role-only
`store_message_extraction` RPC. The single consolidated development baseline
`supabase/migrations/20261104000100_initial_schema.sql` defines it alongside embedding state/functions.
The hosted non-destructive delta requires review/approval; never run the baseline reset remotely. The function uses SECURITY INVOKER, a fixed
search path, explicit tables/columns, an enabled room mapping and a transaction
advisory lock per source. Source, commitments, decisions and memories commit
together. Only then is extraction marked completed. Failures roll back the whole
write. Replays of completed sources return counts without mutating existing data.
Commitments retain database defaults `pending` / `not_started`; decisions remain
`pending` approval. The model cannot assign approvals, task status or record scope.

The deployed foundation already has tenant/room composite foreign keys,
source identifiers, extraction tracking and item uniqueness constraints. The
foundation migration is destructive and must never be rerun on this database.
The user explicitly authorized local consolidation of the development baseline; hosted migration history is unchanged. Existing incomplete data incorrectly marked
completed by older code is not automatically rewritten by the new function.

## Local embedding worker and semantic retrieval

The backend owns one lazy `Supabase/gte-small` model per process, using FP32 ONNX
weights at revision `93b36ff09519291b77d6000d2e86bd8565378086`. Public setup assets
are checksum-verified and kept in ignored `.local/models`; runtime never downloads
models or sends chat to the host. `core/embedding_model.py` reuses one tokenizer,
one CPU inference session and one executor thread. Two intra-op CPU threads and
one inter-op thread bound native computation. The pinned tokenizer's WordPiece
normalization and special tokens are preserved, padding is masked during mean
pooling, and vectors are L2-normalized. Both memories and questions share the
same revision and preprocessing recipe, recorded in the database.

The model accepts 512 tokens including CLS/SEP. Truncation is explicitly disabled.
Oversized memory text remains fully persisted and its job becomes
`failed/input_too_long`, exhausted until a deliberate operator action/content edit.
This implementation records that limitation instead of silently dropping evidence
or inventing memory chunks. Oversized questions use an explicit keyword fallback.
Each returned vector must be 384-dimensional, finite, nonzero and normalized.

`services/embedding_service.py` runs independently of AI extraction and HTTP
admission. It claims at most eight committed memory rows, performs bounded
single-item inference on the shared executor, then finishes each via a guarded
RPC. Memory text itself is the durable job; no process-local embedding queue can
lose acknowledged memory. Rows carry pending/processing/completed/failed status,
content hash, attempts, next-attempt time, five-minute lease and a claim token.
Only three attempts are automatic, with thirty-second backoff. Expired leases
are recoverable; an exhausted crashed lease becomes failed. The SQL completion
predicate checks source scope, current content hash, processing state and current
claim token. A content edit computes a new hash, clears vector/token/completion,
and resets retry state. A stale worker cannot attach a vector even after text
is edited back to an earlier value, because its token was invalidated.

Embeddings supplement relational owner, deadline, approval and task/decision
status fields. Failures do not roll back useful source/extracted text. Completed
unchanged memories are not re-embedded on source replay. `LOCAL_EMBEDDINGS_ENABLED`
can disable inference while retaining explicit keyword retrieval and storage.
No inference task blocks the event loop or listener heartbeats.

`services/retrieval_service.py` embeds a redacted question and calls the protected
`search_memories` RPC once per trusted authorized room; Discord-origin requests
use only the origin room. The function filters tenant, room, active flag,
completed state, content hash and pinned recipe before ordering and limiting.
Per-room top-five candidates are merged to a global top five. Only eligible
vectors rank, with message UUIDs preserved for citations. The backend fetches
linked structured rows and source identifiers under the same authorized scope;
Codex is instructed to cite `[source:<message_id>]` and not invent unsupported facts.
If embeddings fail or no matches exist, bounded lexical reads provide explicitly
labelled `keyword_fallback` records, never labelled vector matches. Scope denial
is not downgraded to fallback.

Current live corpus observation: six memories, zero vectors. Exact cosine search
with existing scope indexes is appropriate. HNSW is deferred until representative
latency/recall tests justify it. A future cosine HNSW index would use
`extensions.vector_cosine_ops`, matching `<=>`; no capacity or compute upgrade
is claimed or performed here.

## Replies and viewer behavior

`store_only` is silent even when addressed. Unaddressed `none` is silent.
Query actions synthesize a grounded answer; addressed `none` can return its answer
under the existing policy. `SWITCH_REPLY_TO_DISCORD` controls outbound posting.
Only automatic Discord-origin requests can post through Switch to their original
room/thread. Viewer-origin questions stay in the viewer.
Typed viewer storage requires exactly one authorized room; otherwise a source
message must identify the destination. A selected source without retained original
evidence cannot be stored under a fabricated Switch identifier.
No Preferences UI, `reply_mode` setting or `router.py` is implemented.

Processing is completed durably before attempting outbound delivery. A failed
Switch reply does not trigger another extraction/write. Delivery is best effort:
a crash after completion can lose a reply, and there is no durable reply outbox.
Own-message guards use configured sender IDs and the latest 100 posted content
fingerprints; fingerprints also survive restart in the journal. Fingerprints are
saved before posting, catching early echo events even when participant IDs differ.

The public viewer shell at `/` embeds no private data or credentials. Unlock uses
a masked ingest token retained only in tab memory; `/viewer/ws` authenticates in
its first frame, enforces same-origin policy and rechecks scope/credentials.
Lock closes the socket and clears token/content/pending requests. Text uses
`textContent`, restrictive CSP and no-store responses. Browser heartbeats,
reconnection, bounded event queues and viewer-only questions remain intact.
`POST /ai/requests`, `GET /ai/requests`, preview/status routes and the viewer socket
require ingest authentication. `/api/v1/*` record routes require the separate
admin token and remain local/server-side interfaces, not mobile user auth.

## Configuration, layers and diagnostics

`main.py` owns reusable async Supabase/Switch HTTP pools and AI/embedding worker
startup and shutdown. `core/config.py` reloads local settings when ignored `.env` files change;
new work uses current credentials without embedding them in jobs. `/health`,
`/docs` and `/openapi.json` start without service credentials and prove liveness
only. `models/` validates data, `controllers/` handles HTTP, `services/` owns rules,
`repositories/` accesses data and `core/` owns authentication/configuration.

Required storage settings: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`SWITCH_TENANT_ID`, `SWITCH_ROOM_IDS` plus the existing private runtime settings.
Ingestion requires `SWITCH_INGEST_TOKEN`; listener delivery additionally needs
`SWITCH_BRIDGE_URL` and the existing client credentials. Optional reply/debug
settings are independent. Credentials are backend-only; no privileged key belongs
in Expo `EXPO_PUBLIC_*` variables. Existing RLS/client grants are unchanged.

Default processing logs contain references, actions, states, counts and sanitized
failure categories. They never print full chat/model/synthesis text, headers or
credential-bearing URLs. `LOG_DISCORD_MESSAGES=true` remains an explicit opt-in
for bounded redacted local message previews. Wire-level HTTP/WebSocket logging
must stay disabled. `LOG_LEVEL` controls application diagnostics.

## Verification and limits

See `supabase/USEFUL_STORAGE_VERIFICATION.md` for actual evidence and approval gates.
Default offline tests mock Codex/Supabase and use real disposable local SQLite journals.
An opt-in real ONNX test verifies actual local vectors. Fresh disposable PostgreSQL
creation/reset is prepared but blocked by declined test-engine installation.
The hosted plan is documented in `supabase/DEPLOYMENT_PROPOSAL.md`; no remote schema
change has been applied.
`supabase/tests/useful_storage.sql` tests the approved RPC with disposable database
fixtures, replay protection, approval defaults, source metadata, scope rejection
and transaction rollback; `embedding_jobs.sql` checks lease/hash/semantic behavior; all fixture changes roll back. A separate human-posted
Discord fixture is still required to verify the complete live bridge/runtime path.

Reminders, recording controls, document ingestion/write-back, mobile private-data
authentication and additional messaging integrations are outside this task.

References: [Supabase database functions](https://supabase.com/docs/guides/database/functions),
[Switch agent protocol](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol),
[Switch events](https://docs.switchagents.ai/switch-rooms/internals/agent-events),
[Switch connections](https://docs.switchagents.ai/switch-rooms/internals/agent-connection).
