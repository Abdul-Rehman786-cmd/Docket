# Automatic commitment management from chat

For current creation, use [SIMPLE_COMMITMENT_CAPTURE.md](SIMPLE_COMMITMENT_CAPTURE.md). Registration/member mapping/human classification and lifecycle activation are not prerequisites for tagged capture. This document describes the separately controlled advanced action integration.

**Current open chat policy:** [OPEN_COMMITMENTS.md](OPEN_COMMITMENTS.md) supersedes the mapped-member onboarding and untagged-action requirements below. The older mapped contract is retained for other internal integrations and its historical tests; the ingestion worker uses `prepare_open`, not this legacy onboarding path. Open chat derives sender ownership without registration, requires a genuine agent mention, supports creation/start/own-task reads, and records explicit owner acceptance. The existing local chat migration has been revised by user instruction; editing it does not deploy or safely replay an applied migration.

Implemented locally on 2026-11-05. Hosted migration, live sender onboarding and external chat verification have not been performed. No real tasks or Discord messages were changed.

## Execution path and reused components

The subsequent [reminder milestone](REMINDERS.md) adds separate planning/outbox/dispatch modules and extends the shared room policy. It does not change chat acceptance or public authentication rules. Hosted lifecycle/chat/reminder deployment remains pending review; the earlier no-scheduler scope statements describe this chat module's original task.


```text
Discord -> Switch authenticated event stream -> existing listener
    -> POST /messages (ingest credential + separate adapter credential)
    -> existing durable ingestion journal
    -> backend resolves mapped human and bounded eligible candidates
    -> existing private Codex runtime proposes task_changes
    -> backend evidence gate -> CommitmentService.perform_chat
    -> one lifecycle transaction -> commitment + memory + audit + receipt
```

Existing extraction, private Switch runtime, source identifiers, scoped room roles, lifecycle transitions, versions, receipts, audit history, deterministic summaries, embedding invalidation/recovery and journal retries are reused. No second bot, direct model SQL, new human-login screen, manual task controls, scheduler or notification integration was added. New public lifecycle endpoints retain their fail-closed individual-user authentication dependency. The automatic worker calls the internal service directly; it never calls those endpoints with an ingest token.

## Identity and authorization

The viewer's shared ingest token is not evidence of a person. Listener forwards now additionally carry `X-Adapter-Token`, configured through `SWITCH_ADAPTER_TOKEN`. It must be separate from the ingest/admin/database credentials and at least 32 random characters. The controller verifies it before internally marking a durable delivery `adapter_verified`; that field is not accepted in incoming message JSON. The listener preserves the actual Switch sender, optional external platform author, message ID, original timestamp and thread. The adapter token authenticates our trusted listener, not each individual speaker. Upstream platform verification remains Switch's responsibility.

`commitment_chat_identities` is a backend-maintained mapping of `(tenant, internal room, Switch sender, external author or empty string)` to a stable `room_members.switch_user_id`. Missing mappings, missing timestamps, missing/disabled policies or unresolved membership cannot execute actions. Names in message text and `sender_name` never establish initiator identity. Do not infer mappings from a participant name or opaque ID prefix. If platform author metadata is unavailable, onboard the exact Switch puppet ID with an empty external author; if it is available, match both fields exactly. Map verified humans only; model/application senders remain excluded by the existing loop guards.

Runtime privileges cannot provision identities or policies. A trusted operator must review the mapping and actual membership in an approved backend onboarding step. The database rechecks the mapping, enabled policy, room, member role and task ownership under locks on every write. The worker's fixed execution identity is `switch-ingestion-worker`; audit records separately identify the initiating member and exact source. The model cannot supply either authority or scope.

The existing unauthenticated `/viewer/env` read/write routes were disabled with 503 because they exposed/replaced backend credentials. Otherwise a public caller could obtain the adapter credential and impersonate the listener. Configure secrets locally; do not use viewer environment APIs. The message feed, WebSocket and AI viewer remain in place. The old environment panel cannot read or save configuration.

## Automatic policy

| Action | Chat authority and evidence |
| --- | --- |
| Complete | Owner of the matching eligible task, or room admin for already approved work; clear first-person completion evidence |
| Cancel | Owner or room admin; explicit cancellation statement |
| Reschedule / clear deadline | Owner or room admin; explicit change, resolved instant or explicit removal |
| Reassign | Room admin only; explicit settled handoff and a uniquely resolved current in-room member |

An `approver` role does not gain admin authority in this chat policy; it may update its own tasks. Public/manual lifecycle policies remain unchanged, including their stricter assigned-user completion rule. Chat never approves, rejects, starts or reopens tasks. Rejected or terminal work cannot be changed by a new conversational action. Equivalent eligible actions remain idempotent through the existing service.

The supported handoff policy is deliberately **`admin_only`**. An owner's statement that “Rehman is taking over the report” is an unaccepted proposal and makes no change. A mapped room admin's identical settled statement satisfies the policy. The target must resolve uniquely against trusted backend labels or a stable platform mention such as `<@platform-id>`, and its stable ID must match the proposal and current room membership. Duplicate labels or an out-of-scope target leave ownership unchanged. Labels locate references; they do not prove who is speaking. Target-acceptance conversations and owner-only handoff negotiation are not implemented.

### Pending approval compatibility

New extraction remains `pending` / `not_started`; no human approver is fabricated. Pending tasks may be cancelled, reassigned or rescheduled under the chat authority policy. Completion of a pending task additionally requires its actual owner and the room's explicit `accept_pending_owner_completion=true` policy. This option defaults to false. Admin authority alone cannot complete another member's pending task.

An allowed pending completion leaves `approval_status=pending` and the reviewer/time unchanged, while setting `task_status=completed`, the completion time, `automated_acceptance_at` and `automated_acceptance_source_id`. The summary explicitly identifies automated owner completion evidence. This is an observation of the owner's completion statement, not human approval. Public/manual completion still requires approval. Rejected records are never accepted automatically.

Existing commitments need a stable `owner_switch_user_id`. Extraction currently retains unresolved owner names; this change does not silently assign those names to people or backfill ownership. Unassigned/name-only commitments remain unchanged until a trusted stable assignment exists. This is a deliberate authorization limitation, not proof that every extracted task is immediately manageable.

## Proposal and evidence contracts

`LLMDecision` gains optional `task_changes`. Each proposal carries action, candidate commitment UUID, original Switch source message ID, exact full supporting source text, `clarification_required`, optional stable assignee and optional aware deadline. It cannot include roles, actors, tenant or room authority. Lifecycle proposals require `action=none`, empty extraction arrays and no retrieval request. The prompt asks for at most one proposal; multiple bounded proposals produce one ambiguous outcome without partial execution.

Before reasoning, `prepare_commitment_chat` registers immutable redacted source evidence and resolves the actor, policy and candidate set. Candidates are current owned tasks, or tasks visible to the room admin, in the exact room/thread, pending/approved and nonterminal. Their original source conversation must not postdate the update. It returns at most 21 rows and 101 roster entries; exceeding 20 candidates or 100 roster entries suppresses actions rather than selecting from an incomplete set. No unrelated room history or tenant search is offered. This initial implementation uses the current message and structured candidate context, not broad conversational history.

The backend freezes the plan in the journal before contacting Codex. It independently verifies the proposal against that plan, including exact source ID and full-message evidence, a unique subject match, actual actor authority, target resolution, deadline resolution and candidate version. The lifecycle service and database revalidate current state and authorization before writing. Model confidence is not an authorization input.

The evidence grammar is conservative English. Supported examples include:

- “I've sent the report.” / “I have finished the report.”
- “Cancel my report task; it's no longer needed.”
- Admin: “Rehman is taking over the report.”
- “Move my report deadline to Monday.” / “Move my report deadline to 2026-12-01T17:00:00+05:00.”
- “Clear my report deadline.”

Supporting text must match a unique candidate's meaningful subject words, not merely the ID picked by the model. Bare “Done” requires exactly one eligible candidate. Questions, quotations, block quotes, hypotheticals, tentative/negative statements, unsupported clauses and clarification-required proposals cannot change a task. Unsupported natural-language variations fail closed; expanding the grammar requires additional positive and negative tests. Matching by keyword alone never authorizes an action.

Lifecycle-looking messages cannot create a second commitment even if the AI mistakenly returns new extraction. Ordinary retrieval questions continue through the existing query path; mixed update/extraction/question output can retain its query while suppressing duplicate storage. Ambiguous interpretations finish once with a safe diagnostic, not an automatic reinterpretation loop.

### Deadlines

Absolute dates require an explicit offset. Relative weekdays use the original source timestamp and a confirmed room IANA timezone. A weekday means its next strictly future occurrence within the coming week; same-day weekday wording is ambiguous. Time must be explicitly supplied as `at HH:MM` or preserved from the task's existing deadline in that confirmed timezone. A missing time, missing/unknown timezone, unsupported date expression, or ambiguous/nonexistent DST clock time leaves the task unchanged. The backend recomputes the instant and compares it with the model's proposal. It never uses worker processing time to resolve a date. Explicit deadline clearing requires clear/remove wording and null, not an unresolved date.

`tzdata` supplies timezone definitions on systems without an IANA database, including Windows; see [Python's ZoneInfo data-source documentation](https://docs.python.org/3/library/zoneinfo.html#data-sources). Stored deadlines remain consistent UTC instants. A policy/timezone change while reasoning conflicts instead of applying an outdated interpretation.

## Durability, atomicity and outcomes

The existing private SQLite ingestion journal adds `chat_plan` and `chat_outcome` columns through a local additive upgrade. Validated decisions, original versions and evidence plans survive technical failures/restarts. Completed jobs clear the plan/payload/decision and retain safe outcome/source references. This remains a single-host, single-worker journal, not a distributed queue. Existing jobs without adapter attestation cannot gain chat authority retrospectively.

Idempotency UUIDs derive from tenant, room, original source message, commitment and action. One source can produce at most one lifecycle audit event, enforced by a database unique index and source-row lock. Replayed source/proposal content returns the stored receipt, with no additional update, event or vector invalidation. A new interpretation under a consumed source conflicts, even if local journal history were lost. Changed source content or identity conflicts. Version conflicts do not silently reselect a newer task or overwrite it. A new user message is required to clarify/reconfirm.

The candidate/source registration RPC and the preflight read are separate from the mutation transaction. They are **not** described as one atomic transaction. `perform_chat` calls one backend-only write RPC, using the shared lifecycle transaction engine. Under locks it validates source scope, current mapping/member/role, policy snapshot, target membership, idempotency, version and transition, then writes task state, linked memory, audit event and receipt atomically. A failure at receipt insertion rolls all lifecycle writes back; already registered source evidence remains available.

Audit adds backend actor, initiating member, source evidence ID, exact redacted evidence, derived outcome and before/after lifecycle values. Source messages and original message links are retained. Linked summary edits use the existing invalidation trigger, clearing stale vectors/claims; completed/cancelled summaries become inactive and ineligible for reminders. No scheduler runs.

Outcomes are `applied`, `no_change`, `ambiguous`, `unauthorized`, `conflict` or `failed`. They appear in the AI response contract and retained journal diagnostics. Nontechnical declines finish the job and are not retried. Technical failures use the existing three-attempt/thirty-second-backoff recovery; the saved proposal/version is reused. Disabling the global feature also blocks cached proposals during recovery. A replayed `applied` result describes the original accepted mutation, not another state change.

Untagged automatic actions and ambiguity remain silent. Addressed messages may receive a deterministic brief result or clarification under the existing reply setting/watermark policy. Confirmations do not use model-generated claims of success. Existing AI/application sender and persisted echo guards remain active.

## Files and deployment

Core files: `models/commitment_chat.py`, `services/commitment_chat_evidence.py`, `services/commitment_chat_service.py`; the existing lifecycle models/service/repository gain an internal chat contract. The AI decision, prompt and worker, ingestion controller/journal, security/configuration and listener are extended. Unsafe viewer environment routes are disabled. `requirements.txt` adds timezone data. Tests cover fake domain state, actual local journaling, mocked AI/HTTP and disposable SQL.

Additive `supabase/migrations/20261105000200_commitment_chat.sql` adds policy, trusted-identity and immutable-source tables; automated-acceptance columns; audit source/backend/outcome columns; a one-source/one-action index; source/candidate preparation and internal mutation functions. It factors the existing lifecycle write into one shared engine with manual/chat wrappers, preserving manual authorization and transaction behavior. The new outcome column classifies existing no-op events without changing old actor/state evidence. Previous migration files are not rewritten. Source/policy/identity tables use RLS and backend-only grants; narrow lock-marker column privileges permit row locks without granting runtime modification of identity, policy or evidence fields.

Deployment remains gated:

1. Review actual hosted definitions and approve non-destructive storage/embedding recovery prerequisites, then the lifecycle and chat migrations in order. **Never run the destructive core baseline remotely.** No hosted SQL was applied here.
2. Onboard actual tenant/room/member/sender tuples with a trusted operator. Add an enabled room policy and confirmed timezone; explicitly decide whether pending owner completion is allowed. Do not map bots or infer people from names.
3. Generate a distinct adapter-only token into ignored local `.env`, configure the listener/backend, install requirements, and set `AUTOMATIC_COMMITMENTS_ENABLED=true` only after those checks. The default is false; `.env.example` contains placeholders only. Keep secrets out of the viewer, mobile app and commands/transcripts.
4. Restart backend/listener and verify using disposable exact-scope fixtures after deployment approval. Hosted concurrent lock contention and real Switch-to-member identity mapping still need live verification. Do not send external messages or alter real tasks without authorization.

No human-login integration is needed for this trusted chat path. Public/manual lifecycle endpoints still return 503 until individual-user authentication is integrated separately.

## Verification

```powershell
cd D:\Hackathon\team-memory\backend
.\.venv\Scripts\python.exe -m pytest tests/test_commitment_chat.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd ..\supabase\tests
npm run test:chat
```

The SQL runner creates a fresh disposable PGlite PostgreSQL database, applies all three migrations and runs both the prior manual lifecycle fixture and new chat fixture. It never reads `.env` or contacts hosted services. Python tests use fake lifecycle persistence and mocked AI responses, but a real disposable SQLite journal for recovery tests. SQL tests verify actual resulting records, audit, memory and rollback, including service-role execution. These are stronger than proposal-only tests, but are not real hosted or external-chat verification. See [the execution report](supabase/COMMITMENT_CHAT_VERIFICATION.md).
