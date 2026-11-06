# Document discovery and questions in Discord

Implemented locally, 2026-11-06. No hosted changes, shared-service restarts,
real document edits or live Discord verification were performed.

## Conversation

Address the existing bot using the normal supported mention mechanism:

```text
You: @team-memory-client List our documents.
Bot: 1. Launch plan (planning.pdf)
     2. Launch deck (launch.pptx)
     Reply with a number or filename.
You: @team-memory-client Find the presentation about our launch.
Bot: Selected Launch deck (launch.pptx). Ask a question about it.
You: @team-memory-client What does planning.pdf say about the deadline?
Bot: [Answer from the retrieved passages]
     [Document: Launch plan (planning.pdf); stored text lines …]
You: @team-memory-client Who is responsible?
Bot: [Answer about that same document, with a document citation]
You: @team-memory-client Summarize it.
Bot: [Summary with citations]
     Partial summary of sampled passages; some sections may be omitted.
You: @team-memory-client clear selection
Bot: Document selection cleared.
```

Discovery may instead show several numbered choices or report no convincing
match. Choose a displayed number or an unambiguous filename/title. Use `select
Launch plan`, `use planning.pdf` or `switch to planning.pdf` to change documents.
Quote filenames containing spaces: `find "Launch Plan.pdf"`. Use `more documents`
for the next list page. Numbers refer to the most recently displayed page/choices
for that sender. Bare `yes`/`save` remain upload/draft consent, never selection.

Mention the bot to begin a document conversation. While that same sender's
tenant/room conversation is active, subsequent replies need no mention. Both `1`
and `1 can you explain whats in it` go through AI document routing. A combined
choice/question uses an ask/summary intent with the displayed choice and can
answer in the same turn. Unrelated chatter receives no unmentioned document reply.
After expiry, clear or API restart, begin again with a mentioned request; previously
displayed choices are not restored from old channel messages.

Requests are interpreted by the existing AI gateway rather than matched against
fixed phrases. For example, “what is in our shared library?” can mean a document
inventory, “the second one” can choose a displayed document and “give me the gist”
can request a summary. Those are examples, not required wording. Inventory still
comes from scoped database metadata, without inventing uploader ownership or
historical upload filters the repository does not support.

## Reuse and boundaries

The path remains Discord → Switch → existing listener → authenticated `/messages`
→ AIService → DocumentChat → existing Switch channel reply. No new endpoint,
registration, member mapping, sender classification or messaging integration was
added. Existing self/AI echo guards and authenticated adapter metadata remain in
force. Beginning a conversation requires the normal bot mention/addressing
behavior. Unmentioned continuations additionally require the sender's own active
conversation; the model cannot create that authorization by proposing an intent.

Existing imports, immutable versions, chunk embeddings, publication, document-only
semantic search, room resolution and private `ask_switch(..., synthesis=True)`
gateway are reused. General memory, commitment, profile and attachment-consent
paths remain separate. Before general reasoning, an enabled addressed-message
or authorized active-continuation path asks the existing private gateway for a structured document intent using the
bounded message, selection/choice availability and two short context turns. No
document content or raw tenant/room/document IDs are provided for classification.
The `DocumentIntent` model rejects unknown actions/fields, conflicting targets,
invalid choice numbers and oversized values. A named reference must be copied
from the current message. Backend name resolution, scope, current-publication
checks and cached choice validation remain authoritative.

The classifier can propose list, more, find, select, ask, summarize, clear,
clarify or none. Only a valid none proposal permits ordinary reasoning to resume;
malformed routing requests clarification rather than silently querying memories.
A selected document does not force unrelated questions into document mode. Known
profile/task workflows take precedence, and deterministic attachment/draft consent
remains outside classification. Interpretations are probabilistic: these checks
validate authority and structure, not the model's understanding of every message.

Listing uses metadata, never AI-generated inventory. A small backend-only
repository `catalog` operation checks the existing RPC's room authorization,
then reads active documents joined to their current completed published version
and enabled room. Queries explicitly constrain tenant and room and return
title/filename/publication IDs, without source text or vectors. It requires the
existing Data API tables, grants and `documents_published_version_fk` relationship;
no database migration is needed. No privileged client credentials are exposed.

Discovery combines bounded title/filename metadata and existing semantic hits,
grouped by document. Exact names resolve directly. Vague candidates are judged
from their labels/passages by the existing reasoning gateway, rather than an
invented similarity threshold. This judgment is probabilistic and can be wrong;
use an exact filename if the proposed choice is incorrect.

Questions retrieve only the selected document's current published version.
Scope, activity and publication are checked before retrieval and after synthesis.
Publication changes during synthesis discard the answer and ask for a retry;
lost access, archiving, deletion or unpublication clears selection. A replacement
published between questions resets conversational history. Unpublished replacement
work does not hide the previous successfully published version.

The model receives bounded passages and at most two short document-related turns.
Historical generated answers are explicitly context, not evidence. Prompts treat
all supplied text as untrusted data, never instructions. A structured response
must identify allowed evidence references; missing/unsupported/malformed sources
produce a no-reliable-answer response. Readable source labels and exact stored
text lines are rendered by the backend, then use the existing secret redaction
and reply cleaning. No original page/slide coordinates are inferred. Reference
validation does not independently prove every model-generated claim is correct.

## State, limits and recovery

- Selection/choices: process-local, keyed by tenant, room and stable Switch sender.
  Expires after **15 minutes of document activity**, API restart or bounded-cache
  eviction. At most 200 sender scopes; no full documents retained.
- Two context turns, each question/answer capped at 500 characters. A new document
  or published version clears history. Failed requests do not commit partial state.
- Source-message replay guard: 2,048 identities for 30 minutes. An old selection
  message cannot act on a newer displayed list within this window; the existing
  durable ingestion journal also prevents normal duplicate delivery/recovery.
- Use **one API process** for consistent conversational selection. Multiple
  independent workers do not share selections. Uploaded documents remain durable
  regardless of selection expiry or process restart.
- Lists: five entries/page, bounded offset up to 10,000. Discovery reads at most
  50 initial metadata entries plus a bounded exact-name lookup, and 20 semantic
  chunk hits. At most eight grouped candidates are considered and five shown.
  Discovery is bounded, not an exhaustive full-corpus search.
- Questions: up to eight retrieved passages, each at most 2,000 characters;
  total evidence at most 16,000 characters. At most three actual passage citations.
  Enabled addressed messages and authorized active continuations use one bounded intent-classification gateway call.
  Listing/exact selection require no further AI call; a resolved question adds one
  synthesis call, and vague discovery plus answering adds at most two. Unrelated
  requests continue the normal reasoning flow after classification. All calls
  reuse the existing private gateway, timeout and model; no new provider/model is
  installed. Known task/profile workflows and save/skip consent bypass this call.
- Summaries: source-order samples spread from beginning to end, at most eight
  chunks from the existing maximum 1,024. Always labeled partial; not a promise
  of complete coverage or a substitute for reading the document.

## Setup and local verification

No new dependencies, models, SQL scripts or environment variables are required.
Use existing `DOCUMENT_INGESTION_ENABLED`, `LOCAL_EMBEDDINGS_ENABLED`, configured
tenant/room mappings and private AI gateway setup. The listener must send its
existing dedicated `SWITCH_ADAPTER_TOKEN` in addition to ingest authentication;
the trusted token must have at least 32 characters and differ from shared/admin
credentials. Use ignored backend configuration, never chat or a client build.

Upload consent uses `SWITCH_DOCUMENT_ADAPTER_TOKEN`; ordinary document questions
use the separate `SWITCH_ADAPTER_TOKEN`. An upload startup message reporting
`adapter_configured=True` therefore does not verify chat authentication. Listener
startup now reports `general_adapter_ready` separately. If it is false, configure
the general credential in the ignored local configuration and reload both API
and listener. Explicit document questions fail with a setup explanation instead
of falling back to unrelated memories when adapter identity is unverified.

After review, the operator must load the updated backend code and verify with
approved disposable published documents in Discord. Shared services were not
restarted by this implementation. The hosted metadata catalog path was
subsequently verified through read-only backend requests during troubleshooting,
without reading source text or modifying documents. Live gateway document
relevance/answer quality and the updated end-to-end chat flow remain rollout checks.

Local commands from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_document_chat.py -q
$env:RUN_LOCAL_MODEL_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use fake AI/HTTP responses, scoped in-memory document storage and temporary
SQLite ingestion journals. The opt-in test performs real installed local-model
embedding/retrieval/publication, with fake answer synthesis. The disposable
PostgreSQL/pgvector suite (`node document-schema.mjs` from `supabase/tests/`)
checks existing document/embedding/storage contracts and the metadata join.
Neither test path contacts hosted services or sends live messages.

Verification for this implementation: full backend run **595 passed** (including
real local model tests); after the final filename/Unicode regression addition,
the final document-chat run **25 passed**, including real local inference. The
disposable PostgreSQL document, storage, embedding and format suites also passed.
The only reported warning is the existing Starlette/httpx test-client deprecation.

Saved-inventory/authentication troubleshooting regression run: **601 passed,
5 skipped**. This run covered the original saved-document question, missing
adapter fail-closed behavior, authenticated delivery and separate startup
credential diagnostics. Opt-in local-model cases were skipped in that run.
The ignored local configuration was repaired without restarting shared services;
both API and listener must load matching general adapter configuration.

AI-intent routing revision: **637 backend tests passed**, with opt-in real local
embedding tests enabled. Classifier and answer responses are mocked for repeatable
behavior tests, including indirect wording without document keywords, ordinals,
follow-ups, malformed/unauthorized proposals, consent and unrelated-request routing.
This verifies backend routing and enforcement, not live model intent accuracy.
The revised prompts still need fresh live Discord verification after API reload.

Unmentioned-continuation regression run: **642 passed, 5 skipped**. Tests cover
bare choices, combined choice/questions, no-context/expired/restarted conversations,
sender/room/tenant isolation and unrelated unmentioned chatter. External AI and
delivery remain mocked; opt-in real-model tests were skipped in this run.
