# Commitment management contract

Implementation date: 2026-11-05. Local implementation and verification; no hosted deployment or individual-user authentication is claimed.

## Existing components reused

The later [automatic chat integration](AUTOMATIC_COMMITMENTS.md) adds an internal worker contract with trusted adapter/sender mapping, owner/admin chat policies and separately recorded automated owner acceptance. Rules below continue to describe the public/manual API. Its human-authentication dependency remains closed; chat execution does not unlock those routes. The separate chat migration has not been applied remotely.


The schema already separates approval from task progress and stores reviewer, approval time, completion time, stable Switch assignee ID, deadline and reminder bookkeeping. Existing `room_members` roles are `member`, `approver` and `admin`. Rooms have internal UUIDs, tenant ownership, an opaque Switch ID and an enabled flag. Commitments retain a composite foreign key to their source message. Extraction continues to create pending, not-started records; it cannot approve work.

The existing admin-only `GET /api/v1/commitments` remains diagnostic. Ingest/admin tokens authorize server operations and do not identify people. Switch listener, private Codex extraction, SQLite ingestion journal, viewer and AI orchestration are preserved. No bot, LLM mutation step, scheduler or notification transport was added.

## Independent module boundaries

| Layer | Contract / implementation |
| --- | --- |
| HTTP | `backend/app/controllers/commitments.py`: explicit request types, UUID scopes, bounded pagination |
| Domain models | `backend/app/models/commitments.py`: states, actions, actor, scope, events and results |
| Business rules | `backend/app/services/commitment_service.py`: authorization policy, transitions, reminder eligibility and deterministic summaries |
| Storage port | `backend/app/repositories/commitment_contract.py`: context, list, history and atomic apply operation |
| Supabase adapter | `backend/app/repositories/commitment_repository.py`: backend-only RPC transport, safe errors, response scope checks |
| Identity port | `backend/app/core/commitment_identity.py`: `ActorResolver.resolve(Request) -> Actor` and FastAPI dependency |
| Transaction | `supabase/migrations/20261105000100_commitment_lifecycle.sql`: locked authorization, transition checks, linked memory, event and receipt |

An authentication teammate can implement the identity port independently. It must verify a session, map its immutable subject to `room_members.switch_user_id`, and derive the actor without accepting client actor/role/tenant claims. Room membership and role are loaded from storage. A storage teammate can implement the repository port with the same atomic semantics. The fake repository in tests is an example for development, not deployment persistence. This task does not modify the SQLite ingestion module.

**All new routes return 503 until a trusted individual-user resolver is integrated.** Shared tokens, role headers and actor headers do not unlock them. Dependency overrides simulate trusted identity only in tests. Future authentication should return 401 for absent/invalid sessions; authenticated users outside a scope receive 404. No admin-token impersonation or development bypass exists. No frontend setting enables these routes.

## HTTP contract

Base: `/api/v1/tenants/{tenant_id}/rooms/{room_id}/commitments`. Scope identifiers are internal UUIDs, not Switch room names. The URL requests a resource; it never proves membership.

| Method and suffix | Purpose |
| --- | --- |
| GET base | Scoped list |
| GET `/{commitment_id}` | Scoped detail with reminder eligibility |
| GET `/{commitment_id}/history` | Scoped chronological action history |
| POST `/{commitment_id}/approve` | Approve |
| POST `/{commitment_id}/reject` | Reject |
| POST `/{commitment_id}/start` | Start |
| POST `/{commitment_id}/complete` | Complete |
| POST `/{commitment_id}/cancel` | Cancel |
| POST `/{commitment_id}/reassign` | Assign a stable room-member ID |
| POST `/{commitment_id}/reschedule` | Set or explicitly clear a deadline |

Every action requires `idempotency_key` (UUID) and `expected_version` (positive integer), plus optional `reason` (maximum 1,000 characters). Existing credential redaction applies before reasons are persisted or returned. Unknown body fields are rejected. No arbitrary-column update endpoint exists. Reassign additionally requires `assignee_id`; reschedule requires `due_at`, either an aware timestamp such as `2026-12-01T17:00:00+05:00` or explicit `null`. Omitting `due_at` fails validation. Deadlines normalize to an instant in UTC; no timezone or deadline is inferred. Display names cannot be assigned. Assignees must currently exist in the same tenant and room.

List filters: `approval_status`, `task_status`, `assignee_id`, inclusive `due_from` / `due_to`. A due range excludes undated records. List/history accept `limit` 1–100 (default 25) and `offset` 0–10,000. List order is descending creation time then ID; history is ascending event sequence. Pages include `items` and nullable `next_offset`. Offset pages are bounded but are not a snapshot across concurrent changes. Detail returns `commitment` and `reminder`. Actions additionally return `event` and `changed`.

| Status | Meaning |
| --- | --- |
| 401 | Future identity provider rejects a missing/invalid human session |
| 403 | Current scoped member lacks action permission (`forbidden_action`) |
| 404 | Missing/inaccessible scope, disabled room or record (`not_found`) |
| 409 | `stale_version`, `invalid_transition`, or `idempotency_conflict` |
| 422 | Invalid HTTP payload/filter; storage may return `invalid_assignee` / `invalid_action_payload` |
| 503 | Human authentication unavailable, missing/unavailable storage RPC, or invalid storage result |

Errors never echo body values, SQL diagnostics, URLs or credentials. Responses use `Cache-Control: no-store`.

## Policy and transitions

Every action requires a current member of an enabled room in the correct tenant. Approvers/admins can approve, reject, cancel, reassign and reschedule. Only the current assignee can start or complete approved work; admins do not implicitly complete another person's task. Any current room member can read records and history in that room. These are existing role names, not client-provided authority.

| Action | Permitted source state | Result |
| --- | --- | --- |
| Approve | Pending + not_started | Approved; trusted reviewer and approval timestamp |
| Reject | Pending + not_started | Rejected; reviewer/time and optional reason in event |
| Start | Approved + not_started | Approved + in_progress |
| Complete | Approved + not_started or in_progress | Approved + completed; completion timestamp |
| Cancel | Pending or approved + not_started or in_progress | Same approval + cancelled; cancellation time and actor/reason in event |
| Reassign | Pending or approved + not_started or in_progress | Stable member ID changes; unresolved display name cleared |
| Reschedule | Pending or approved + not_started or in_progress | Deadline set or cleared |

Rejected records permit only equivalent reject retries. Completed/cancelled records permit only equivalent complete/cancel retries, respectively. Rejected approval is never reversed; terminal work is never reassigned, rescheduled or reopened. Reopening is a future explicit action. Equivalent actions on eligible states are no-ops with the current version. Completion/cancellation/reviewer timestamps remain unchanged on no-ops.

## Atomicity, concurrency and history

The service performs a read-only preflight. That read is **not** part of the write transaction. The adapter then makes one `apply_commitment_action` RPC. PostgreSQL obtains a transaction-scoped advisory lock for `(tenant, room, actor, idempotency key)`, locks the enabled room and actor membership for sharing, and locks the scoped commitment for update. It revalidates permissions, key content, expected version and transition under those locks; reassignment also locks the target membership. Concurrent removal of a checked membership waits for the transaction. The database uses its own transaction timestamp, not a client clock.

In the same transaction a change updates state, increments `version`, clears `next_reminder_at`, reconciles the linked memory, appends an event and records the original result. Any failure rolls everything back. A future planner must recalculate next delivery times after changes. `last_chased_at` remains historical.

Keys are scoped to tenant/room/actor and bind commitment ID, action, expected version and normalized payload. Same key/content returns the saved result without another event/update even if the original version is now stale. Permissions are rechecked before replay. Different content under the same key conflicts. A fresh key with a stale version conflicts, including an equivalent action. A fresh equivalent action with the current version records a `changed=false` event/receipt without incrementing the version. Event sequences may have gaps after rollback.

Events retain commitment/tenant/room, trusted actor, action, database time, expected version, idempotency key, reason and before/after lifecycle values. Actors remain recoverable after membership removal. Backend grants on event/receipt tables permit select/insert only; client roles have neither table nor function privileges. The service credential is privileged infrastructure, not human authorization. No API edits events or deletes receipts. Receipts currently have no expiry; pruning requires a separately defined retry window.

## Search and reminder integration

Summaries use the existing `(tenant_id, room_id, message_id, memory_type='commitment', extraction_key)` relationship. Extraction and lifecycle writes generate explicit assignee, approval, task and UTC deadline lines deterministically. Source text, external message links, extraction key and original unresolved external assignee evidence remain intact.

Text changes invoke the existing embedding trigger: vector and in-flight claim are invalidated; content hash changes; the embedding worker recovers active pending summaries. Rejected/completed/cancelled summaries remain stored with explicit historical states but become inactive in ordinary semantic retrieval. History/detail remains accessible. Pending active summaries explicitly say `Approval: pending`. Migration backfill repairs existing linked summaries and may queue substantial re-embedding work.

`reminder` is a structured **candidate contract**, not a scheduled notification. `eligible=true` requires approved, nonterminal work, a stable assignee ID and a deadline. Reasons include `pending`, `rejected`, `completed`, `cancelled`, `unassigned`, `no_deadline` and `eligible`. A future planner consumes result/event, invalidates its cached version, then rereads current state, membership and policy immediately before planning/delivery. Replayed results are historical and cannot authorize delivery. Missing members suppress delivery. No scheduler, reminder endpoint or notification delivery is implemented.

## Deployment review

The additive migration adds `version`, `cancelled_at`, two tables, indexes and backend-only functions. Existing approval/task/reviewer/assignee/deadline columns and constraints are reused. Assignee validation is transactional rather than a new FK that could block unresolved extraction or invalidate legacy rows. Existing cancelled rows retain unknown cancellation times; no historical actors/times are fabricated. Scope constraints and composite FKs bind audit/receipts to commitments. Review pre-existing inconsistent lifecycle rows before enabling actions; no data is silently approved or reopened.

Do not run destructive `20261104000100_initial_schema.sql` on hosted Supabase. Follow `supabase/DEPLOYMENT_PROPOSAL.md`: compare hosted definitions, approve/apply non-destructive storage/embedding prerequisites first, then review/apply this additive migration once. The prerequisite is the full embedding recovery schema and trigger, not merely an older vector-clearing trigger. Deployment tooling must record it once; it is not a replayable reset. **No remote SQL was applied in this task.**

Before approval, review scope mappings, membership data, inconsistent states, backfill/re-embedding cost and grants. After approved deployment, refresh PostgREST schema discovery and run disposable exact-scope fixtures with dependency-ordered cleanup/rollback. Never modify real tasks or send Discord messages. Enabling routes additionally requires reviewed trusted human authentication. Until both dependencies are satisfied, the module is locally verified with fail-closed routes.

## Verification commands and limits

```powershell
cd D:\Hackathon\team-memory\backend
.\.venv\Scripts\python.exe -m pytest tests/test_commitment_lifecycle.py tests/test_commitment_transport.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd ..\supabase\tests
npm ci --ignore-scripts
npm run test:commitments
```

Domain tests use a fake repository and controllable clock. HTTP tests override identity; transport tests mock PostgREST. The Node runner uses pinned PGlite PostgreSQL + pgvector, creates a fresh disposable local database, applies the baseline and additive migration, and rolls fixtures back. It tests actual SQL functions, backend-role execution, permissions, stale versions, idempotency, source retention, embedding lease invalidation and rollback after receipt insertion failure. It does not test hosted PostgREST or real multi-connection lock contention; concurrent service tests use the locked fake. Hosted lifecycle actions remain unverified. See `supabase/COMMITMENT_VERIFICATION.md` for executed results.
