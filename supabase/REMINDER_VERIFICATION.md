# Reminder verification and hosted status — 2026-11-05

## Actual hosted read-only findings

Executed `python -m app.commitment_deployment_check` against the configured hosted PostgREST endpoint. The sandboxed request could not connect; the approved network-enabled rerun succeeded. Only `GET /rest/v1/` metadata was requested. No private rows, credentials, project URL, DDL, fixture writes or Switch messages were printed/sent.

- Backend-visible functions present: atomic extraction storage and embedding claim/finish functions.
- Memory hash/status/claim/lease columns present, including `content_hash`, `embedding_content_hash`, `embedding_status`, `embedding_claim_token`, `embedding_lease_until`.
- Commitment lifecycle/chat functions not exposed. Required commitment version/cancellation/automated-acceptance columns and chat policy/identity metadata absent.
- Reminder RPCs and occurrence/version/send-start columns not exposed.
- Local flags: automatic commitments false, adapter credential absent, non-AI identity configured, tenant configured, two configured rooms, reminder planning false and delivery false.

The host is not ready for the implemented commitment/reminder workflow through the exposed API. This supersedes historical reports that storage/embedding functions were still absent. A stale schema cache cannot be excluded by OpenAPI alone; exact definitions, constraints, client grants, RLS and migration history remain unverified. `reminder_deployment_review.sql` is the prepared read-only catalog check; it was not executed on hosted Supabase. No hosted lifecycle mutation or disposable hosted fixture verification is claimed.

## Executed local checks

| Check | Result / boundary |
| --- | --- |
| Full backend `pytest -q` | **304 passed, 1 skipped**; existing Starlette/httpx deprecation warning |
| Reminder and transport suites | **46 tests** included in the full run; fake domain repositories/preferences/sink, controllable clock, real local journal, mocked HTTP |
| `npm run test:reminders` | **Passed**, actual disposable PostgreSQL/PGlite + pgvector |
| Application import | Passed |
| Python compilation / `pip check` | Passed; no broken requirements |
| `git diff --check` | Passed |

No test waited for a real deadline. The optional real embedding-model test remains skipped; the reminder code makes no routine LLM call. Existing ingestion, extraction, embeddings, automatic chat updates, manual lifecycle and viewer tests pass. No runtime dependency or paid scheduler was introduced.

The SQL runner creates an in-memory database, applies core/lifecycle/chat/reminder migrations and executes prior manual/chat fixtures plus reminder assertions. Fixtures roll back. It verifies:

- Stable occurrence uniqueness, no second claim while one is leased, exact tenant/room scoping and stale-token refusal.
- Planning/claiming does not update successful-send state.
- Confirmed-send record, message reference, attempt count, timestamp and commitment bookkeeping update atomically without changing lifecycle version.
- Transient retry delay, no early retry, unstarted lease recovery with token rotation and started lease quarantine after restart.
- Late acknowledgement for a quarantined attempt; a forced failure during commitment bookkeeping rolls back the outbox's sent update too.
- Null-token acknowledgement is refused; a genuine retained-token acknowledgement survives later invalidation. The catalog-review script also executes successfully on the disposable schema.
- Completion invalidates an outstanding claim; old versions cannot begin a send.
- Policy changes increment versions, and client roles cannot execute worker functions.
- A second task in the same room is deferred by room spacing without consuming an external attempt.
- Original evidence remains unchanged; previous manual/chat lifecycle fixtures pass with the reminder triggers installed.

These SQL assertions are real local database execution. They are not hosted PostgREST execution or genuine multi-connection contention tests. Python tests exercise concurrent fake claims and overlapping scheduler ticks. The supported deployment remains a single application worker; multi-instance safety is not asserted.

## Changes and review package

- Additive migration: `migrations/20261105000300_reminder_outbox.sql`; extends the existing room-policy and reminder-delivery tables with versioned preferences, occurrence identities, send-start state, invalidation hooks and backend-only worker RPCs.
- Models/ports: `models/reminders.py`, `repositories/reminder_contract.py`.
- Services: pure planner, bounded scheduler, reusable dispatcher hooks, strict Switch notification sink and application worker.
- Persistence: `repositories/reminder_repository.py`, using existing backend auth/pools and safe errors.
- Runtime integration: `main.py`, configuration and placeholder-only `.env.example`; default flags false.
- Viewer reminder fields are read-only and no longer persisted as browser-local authority. Environment APIs stay disabled; no public preferences mutation endpoint was introduced.
- Hosted probe: `app/commitment_deployment_check.py`; catalog review: `supabase/reminder_deployment_review.sql`.
- Contracts/operations: [REMINDERS.md](../REMINDERS.md).

Defaults: reminders off; lead 30 minutes; overdue cadence 24 hours; 3 overdue follow-ups per task/policy cycle; minimum task gap 30 minutes; room gap 60 seconds; quiet 22:00–08:00; confirmed IANA timezone required; original room/thread routing; no mentions. Unknown external outcomes are quarantined, not blindly retried. `sent` confirms a Switch acknowledgement, not Discord delivery or human receipt.

## Remaining approvals and blockers

1. Review hosted definitions/grants and legacy reminder rows. Approve the missing additive lifecycle, chat and reminder migrations before any remote schema changes. Never apply the destructive core reset or casually reconcile migration history.
2. Configure reviewed room platform/membership, policy, timezone and identity mappings. No real policies/mappings were overwritten. Public human preference authentication remains unfinished; use trusted server administration.
3. After deployment approval, run disposable hosted fixtures and exact-scope cleanup, including actual worker function discovery, claims and recovery.
4. Separately authorize and verify one disposable external reminder through the actual non-AI Switch identity and original bridge/thread. No such delivery was authorized or attempted here.

Real Switch delivery, downstream Discord delivery, automatic ambiguous-message reconciliation and multi-instance operation remain unverified/unsupported. No exactly-once external delivery is promised. A task can change between the final database recheck and an external post; in-flight acknowledgements preserve that outcome rather than rewriting history.
