# Discord document consent and ingestion

Current local implementation, 2026-11-06. This revision changes channel replies,
consent and supported formats. Its approved format migration is applied to the
configured Supabase project, and backend dependencies are installed in the project
venv. Shared service reload and live binary-flow verification remain outstanding.
No live messages, real team-document changes, deployment setting changes or shared
service restarts were performed. Earlier Markdown/text ingestion was reported
working by the user; that does not verify the binary formats in the hosted flow.

## Conversation

```text
User: [uploads planning.pdf]
Bot: Save planning.pdf to team memory? Reply yes or save, or no to skip.
User: yes
Bot: Processing planning.pdf…
Bot: Saved planning.pdf to team memory.
```

All document prompts, updates, errors, ambiguity explanations and success replies
go to the originating room/channel. No thread/reply parameter is sent to Switch.
Original upload/thread metadata remains in the request's provenance. Delayed or
retried outbox entries from older versions are rendered again from request state;
their stored reference-bearing text and thread destination are ignored.
Unrelated conversational replies retain their existing routing.

The Switch documentation MCP confirms thread correlation is explicitly carried
by the replied-to event ID. The verified `post_message` operation accepts `body`
and optional `thread_id`: omission is a top-level post. The adapter now sends only
`body`, retaining its authenticated connection/session headers and the existing
per-room session bindings. It opens no separate connection and has no alternate
send fallback after an uncertain result. See
[thread routing](https://docs.switchagents.ai/switch-rooms/internals/collaboration-bridge#threads-edits-and-deletes).

## Deterministic uploader-only consent

Complete phrases, case-insensitive with terminal punctuation:

- Save: `yes`, `save`, `save it`, `yes please`.
- Decline: `no`, `skip`, `cancel`, `don't save`.
- Choose a filename: `save planning.pdf` or `skip planning.pdf`.

Words embedded in longer sentences do not authorize anything. No LLM infers
consent. The listener and API share the same parser. With this feature enabled,
complete consent commands go to the private document route; a command without a
pending upload is ignored and cannot create an import or fall into AI processing.

Eligibility binds the authenticated sender's exact opaque ID, tenant, room and
bridge. It does not depend on display names, personal registration, member mappings,
a human/agent classification or the thread where the response was typed. Known
self/bot identities remain excluded. Ingest authentication plus the distinct
listener-only adapter credential and configured room/bridge boundaries remain.
A shared proxy ID can only prove ownership by that shared identity, not distinguish
people behind it. Never give adapter credentials to end users.

One complete, unexpired pending request is selected. A complete attachment group
is one request listing its files. Multiple requests prompt a filename choice;
a filename occurring in more than one pending request leaves all unchanged.
The bot explains that these requests must expire before uploading one copy to
resolve duplicate-name ambiguity. No internal reference is required or displayed.
Internal references remain in logs, idempotency keys and protected operator APIs.
Legacy reference commands are accepted only for compatibility with prompts already
delivered before the upgrade; new replies never advertise them.

## Atomic recovery (ATOM)

A local SQLite `BEGIN IMMEDIATE` transaction selects the eligible request,
stores the confirmation message identity/fingerprint and selected request, changes
state, and enqueues the status reply together. An identical confirmation replay
returns its original outcome even after publication or a restart; it cannot
consume another upload. Conflicting reuse of a message identity is rejected.
Ambiguous and no-pending outcomes are also bound durably, so an old `yes` does
not become valid after another upload arrives. Rollback leaves all state unchanged.

The confirmation ledger is bounded at 100000 entries and fails closed at capacity;
request/outbox limits remain. This is a private, persistent single-host SQLite
ledger, with one API worker and one listener required. Do not erase it to retry.
The live-only listener policy is preserved: a newly started listener skips offline
events; transient reconnects resume within that process. Accepted imports and
outbox work recover independently. Incomplete or expired requests cannot be saved.

Imports reuse the existing Supabase RPC transaction for source, version, chunks
and idempotency. Embedding publication remains atomic. A failed replacement leaves
the previously published version available. Success is sent only after all files
are completed, active and current. Queued messages may be superseded by final state.
An uncertain outbound send can repeat a reply; exactly-once chat delivery is not
claimed. Confirmation replay does not create duplicate documents.

## Formats, extraction and limits

Supported: UTF-8 `.md`/`.txt`, text-based `.pdf`, modern `.docx` and `.pptx`.
Original filenames and formats remain in document/version metadata, and Switch
source identities remain in attachment provenance. Only searchable extracted text
is stored; original binary files are not separately archived in Supabase Storage.

PDF uses pinned `pypdf==6.21.4`. DOCX/PPTX use standard-library ZIP and XML parsing,
not new Office dependencies. DOCX body paragraphs/tables and PPTX slide text/tables
are extracted in document/slide order. Headers, notes, chart data, images, OCR,
legacy `.doc`/`.ppt`, and macro-enabled files are outside this implementation.
PDF layout/reading order depends on its text structure.

Actual signatures, XML roots, package content types and relationships are checked.
Password-protected/encrypted, malformed, macro/embedded-object packages, DTD/entity
XML and unsupported formats are rejected. External links are never resolved or
fetched; macros and embedded objects are never executed. Empty/scanned documents
receive a no-searchable-text explanation. An extension/MIME declaration alone is
not accepted as proof of file format.

Limits are enforced across HTTP upload, download and parsing:

| Boundary | Limit |
| --- | --- |
| Original upload/download | 8 MiB hard maximum/default |
| Extracted UTF-8 text | 256 KiB and 262144 characters |
| PDF pages / PPTX slides | 100 / 200 |
| DOCX paragraphs | 10000 (rendered page count is not inferred) |
| ZIP members / total expanded bytes | 1024 / 32 MiB |
| Individual archive member / decoded PDF stream | 4 MiB |
| Parser processes / wall time / memory per process | 2 / 20 seconds / 512 MiB |
| Chunk count | Existing 1024 maximum |
| Files per request | Existing default 4, configurable up to 8 |

Binary parsing runs outside the event loop in disposable subprocesses. Windows
Job Objects / Unix address-space limits enforce the memory bound. Timeouts kill
and reap the parser. Cancellation waits for its bounded completion/timeout before
releasing the process slot. ZIP members are bounded and read only
in memory, never extracted to disk. Resource-limit setup failure rejects parsing.
No download, extraction, content storage or embeddings happen before consent.

`SWITCH_DOCUMENT_MAX_BYTES` may lower the 8 MiB maximum; the same configured limit
applies to HTTP imports and Discord download checks. Existing ignored `.env`
settings were not changed; a previously configured 256 KiB override remains lower
until the operator explicitly updates it. Extracted text keeps the existing
Postgres 256 KiB constraint. Parser docs:
[pypdf extraction](https://pypdf.readthedocs.io/en/stable/user/extract-text.html),
[Python XML security](https://docs.python.org/3/library/xml.html#xml-security).

Citations refer to stored text lines, including extracted-text lines for binary
formats. Search responses explicitly carry `location_basis=stored_text_lines`.
They do **not** claim original PDF pages, DOCX pagination or PowerPoint slide
positions. Original source metadata is retained separately. Document retrieval
through the existing scoped search API remains separate from Discord answer
integration; this revision does not connect document search to ordinary AI replies.

## Applied migration and remaining service rollout

Applied with explicit user approval:
`supabase/migrations/history/20261106095848_document_binary_formats.sql`.
It transactionally expands the existing `documents.source_type` check to
`markdown`, `text`, `pdf`, `docx`, `pptx`. It does not replace the RPC, delete
rows, reset tables, change RLS/grants or alter existing source/version/chunk data.
The earlier versioned-document migration was not edited. The original consolidated
baseline now declares the same formats for fresh installs, as explicitly requested;
never replay that destructive baseline on hosted data.

Schema/dependencies are ready. Before live use, review any lower local upload
override and explicitly authorize reloading/restarting the shared services. Keep one listener
and one API worker. Then approve a disposable Discord fixture test: upload each
format, reply `yes`, verify a channel-only prompt/update/success, scoped search
and a citation, another sender's rejection, filename ambiguity and a decline.
Do not use a legacy failed reference for a new upload. The extraction tests use
non-sensitive fixtures under `backend/tests/fixtures/documents/`.

## Local verification

The local verification report distinguishes real SQLite transactions, actual PDF/
OOXML parser subprocesses, real local ONNX inference, disposable PostgreSQL and
mocked Switch/hosted document repositories. No live Discord/hosted binary ingestion
has been verified. Terminal `document stage=...` logs retain internal references
and fixed failure categories but never file contents, credentials, media handles
or raw parser/remote exception text.


Final verification: **570 backend tests passed** with `RUN_LOCAL_MODEL_TESTS=1`.
This includes a real PDF parser process through local ONNX embeddings, publication
and cited search using mocked Switch and an in-memory document repository; actual
DOCX/PPTX parser processes; real SQLite replay/concurrency/rollback tests; and
existing messaging, commitments and reminders. A missing already-declared local
`tzdata==2026.6` dependency was restored before the final run; no date logic changed.
One pre-existing test-client deprecation warning remains.

Disposable PostgreSQL/PGlite checks passed document contracts, the additive format
migration and all three format/idempotency cases, useful storage and embedding-job
regressions. The schema harness detects documents already present in the consolidated
baseline instead of replaying their earlier creation migration. No applied migration
was edited. `git diff --check` passes. No Git commit or push was made.


## Approved hosted schema/dependency update — 2026-11-06

The user explicitly approved applying the format migration and updating the original
consolidated SQL. Supabase recorded `20261106095848_document_binary_formats.sql`.
Only that additive transaction ran remotely; the destructive original baseline
was never replayed. The original `20261104000100_initial_schema.sql` now declares
the same five source formats for **fresh installations only**, under the user's
explicit exception to migration immutability. The already-applied versioned-document
migration remains unchanged. Never rerun the original baseline on hosted data.

Backend/dev/local-embedding requirements are installed in the existing project
`.venv`; `pip check` reports no broken requirements. PDF parsing is pypdf 6.21.4.
Use that environment to run the backend. The backend-only RPC grants were verified
unchanged. No feature settings, ignored `.env` limits, services or real team documents
were changed, and no live message was sent during this deployment step. Shared
service reload and live binary ingestion verification remain separate steps.
This section supersedes the earlier pending-migration statements above.


## Windows Uvicorn reload parser fix — 2026-11-06

A live DOCX import downloaded successfully but reached a generic worker warning.
The active backend uses Uvicorn `--reload`; its Windows Selector event loop does
not support `asyncio.create_subprocess_exec`. Tests using `asyncio.run` previously
used the Proactor loop and missed that deployment-specific exception.

Binary parsers now launch with `subprocess.run` inside `asyncio.to_thread`. The
same two-process semaphore, 20-second hard timeout, child memory limits and output
limits remain. Timeout kills/reaps the child. Cancellation retains the slot until
the bounded worker has finished, preventing orphaned work or concurrency overruns;
a cancelled import does not proceed to storage. No new dependency or schema change
is required. A real DOCX parser regression runs explicitly under the Windows
Selector loop. Unexpected worker diagnostics now include only the exception class,
never raw exception contents or tracebacks.

An existing in-flight request can recover when its five-minute lease expires;
confirmation and stable import identity are retained. Never delete the journal
or create another listener to retry it.


Verification for the Windows fix: **571 backend tests passed** with actual local
inference enabled, including the Windows Selector-loop parser regression. The
user-confirmed live DOCX request `78c12dc332b4` recovered automatically on attempt 2,
reached `published`, and all four request outbox entries were marked sent. The
journal/confirmation was not reset and no upload/consent was fabricated. This
provides a real hosted DOCX publication result beyond mocked parser tests; it does
not establish live PDF or PPTX verification. Only publication metadata was checked,
not the user's document text. No migration or feature-setting change was needed.
