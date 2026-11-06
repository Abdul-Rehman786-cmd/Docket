# Open, mentioned commitments

**Creation is now simpler:** [SIMPLE_COMMITMENT_CAPTURE.md](SIMPLE_COMMITMENT_CAPTURE.md) supersedes the creation, positive human/bot proof and exact-deadline requirements in this document. Tagged capture uses existing ingestion auth/addressing and the delivered sender ID, atomic extraction storage and the embedding worker. The lifecycle/history/reminder machinery and this older internal contract remain separately controlled; creation does not depend on them.

Current local chat policy, 2026-11-06. This supersedes mapped-member onboarding and untagged task updates in AUTOMATIC_COMMITMENTS.md. That legacy contract remains available to other internal integrations. No hosted task or external chat message was changed.

## Identity and mentions

An authenticated listener delivery in an enabled, configured room establishes a previously unknown human sender. No account linking, registration, invitation, manually assigned role or sender mapping is required. Ownership is `openchat:` plus SHA-256 of the JSON tuple `[tenant UUID, internal room UUID, "switch", immutable Switch sender ID]`. Switch puppet identity is the namespace; external IDs and display names never merge accounts or grant authority.

The database automatically materializes a default, nonprivileged participation reference in existing `room_members` for lifecycle/FK/reminder compatibility. This is backend bookkeeping, not human onboarding or manager authority. It does not create or overwrite `commitment_chat_identities` mappings. Open actions enforce ownership even if that participation row accidentally acquires an admin role. Existing name-only tasks cannot be claimed by saying “my”; they need authorized internal reassignment.

`X-Ingest-Token` protects ingestion; a distinct `X-Adapter-Token` authenticates source metadata. Shared viewer/admin/ingest credentials alone cannot assert a human identity. The listener fetches its actual registration using `get_agent_detail` and forwards the registered agent ID/name, original sender, timestamp, routing and optional structured mention/bot metadata. The backend compares the registration ID to its configured listener identity. Structured `mentioned_agent_ids`, when present, are authoritative. Otherwise a fallback requires both Switch `addressed=true` and the exact registered `@name` token with boundaries. An addressed follow-up, arbitrary name substring or mention of another account is insufficient. Commands currently put the registered-name mention first.

**Adapter limitation:** Switch's documented basic message event guarantees sender/addressed/timestamp, but does not guarantee structured mention targets or a human/bot flag. Missing `sender_is_bot` remains unknown, never assumed human. If the adapter cannot supply verified `sender_is_bot=false`, tasks stay unchanged and the reply explains the issue without asking for registration. The deployment's metadata capabilities still need verification. Do not fill a constant false for all senders. Native platform mention syntax not normalized to the registered `@name` prefix and unverified aliases are safely declined. These are adapter capabilities, not per-person mapping requirements.

## Conversation contract

Use the actual listener registration name in place of the placeholder:

```text
@<registered-name> I'll finish report by Friday at 09:00.
→ Created your commitment: finish report
@<registered-name> I started my report.
→ Updated the matching commitment.
@<registered-name> Move my report deadline to Monday.
→ Updated the matching commitment. (preserves its existing 09:00 in the room zone)
@<registered-name> What are my tasks?
→ Your open tasks: finish report (in_progress; due …)
@<registered-name> I finished my report.
→ Updated the matching commitment.
```

Also supported: `Cancel my report task.` and `Clear my report deadline.`, each with the genuine mention. The conservative English gate accepts a single explicit `I’ll`, `I will` or `I commit to` promise for creation. Its proposed description must equal the source task phrase. The model cannot propose an owner. Questions, quotations, hypotheticals, tentative promises, ambiguous matches and unresolved dates leave tasks unchanged.

“Friday” without an established time is insufficient for a new deadline: clarification requests a time rather than inventing one. Weekday resolution uses the original message timestamp and confirmed room IANA timezone; same-day ambiguity and DST gaps/folds are rejected. Absolute timezone-aware instants are accepted. Rescheduling can preserve an existing deadline's local time. Open conversational reassignment is refused because target acceptance and authority cannot currently be established; existing protected internal reassignment remains available.

The existing private AI proposes one structured action. Creation/list have a null commitment ID; updates choose a backend candidate. The candidate set contains only this sender's open, non-rejected commitments in the permitted room, across threads, with 21 rows to detect overflow. Mutations refuse a truncated set. “My tasks” returns up to 20 with a truncation notice, without room-wide retrieval. Exact source evidence, task identity, transition and version are independently validated.

General chat extraction has commitment entries removed before storage/replay/confirmation, so unmentioned messages cannot create tasks. Facts and decisions retain their existing paths. An extraction-only runtime response containing one promise can become a proposal and receives the same validation. Mixed extraction preserves unrelated records; request the commitment separately. Mentioned commitments bypass the old draft/yes confirmation flow. Private/manual HTTP routes still fail closed through their individual-user resolver (503); no unauthenticated task endpoint was added.

## Atomic lifecycle and reminders

Creation keeps human approval pending with no reviewer/time, task not started, and records `owner_accepted_at` plus immutable `owner_acceptance_source_id`. This is explicit owner acceptance, not manager approval or blanket acceptance of AI extraction. The older automated-completion evidence stays separate. Owner-accepted pending tasks can start/complete through open chat and satisfy shared reminder eligibility. Internal reassignment clears the previous owner's acceptance.

The repository port adds `create_chat`. Backend-only SQL `create_owner_commitment` locks scope/source/policy, checks a scoped receipt, preserves/inserts source evidence and atomically writes task, deterministic linked memory, create audit and receipt. New-record audit uses expected version 1 with an empty before-state. Updates reuse the lifecycle service/transaction engine, versions, audit, receipts and embedding invalidation. Audit preserves initiating owner identity, backend actor and redacted source evidence.

Creation keys derive from scope/source message; update keys also include task/action. The existing durable journal saves plan/proposal before execution. Lost acknowledgements replay receipts; changed source/payload conflicts. Uncertain or denied actions are not technical retries. Original conversations are not rewritten. Confirmations reuse the existing Switch reply policy/watermark and remain best effort.

The existing reminder planner shares the new owner-acceptance eligibility rule. Automatic participation references satisfy existing membership checks without manual setup. Reminders need no fresh mention. Task version changes supersede queued deliveries through existing triggers; dispatch rechecks eligibility. Quiet hours, cadence, limits, claims/recovery and ambiguous-send handling are unchanged. Global and room reminder defaults remain disabled.

## Migration status and remaining operator steps

All schema/function changes are in the existing `supabase/migrations/history/20261105000200_commitment_chat.sql`. It adds owner-acceptance columns/pair constraint/scoped source FK, create audit value/RPC and open preparation/authorization/eligibility. Existing owner/scope indexes, audit uniqueness and receipts are reused. No new SQL file was created.

Before editing, a read-only hosted PostgREST probe exposed storage/embedding support but no lifecycle/chat/reminder functions or columns. Exact migration history, grants/definitions, schema-cache freshness and other environments could not be verified. This is not proof the migration was never applied anywhere. Editing locally does not update hosted Supabase; replaying a previously applied version can fail or be unsafe. No migration was marked deployed, no history changed and no reset executed.

1. Review the real hosted catalog/history and storage/embedding prerequisites. Review a non-destructive hosted reconciliation from the existing migration diff, preserving data. Obtain approval before remote changes. Do not replay the destructive core baseline or overwrite migration history. This task creates no replacement SQL file.
2. Verify approved disposable hosted creation/actions/receipts/history/memory/reminder invalidation, exact grants and cleanup. No real team tasks are authorized.
3. Retain existing tenant/allowed-room mappings. Enable only reviewed room chat-policy rows and confirm their IANA timezone. Do not create/overwrite real policies automatically. There is no per-person configuration step.
4. Configure a distinct private adapter secret locally; retain non-AI listener and private-runtime credentials. Enable `AUTOMATIC_COMMITMENTS_ENABLED=true`, restart API/listener, and verify registration lookup, immutable human identity and explicit mention metadata. Missing proof blocks task changes. The backend appends the expanded proposal contract to the existing private runtime.
5. Keep `SWITCH_REPLY_TO_DISCORD=true` when confirmations are desired. Enable reminders separately through `REMINDERS_ENABLED`, `REMINDER_DELIVERY_ENABLED` and reviewed room reminder policy. Existing single-worker deployment limits apply.
6. Obtain authorization before real Switch delivery verification. Fake replies/sinks and local database success do not establish external delivery.

Tests: backend `python -m pytest -q`; from `supabase/tests`, `npm run test:chat` and `npm run test:reminders`. Python uses fake persistence, mocked AI/HTTP and a real disposable SQLite recovery journal. PGlite runs real disposable PostgreSQL SQL for creation/replay/ownership/version conflicts/rollback, source/audit/memory consistency and reminder supersession. Its single connection does not prove multi-connection lock contention or multi-instance safety. No real hosted action, model run or external send is claimed.
