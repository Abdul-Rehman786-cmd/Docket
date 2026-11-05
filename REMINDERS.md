# Reminder planning and delivery

Implemented locally on 2026-11-05. Defaults are disabled. Hosted metadata was inspected read-only; commitment/chat/reminder deployment is blocked pending review and approval. No real tasks or external chat messages were changed.

## Current deployment findings

The actual configured hosted PostgREST metadata exposes `store_message_extraction`, `claim_memory_embeddings` and `finish_memory_embedding`, and the required memory hash/status/claim/lease columns. The older report saying those functions were absent is superseded by this observation.

The metadata does **not** expose `read_commitment_lifecycle`, `apply_commitment_action`, `prepare_commitment_chat`, `apply_commitment_chat_action` or any new reminder RPC. Commitment `version`, cancellation and automated-acceptance columns, chat policy/identity tables and reminder occurrence/version/send-start columns are absent from the exposed schema. The hosted API therefore cannot currently support the implemented workflow. Metadata alone cannot rule out a stale schema cache or verify full function bodies, constraints, migration history, RLS and exact client grants.

Local configuration has two allowed Switch rooms, a configured tenant and non-AI identity, automatic commitments disabled, no adapter credential, and both reminder flags disabled. Credential values and private records were not printed. This confirms configuration presence, not Switch runtime health or bridge delivery.

`python -m app.commitment_deployment_check` repeats the read-only metadata probe. The approved operator can use `supabase/reminder_deployment_review.sql` for a read-only catalog review of function hashes, signatures, privileges, constraints and triggers. That catalog script has **not** been run on the host. See [verification/deployment status](supabase/REMINDER_VERIFICATION.md).

## Reused architecture and authorization

The planner calls the existing `reminder_state()` eligibility function. Database guards use the existing `commitment_reminder()` function. Approved, nonterminal tasks with deadlines and stable assignees are candidates; additional checks require an enabled Discord room, enabled reminder policy, confirmed IANA timezone and current room membership. Completed, cancelled, rejected, unassigned and unapproved work is ineligible. Automated owner acceptance remains distinct from human approval; its completed/pending records are not reminder targets. No approval rules or lifecycle action meanings were changed.

The existing backend-managed `commitment_chat_policies` row is extended with reminder preferences. Its chat `enabled` flag still controls chat management; `reminders_enabled` independently controls reminders. No competing preferences table or public settings endpoint was created. Runtime credentials can read policies but cannot change their protected columns. Changes require a trusted database operator until real user-facing settings authentication exists. Adapter credentials do not authorize human preference changes.

The viewer's old reminder values were only browser-local. Its reminder controls are now read-only, and reminder settings are no longer saved/restored from localStorage. Those controls do not display live server policy or enable delivery. Environment-reading/editing routes remain disabled. No room policies or identity mappings were automatically created or overwritten.

## Defaults

| Setting | Default / behavior |
| --- | --- |
| Global planning | `REMINDERS_ENABLED=false` |
| External dispatch | `REMINDER_DELIVERY_ENABLED=false`, independently gated |
| Room policy | `reminders_enabled=false` |
| Timezone | Existing confirmed room IANA timezone; no default or inference |
| Upcoming lead | 30 minutes; one upcoming occurrence per task/policy version |
| Overdue cadence | 24 hours after the previous successful overdue reminder in the cycle |
| Maximum follow-ups | 3 overdue reminders per commitment-version/policy-version cycle |
| Minimum gap for a task | 30 minutes after any confirmed reminder, including prior versions |
| Room spacing | 60 seconds after any successful reminder in that room; in-flight sends also gate another begin |
| Quiet hours | 22:00–08:00 in confirmed room timezone; equal start/end explicitly disables quiet time |
| Routing | Original source thread when present, otherwise original room; optional room-only policy |
| Mentions | None; display names/IDs are never converted into unverified pings |
| Polling / bounds | Separate 15-second loops; scheduler reads at most 25 tasks, dispatcher processes at most 4 claims per tick |
| Claim lease | 2 minutes |
| External attempts | At most 3; retry delays 30 and 120 seconds before attempts 2 and 3 |

Quiet hours are evaluated against actual UTC instants in the configured timezone, including overnight windows, DST gaps and folds. A deferred reminder is not sent or counted as an external attempt. Missed overdue intervals coalesce into one intention; they do not become a backlog. Room spacing prevents multiple deferred tasks from bursting into the room together at quiet-hour end. This can intentionally delay reminders in a busy room.

Rescheduling/reassignment or a reminder policy change creates a new version cycle. Follow-up limits reset for that cycle, but the global last-success gap and room spacing still apply. Persistent failed/ambiguous delivery blocks further automatic reminders in its current cycle until reviewed or superseded. No new deadline/timezone is invented. Due instants use the overdue branch (`now >= due_at`).

## Modules and contracts

| Module | Responsibility |
| --- | --- |
| `models/reminders.py` | Policies, history, work snapshots, intentions, delivery states and notification envelopes |
| `services/reminder_planner.py` | Pure `plan_reminder(work, now)` and quiet-hour calculations; no I/O, wall clock or LLM |
| `repositories/reminder_contract.py` | Fake-friendly storage/sink ports |
| `repositories/reminder_repository.py` | Scoped backend-only RPC transport and response validation |
| `services/reminder_scheduler.py` | Bounded tick, non-overlap lock, cursor and idempotent persistence |
| `services/notification_dispatcher.py` | Current eligibility, routing/rendering hooks, echo protection, send-start and result orchestration |
| `services/switch_notifications.py` | Existing non-AI Switch identity and pooled transport; one post attempt, no unsafe fallback |
| `services/reminder_worker.py` | Application lifecycle and separate planning/dispatch loops |

An intention contains tenant/room/task, stable recipient, task/policy versions, type, ordinal, logical base time, deferred scheduled time and a stable UUID derived from the cycle/type/ordinal. Its identity does not change with each tick. The planner consumes current successful history and pending delivery state, never an LLM deadline interpretation.

The dispatcher accepts readiness/render hooks and a notification-sink port. Future notifications can supply their own envelope/policy adapter while reusing the delivery boundary. Existing AI reply behavior was not migrated. Source-backed fixed reminder text includes task description, UTC deadline, source message UUID and occurrence reference. Source text is redacted and mention characters escaped. It is not an instruction to the model, and routine reminder processing makes no LLM call.

## Persistence, claims and recovery

The additive `20261105000300_reminder_outbox.sql` extends the existing `reminder_deliveries` table. Existing columns for scheduled time, status, attempts, retry, claim/lease, Switch reference and sent time are reused. New fields bind an occurrence to recipient, task/policy versions, kind/ordinal, logical base, durable send-start, invalidation time and frozen notification text. Unique indexes enforce occurrence and logical-cycle identities and one active pending/sending delivery per task. The old time-only unique constraint is replaced so different version cycles can share a timestamp without being conflated.

Unversioned unsent legacy rows are retained as `superseded` with `legacy_unversioned`; they cannot safely prove current intent. Legacy sent rows remain historical and contribute to the last-success gap. Review this classification and affected counts before deployment. No original task, source conversation or lifecycle audit evidence is rewritten.

Scheduler reads and persist calls are separate operations. Each persistence RPC locks the task, rechecks scope/current policy/version, and inserts idempotently. It does not count a successful send. Claiming uses row locks and `SKIP LOCKED`, rotates a claim token and sets a lease. Claiming alone does not increment the external-attempt counter.

The dispatcher reloads task/policy/history, applies quiet hours and cadence, records the existing journal echo fingerprint, then calls `begin_reminder_dispatch`. That short transaction rechecks eligibility, scope, membership, recipient, versions, room spacing and quiet hours, and records send-start/attempt before returning. The transaction ends **before** waiting for Switch. A room advisory lock plus visible in-flight markers prevents two begins from bypassing room spacing. No database transaction spans an external request.

Successful Switch acknowledgement is saved by another atomic RPC, together with `sent_at`, Switch reference, `last_chased_at` and next-cadence estimate. Bookkeeping does not increment the commitment's lifecycle version. The planner uses authoritative delivery history, not the cached next-reminder field alone. Receipt persistence failure cannot safely count as a saved success.

Lifecycle version changes atomically invalidate stale unsent intentions through a trigger; reminder policy changes automatically increment the policy version and invalidate its old intentions. Expired upcoming work is superseded rather than sent late. Room/member removal is rechecked before sending. In-flight sends retain their marker so a later acknowledgement can record what actually happened even after invalidation; stale success does not advance the current task's cadence cache.

Expired **unstarted** claims return to pending with a new token. Expired **started** claims become `ambiguous`, not automatic retries. An explicit ambiguous transport result is also quarantined. A late acknowledgement with the original retained token can record a recovered started attempt. Unknown outcomes preserve text/source/occurrence references for controlled operator investigation. No public endpoint retries or reconciles them.

There is an unavoidable race between the final transaction and the external post: a task may change just after the recheck. The system cannot retract an already accepted message, and no transaction is held open to pretend otherwise. The audit retains that historical outcome. Multi-instance deployment and exactly-once external delivery are **not claimed**.

## Switch delivery semantics and limits

Notifications use the existing `team-memory-client` credentials and `SwitchAPI` connection/pool configuration, through `/ops/post_message`, never a direct Discord API or an additional AI responder. The sink refuses the private AI room, unconfigured destinations, disabled delivery and an identity equal to the AI identity. Original source-thread IDs are preserved where configured. Direct messages, cross-room routing and assignee pings are unsupported in this initial implementation.

Connect/pool failures before posting and explicit 429 rejection are retryable. Explicit 4xx rejection is permanent. Read/write timeouts/errors, 5xx, invalid successful responses and missing message references are ambiguous, because Switch could already have accepted the post. The new sink does not use the existing reply helper's broad fallback after an error; unrelated reply code stays unchanged.

The documentation review did not establish a verified caller-supplied idempotency or automatic history-reconciliation contract for this post operation. The implementation therefore quarantines uncertain outcomes instead of inventing unsupported parameters or blindly posting twice. Operator reconciliation remains pending. See Switch's [posting protocol](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol#writing-a-client) and [thread correlation](https://docs.switchagents.ai/switch-rooms/internals/life-of-a-message#thread-correlation).

`sent` means a valid Switch room-message acknowledgement was received and recorded. It does **not** prove the downstream Discord bridge delivered or that a human read it. Existing configured sender and durable echo-fingerprint guards prevent the application from processing its own reminder as another human task update.

Reminder delivery has its own flag, independent of the existing `SWITCH_REPLY_TO_DISCORD` AI-answer setting. Keeping AI answers server-only does not authorize notifications; both reminder flags and reviewed room policy remain off until explicitly enabled.

## Supported deployment and approval sequence

Supported model: one FastAPI process/worker on one host, existing persistent private `.local` journal, one non-AI Switch listener, hosted Supabase outbox. Both application loops recover from the database after restart. Tests exercise competing claim calls and expired leases locally, but not real multi-connection hosted contention; do not deploy several application instances based on these tests. Polling and bounded batches provide best-effort timing, not an exact-deadline SLA.

Before enabling:

1. Review the hosted catalog and actual prerequisites. Metadata confirms storage/embedding presence, not exact implementation/grants. Do not replay the destructive core migration or rewrite migration history.
2. Approve/apply only the reviewed additive lifecycle `20261105000100`, chat `20261105000200` and reminder `20261105000300` migrations in that order if absent. Inspect legacy reminder rows and actual constraint names first. Refresh PostgREST schema discovery and record genuine applied history only.
3. Review actual room platform/mapping, membership, timezone and reminder policy. No rows are automatically provisioned or enabled. User-facing policy mutation remains unavailable until trusted human authentication exists.
4. Enable planning first (`REMINDERS_ENABLED=true`, delivery false), verify disposable scoped intentions and cleanup after deployment approval, then separately authorize a disposable external-delivery test before enabling `REMINDER_DELIVERY_ENABLED`.
5. Keep the existing non-AI identity in input rooms and Codex isolated in its private room. Confirm original room/thread bridge behavior. Review ambiguous deliveries rather than auto-resetting them.

No hosted schema changes, hosted fixture writes, real team-task changes, external reminders, paid infrastructure or compute changes were made in this task. Full hosted workflow and real Switch/Discord delivery remain blocked/pending, even though local implementation and tests pass.

## Verification commands

```powershell
cd D:\Hackathon\team-memory\backend
.\.venv\Scripts\python.exe -m app.commitment_deployment_check
.\.venv\Scripts\python.exe -m pytest tests/test_reminders.py tests/test_reminder_transports.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd ..\supabase\tests
npm run test:reminders
```

The hosted probe is GET-only metadata. Unit/integration tests use controllable clocks, fake task/preferences/outbox repositories, a real disposable local journal and fake notification sinks. Transport tests mock Switch and PostgREST. The SQL runner uses disposable local PostgreSQL/PGlite, applies all four migrations and verifies previous manual/chat lifecycle fixtures plus the reminder fixture. It never reads `.env` or contacts a hosted service. See the [executed verification report](supabase/REMINDER_VERIFICATION.md) for exact results and limitations.
