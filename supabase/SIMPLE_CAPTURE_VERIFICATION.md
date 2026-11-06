# Simple commitment capture verification — 2026-11-06

Status: safe local implementation complete. No hosted migration, real team task mutation, live AI/Switch message, push or merge was executed. Public model assets were downloaded/checksum-verified locally. See [current workflow and deployment instructions](../SIMPLE_COMMITMENT_CAPTURE.md).

| Executed check | Result | What was real |
| --- | --- | --- |
| Final backend `pytest -q --tb=short` | **380 passed, 1 skipped**, existing Starlette/httpx deprecation warning | Includes ingestion, extraction, protected endpoints, lifecycle/history, reminders, viewer and embedding regressions |
| Capture-specific tests before final expansion | **19 passed** | Mocked AI/HTTP/Switch; real disposable SQLite journaling; real PostgreSQL/PGlite capture/embedding RPCs as service_role; one real ONNX worker/search integration |
| Explicit `RUN_LOCAL_MODEL_TESTS=1` local embedding suite | **13 passed** | Includes the opt-in actual pinned ONNX model test; others cover deterministic inference/repository failure behavior |
| Supplemental post-write rollback assertion | **1 passed** | Real SQL failure after the delegated storage RPC and owner initialization rolls back source/task/memory |
| `npm run test:reminders` | **Passed** | Existing disposable PostgreSQL lifecycle/chat/reminder fixtures and catalog review; previous modules remain intact |
| `pip check` | **No broken requirements** | Existing optional runtime/tokenizer installed, using the Python 3.14-compatible ONNX pin |
| `git diff --check` | **Passed** | No whitespace errors; model assets and all local .env files are ignored |

The full suite's skipped test is the opt-in standalone model test, which was also run explicitly and passed. The capture integration independently ran real inference because verified weights/runtime were installed. No skip is being counted as hosted/model success.

## Directly observed results

- Previously unknown sender, no room_members rows/mappings, no classification or adapter-attestation credential: a tagged personal promise saves through the normal authenticated ingestion path with the advanced lifecycle feature disabled.
- Name/alias/native mention forms are accepted through existing Switch addressing; unmentioned input, addressed follow-ups without a mention, greetings/questions, tentative/quoted statements and a named third-party owner do not create tasks.
- Owner is the original delivered sender ID; AI-selected owner labels are overridden. Source body/IDs/thread/time, structured description/deadline and linked summary are preserved. Deadline wording stays in the summary; unresolved “Friday” leaves due_at null.
- The source, one task and one linked memory commit atomically. Human approval remains pending, no manager/reviewer is fabricated, and the summary records explicit sender acceptance.
- SQL replay yields the same counts without duplicate rows/jobs; source spoofing fails. A lost database acknowledgement followed by a new worker/recovery reuses the saved extraction, leaving exactly one source/task/memory and no repeated AI call.
- Failed storage sends no success confirmation. Successful storage replies that search indexing is pending.
- The actual existing embedding worker records a simulated inference failure without removing the task. A retry of that same memory uses the actual pinned gte-small model and stores a completed, hash-matching 384-dimensional vector.
- The existing scoped semantic retrieval path, with a real paraphrased query vector, ranks the report above an embedded food distractor and preserves source references. A cross-tenant SQL search returns no records.

These are real local inference/database results behind a **mocked HTTP transport**, not hosted PostgREST. Switch posting and AI extraction were mocked; the private runtime's interpretation quality and real bridge addressing remain unverified. PGlite uses one connection, so this does not prove multi-instance contention safety.

## Schema and deployment boundary

New additive `migrations/history/20261106000100_simple_commitment_capture.sql` adds a five-argument overload of existing `store_message_extraction`, required because the old four-argument function does not initialize stable owner columns. It delegates to that existing function and initializes ownership in the same transaction. No table, column, embedding dimension, model or infrastructure was added. Anonymous/authenticated client grants are revoked. The migration was tested only in fresh disposable local databases; it was **not applied remotely**.

Simple capture requires the reviewed storage/embedding prerequisites and this overload, not the advanced lifecycle/chat/reminder migrations. Existing local work in those modules was preserved. Prior applied migration history was not rewritten for this task; never replay the destructive initial schema on the host.

Remaining: obtain approval for reviewed hosted application; verify exact functions/grants/columns and disposable hosted source/task/memory/vector/search with exact cleanup; configure actual host dependencies/assets, ingest/Switch/Supabase credentials, tenant/rooms, optional confirmed COMMITMENT_TIMEZONE and LOCAL_EMBEDDINGS_ENABLED; authorize a separate live Switch verification. The read-only cloud checker now distinguishes capture-overload exposure; it was not used to claim hosted execution here.
