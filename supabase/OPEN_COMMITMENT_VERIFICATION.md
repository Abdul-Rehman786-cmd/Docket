# Open commitment verification — 2026-11-06

Status: implemented and tested locally. No remote DDL, hosted task mutation, real model execution, external message, push or merge was performed. See [the current contract and exact activation steps](../OPEN_COMMITMENTS.md).

## Executed checks

| Check | Actual result | Boundary |
| --- | --- | --- |
| Final backend full `pytest -q --tb=short` | 349 passed, 1 skipped; existing Starlette/httpx deprecation warning | Fake repositories, mocked AI/transports, disposable SQLite recovery; includes all final changes and ingestion, extraction, commitment, reminder, embedding and viewer regressions |
| Focused open/listener/logging regression | 51 passed | Owner-acceptance planning and preservation of ordinary ingestion when optional metadata is malformed |
| Focused open/useful-storage/chat/AI regression | 125 passed | Includes sender-only task reads even when the mocked AI proposes room-wide retrieval, and denial when human identity proof is missing |
| `npm run test:chat` | Passed | Disposable PostgreSQL/PGlite executes revised migration and legacy manual/chat assertions |
| `npm run test:reminders` | Passed, including new open fixtures | All four local migrations; actual SQL creation, idempotent receipts, ownership denial, stale versions, rollback, source/audit/memory state, accepted-owner eligibility and queued reminder supersession |
| Hosted metadata probe, before migration editing and repeated with new fields | Storage/embedding APIs present; lifecycle/chat/create/reminder footprint absent | Read-only service-role PostgREST discovery, not migration history or SQL-body/grant verification |
| Real hosted commitment actions / actual Switch delivery | Not executed | Approval/authorization still required |

The optional real embedding-model test was skipped; no model assets were installed. PGlite executes actual SQL but uses one connection; locked fake race tests and sequential SQL claims do not establish hosted multi-connection contention or multi-instance safety.

## Coverage

Unknown human sender creation requires no member/mapping fixture. Genuine registration mention and authenticated sender metadata are required; unmentioned messages cannot create/update commitments. Tests cover start, completion, cancellation, rescheduling/clearing, owner-only reads, spoofed actor/owner/mention metadata, other-owner denial, ambiguity, unresolved deadlines, denied handoffs, rollback, concurrent replay, stale versions and restart after a lost creation acknowledgement. Existing protected HTTP-route tests remain in the passing regression suite. Facts/decisions still store when commitment extraction is removed.

Assertions examine resulting commitments and explicit acceptance (human approval stays pending), immutable evidence, create/update audit, receipt deduplication and linked memory. Disposable reminder SQL verifies that an explicitly accepted open task is eligible without manual identity setup, and cancellation supersedes its pending delivery before it can be claimed.

## Deployment findings and limits

The only changed schema migration is `migrations/20261105000200_commitment_chat.sql`, revised locally under explicit user authorization. No SQL file was added. Existing SQL fixtures were extended. The file includes source-backed owner acceptance, create audit/RPC, automatic nonprivileged participation projection and owner-only open preparation/action rules, while retaining the legacy mapped internal path.

Hosted metadata lacks the new creation RPC and both owner-acceptance columns as well as prior lifecycle/chat/reminder APIs. The global automatic/reminder flags are disabled; the distinct adapter secret is not configured. Tenant and two allowed rooms/non-AI credentials are configured (values were never printed). Enabled room-policy rows and timezone still need operator review after approved reconciliation.

Metadata cannot verify migration history, stale schema cache, exact constraints/grants or deployment to other environments. Editing an applied local migration does not deploy it; replay may be unsafe. Review the hosted catalog/history, prepare approved non-destructive reconciliation from this existing file's diff, and verify disposable fixtures with exact cleanup. Do not reset, mark this modified file deployed or rewrite history.

The documented basic Switch message event does not guarantee human/bot proof. The adapter must supply verified non-bot status and genuine registered-agent mention evidence; missing proof blocks task changes with an explanation. Native mention syntax not normalized to the registered-name prefix, unverified aliases, conversational reassignment and automatic claiming of legacy name-only tasks remain unsupported. Private/manual routes still require the separate unfinished human-authentication integration.
