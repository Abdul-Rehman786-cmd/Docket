# Commitment lifecycle verification — 2026-11-05

Status: implemented locally; hosted lifecycle execution and real human authentication remain unverified. No hosted SQL, real task mutations or external messages were sent.

## Executed checks

| Check | Result and boundary |
| --- | --- |
| Backend regression: `.venv/Scripts/python.exe -m pytest -q` | 222 passed, 1 skipped. Existing Starlette/httpx deprecation warning. This run preceded one additional race test; the subsequent targeted run includes that test. |
| Targeted lifecycle/transport tests | 22 passed. In-memory repository + controllable clock, simulated identity dependencies and mocked PostgREST transports. No cloud persistence or real authentication. |
| `npm run test:commitments` | Passed on disposable local PostgreSQL/PGlite + pgvector, with actual migration and RPC SQL. No hosted PostgREST. |
| `npm test` | Passed: existing database audit (33 groups, including 66 client-access denials), atomic extraction storage and embedding jobs, on both fresh creation and local reset passes. |
| Python compilation and `git diff --check` | Passed. |

The initial backend regression run failed two embedding tests because NumPy was absent. Installed the repository's existing pinned NumPy 2.3.4 in the ignored virtual environment; no dependency requirements changed. Pinned disposable SQL test dependencies were installed locally and a public package lock added. No model weights were downloaded for lifecycle tests. The existing optional real embedding-model test remains skipped.

The initial schema audit expected only SQLSTATE 23503 for source deletion. PostgreSQL's `ON DELETE RESTRICT` also raises 23001; the assertion now accepts either constraint error and still fails if deletion succeeds. No schema deletion rule was changed.

## Coverage

- Every lifecycle action, direct completion of approved work, reviewers/timestamps, equivalent no-ops and terminal/rejected conflicts.
- Scoped member reads/history, management roles, assigned-worker permissions, cross-room/tenant denial, invalid assignee and no client actor/role trust.
- Required reschedule field, explicit null, aware timestamps and UTC normalization; pending/rejected work cannot be completed.
- Replay, different-content key conflicts, stale versions, and two fake preflights racing before transaction revalidation.
- Accurate before/after audit, unchanged source evidence, deterministic linked summaries and reminder candidate eligibility.
- Real SQL receipt-insertion failure after commitment/memory/audit writes: all changes roll back.
- Real SQL stale embedding claim/lease invalidation and preservation of inactive historical summaries.
- Backend-role RPC execution and backend/client grants, including append-only event/receipt privileges.
- Existing ingestion/extraction/viewer/retrieval tests continue to pass.

PGlite uses a single local connection; it does not prove hosted concurrent lock contention, PostgREST schema discovery, deployment compatibility with an unknown hosted schema, or actual human-session mapping. Those remain deployment checks. The database uses scoped advisory locks, row locks and version checks; service concurrency tests exercise the repository transaction contract with a locked fake.

## Deployment dependencies / next work

1. Review the additive migration and current hosted definitions. The earlier storage reports identify missing atomic storage and embedding recovery upgrades; those reports are historical observations, not a new live inspection in this task.
2. Obtain approval before applying reviewed non-destructive prerequisites and the lifecycle migration. Never run the core reset remotely. The lifecycle migration checks recovery columns and the enabled invalidation trigger; review the actual trigger definition as part of deployment.
3. Integrate a verified individual-user identity provider with the actor port and stable room-member mapping. All new routes currently fail closed with 503; existing admin and ingest credentials cannot impersonate members.
4. After approved deployment, verify hosted RPC discovery, current membership/role locking, concurrent stale writes and disposable exact-scope fixture cleanup. Do not touch real tasks or send Discord messages without authorization.
5. A future reminder planner can consume structured result/events, but must reread current version, membership and eligibility before delivery. Scheduling/notifications remain outside this task.

See [COMMITMENT_MANAGEMENT.md](../COMMITMENT_MANAGEMENT.md) for API, transitions, identity/repository seams and deployment review details.
