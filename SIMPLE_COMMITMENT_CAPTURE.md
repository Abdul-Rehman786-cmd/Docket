# Tagged commitment capture

Current creation policy, 2026-11-06:

```text
Tag bot → private AI extraction → atomic Supabase source/task/summary storage
                                             ↓
                                  existing durable embedding job
                                             ↓
                              existing gte-small worker → scoped search
```

Example: `@<registered-name> I’ll finish the report by Friday.` saves a task owned by the delivered sender ID. Its source and linked summary retain “Friday”; `due_at` is null unless an exact instant is clear. The reply is **“Task saved in Supabase. Search indexing is pending.”** It is sent only after Supabase acknowledges storage. This does not claim that a vector is already stored.

## What is required

The existing listener/API ingest credential, configured tenant and allowed room, enabled Supabase room mapping, existing non-AI Switch credentials/private AI runtime, and storage/embedding functions are required. Privileged Supabase credentials stay on the backend. The existing protected `/messages` endpoint and room boundaries remain intact; no new public endpoint exists.

Creation does **not** require a member record, membership query, user registration/linking/invitation, role, sender mapping, separate adapter attestation, positive human/non-bot classification, lifecycle feature flag or reminder configuration. The source sender is used directly as `owner_switch_user_id`; display names only label that owner. Someone named in the text is never selected as assignee. Exact known-self IDs and existing reply fingerprints still suppress application echoes.

## Addressing and extraction

Switch already computes `payload.addressed`; the listener forwards it. Capture requires that flag and an explicit mention at the start of a clear personal promise. It accepts the existing Switch name/alias form and native Discord `<@id>` / `<@!id>` syntax. No registration-name lookup or new mention-verification service is used by creation. An addressed follow-up without a fresh mention cannot create a task. A plain name substring without supported addressing metadata is insufficient.

Supported initial English grammar is a single `I’ll`, `I will` or `I commit to` promise. The existing private AI must extract a commitment; deterministic normalization takes the description from the source statement and the owner from source metadata. Greetings, questions, tentative/hypothetical/quoted statements and unmentioned commitments cannot create tasks, even if the AI incorrectly proposes one. Existing structured create proposals are normalized into the same extraction path; the worker no longer calls the advanced lifecycle creation RPC for these statements. Facts, decisions and unrelated questions retain their existing processing. Commitments bypass the older draft/yes confirmation flow.

Original deadline wording is preserved in source evidence and `Deadline wording:` in the searchable summary. `COMMITMENT_TIMEZONE` is an optional **confirmed** IANA timezone shared by configured rooms. Leave it blank if that assumption is invalid. An original timestamp plus a clear weekday and explicit time can resolve an instant; absolute aware timestamps are also supported. Missing timezone/time/original timestamp, uncertain dates and DST gaps/folds leave the exact deadline null while still saving the clear promise. The AI's guessed deadline is not accepted. No default time or timezone is invented.

The summary records `Acceptance: explicit sender statement`. Human approval stays pending, with no fabricated manager/reviewer or approval timestamp. Neither saving nor semantic search requires a separate approval command. This path does not assert advanced lifecycle/reminder eligibility; those services and their existing policies remain separately controlled.

## Atomic storage and retry

The existing `store_ai_memory` / `storage_items` / `persist_extraction` services and durable ingestion journal remain in use. One required `p_capture=true` argument selects a five-argument overload of **the same** `store_message_extraction` RPC. The overload calls the working four-argument function and initializes existing stable owner columns in the same SQL transaction. Source, commitment, linked summary and pending embedding state commit together or roll back. The existing four-argument fact/decision/legacy extraction path is unchanged.

The additive migration is `supabase/migrations/20261106000100_simple_commitment_capture.sql`. It adds only a backend-only function overload, no table, column, embedding model or infrastructure. A function change is necessary because the older storage RPC ignores stable owner IDs. A missing overload causes storage failure; the backend never silently falls back to owner-less success. Anonymous/authenticated client execution is revoked. Existing table constraints, owner columns, scope/source locks and unique keys are reused. No migration history was rewritten for this simplification.

Stable `capture-` extraction keys and the existing source lock prevent duplicate tasks/summaries/jobs. A changed sender/body/deadline conflicts rather than claiming or rewriting old evidence. Replays do not overwrite later ownership changes/history. A lost acknowledgement can retry the saved extraction after restart without another AI call; the original source/task/memory are returned. The API's received receipt confirms journal acceptance, not cloud storage.

## Embeddings and search

The linked `memories` row is the durable job. Existing claim/lease/hash/retry machinery handles it. `Supabase/gte-small`, pinned revision `93b36ff09519291b77d6000d2e86bd8565378086`, masked-mean/L2 recipe and **384 dimensions** remain unchanged. No embedding is stored in commitments or a new table. The CPU worker generates a vector and persists it through `finish_memory_embedding`; only completed, recipe/hash-matching active vectors enter scoped `search_memories` retrieval. Failure leaves the saved task intact and retries the same memory within existing limits. Three exhausted attempts remain available for reviewed recovery; they are not called searchable. Keyword fallback remains explicitly labelled.

The existing optional dependency file now pins ONNX Runtime 1.24.1 for Python 3.14 because 1.23.2 has no matching wheel; older Python retains 1.23.2. This is the same runtime dependency/model, not a provider migration. Public weights were downloaded and checksum-verified locally for testing, under ignored `.local` storage.

## Local verification

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-embeddings.txt
.\.venv\Scripts\python.exe -m app.embedding_setup
.\.venv\Scripts\python.exe -m pytest tests/test_simple_capture.py tests/test_capture_database.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

The capture database tests require the repository's existing Node/PGlite development dependencies in `supabase/tests` (`npm ci` if absent). They create fresh disposable PostgreSQL databases, never a hosted reset. They run actual storage/embedding/search SQL as `service_role`, the actual existing embedding worker and real pinned ONNX inference, with mocked AI/HTTP/Switch. A paraphrased report query must rank the report above a food distractor. Tests also check rollback, source/owner/summary consistency, duplicate replay, source spoofing, embedding failure/retry, scopes and restart after committed storage loses its acknowledgement. This proves local integration, not hosted PostgREST or real chat delivery.

For the existing optional model-specific test, set `RUN_LOCAL_MODEL_TESTS=1` and run `tests/test_local_embeddings.py`. The ordinary full suite skips that opt-in test; the capture database integration separately runs real inference when installed assets/dependencies are present. Test reports must state which executed versus skipped.

## Remaining deployment steps

1. Review the actual hosted storage/embedding definitions, columns, triggers, scope mappings and grants. Earlier metadata exposed storage/embedding support, but is not proof of current complete definitions. Review/apply the **additive capture overload** only after approval. Do not replay the destructive initial schema or earlier applied lifecycle migrations. Advanced chat/lifecycle/reminder migrations are not prerequisites for capture.
2. Refresh hosted schema discovery as required and verify `store_message_extraction(uuid,text,jsonb,jsonb,boolean)` is available only to the backend. `python -m app.verify_cloud_storage --check` is read-only and reports capture-overload exposure separately; OpenAPI does not verify exact SQL/grants.
3. Keep backend Supabase URL/service secret, Switch ingest credential, existing non-AI requester/listener identity, private AI room/runtime and tenant/allowed-room configuration in ignored `.env` / approved host secrets. Set a confirmed `COMMITMENT_TIMEZONE` only when known. Enable `LOCAL_EMBEDDINGS_ENABLED=true` and install the optional requirements/assets on the actual API host. Retain one API worker with persistent private `.local` journal storage.
4. Enable `SWITCH_REPLY_TO_DISCORD=true` if confirmations are wanted. Creation does not require `AUTOMATIC_COMMITMENTS_ENABLED`, `SWITCH_ADAPTER_TOKEN`, room member mappings or reminder flags. Existing advanced features keep their separate controls and authorization rules.
5. Obtain authorization before running `python -m app.verify_cloud_storage --run-approved-fixture`; it performs real disposable cloud writes/cleanup with mocked AI and no external chat. Hosted vector persistence/search needs its own approved disposable verification. Never use real team tasks.
6. Obtain separate authorization before sending real Switch messages. Verify actual mention normalization, extraction, durable source/owner/summary/vector and a scoped paraphrase query. No hosted or live Switch success is claimed from the local tests.

Current source includes legacy lifecycle/history/reminder modules unchanged in purpose. They are not removed, silently enabled or automatically provisioned by this simpler creation flow.
