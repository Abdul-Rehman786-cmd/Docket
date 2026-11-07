# Team Memory

Source-backed project memory from team chat: capture commitments, retain decisions
and facts, search with local embeddings, and deliver reminders through Switch.

**Stack:** FastAPI, Supabase/PostgreSQL, optional SQLite adapters, local ONNX
`Supabase/gte-small` embeddings (384 dimensions), Switch-connected private Codex,
a browser viewer, and an Expo mobile starter.

## Feature status

Status reflects the inspected checkout and latest reported runtime results.
Implementation, hosted deployment and live verification are separate milestones.

| Module | Available | Remaining / limits |
| --- | --- | --- |
| Chat and AI | Authenticated Switch ingestion, durable jobs, duplicate/echo guards, private AI reasoning and application replies. | One API worker and one listener; verify room scope and private AI isolation per deployment. |
| Memory and decisions | Structured extraction, linked source evidence, save/skip drafts, embeddings and scoped semantic search. | Database functions and model assets must be configured; input acknowledgement is not storage success. |
| Commitment creation | Mention the registered bot with a personal promise; save source/task/summary and queue indexing. Ownership uses the sender ID without registration or member mapping. | Preserve unresolved deadline wording; creation does not automatically enable lifecycle/reminder policies. |
| Commitment lifecycle | Approve, reject, start, complete, cancel, reassign, reschedule, scoped reads/history, versions and atomic audit/idempotency. | Manual routes require individual-user authentication. Conversational actions have separate adapter, policy and schema gates; hosted verification remains deployment-specific. |
| Reminders | Planner, scheduler, durable outbox, claims/recovery, eligibility rechecks, quiet hours, cadence and Switch dispatch. | Planning, delivery and room policies are independently gated. Real external delivery requires verification. |
| Discord documents | Upload consent, bounded parsers, versions/chunks, embedding/publication, listing, selection, questions and citations. | PDF save reported live; document questions blocked by adapter configuration. Full cited retrieval is not yet verified. |
| Slack documents | Part of the intended upload-and-reference workflow. Slack connectivity has been checked. | **Incomplete:** current document event/configuration/chat paths remain Discord-specific. Slack upload and cited answers are not working end to end. |
| Person profiles | Optional sender/tenant/room-scoped claims, corrections, evidence and retrieval. | HTTP reads require trusted user identity; verify sender isolation and current deployment. |
| SQLite and storage | General record/vector storage, profiles/reminders, cloud-to-local migration tooling, retention and quotas. | Not full engine parity: current document and lifecycle adapters still use Supabase. Migration is not continuous sync. |
| Viewer and mobile | Browser feed, AI results and development settings; Expo health-check starter. | Settings/storage authorization needs work. Full authenticated mobile tasks/documents/search/settings remain unfinished. |
| Future workflows | Document conflicts, reviewed write-back, broader preferences, recording controls and multi-workspace onboarding are in the project plan. | Not implemented as complete workflows. Uploading a new document version is not approved write-back. |

## Architecture

View the [full architecture and settings diagrams](docs/architecture-diagram.md)
or the [standalone SVG](docs/team-memory-architecture.svg), including incomplete
integrations and current authorization boundaries.

```text
Discord / Slack -> Switch -> listener -> authenticated FastAPI
                                            |
                         private AI proposals + domain validation
                                            |
                              atomic repository operations
                                            |
                        Supabase / supported SQLite adapters
                                            |
                           embeddings -> scoped retrieval
                                            |
                        non-AI Switch client -> originating chat

Reminder planner -> durable outbox -> dispatcher -> Switch
Browser / Expo -> FastAPI only
```

Slack document handling is a target in this diagram, not a live-completion claim.
AI interprets requests; backend services enforce ownership, scope, transitions and
persistence. Reuse the existing private AI runtime and separate non-AI client.
Keep direct AI responders out of input rooms to avoid duplicate responses.

| Location | Responsibility |
| --- | --- |
| `backend/app/controllers/`, `models/` | HTTP validation, authentication dependencies and domain types |
| `backend/app/services/`, `repositories/` | Business rules, adapters, database operations and durable journals |
| `backend/app/core/`, `views/` | Configuration/security/models and browser UI |
| `backend/tests/`, `supabase/tests/` | Backend tests and disposable PostgreSQL/PGlite fixtures |
| `supabase/migrations/`, `mobile/` | Database schema/history and Expo app |

See [architecture.md](architecture.md) for design and [AGENTS.md](AGENTS.md) for development rules.

## Local setup (Windows PowerShell)

From the repository root; skip virtual-environment creation if it already exists:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Configure ignored `backend/.env` using [backend/.env.example](backend/.env.example).
Preserve existing credentials. Root `.env` is also supported; shell variables can
override files. Restart both processes after adapter changes.

**Terminal 1 - FastAPI:**

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Match `SWITCH_BACKEND_URL` to this port. For `:8001`, use `--port 8001` instead.
Open `/` for the viewer, `/docs` for API docs and `/health` for API liveness.
Health does not verify database, model or chat readiness.

**Terminal 2 - listener, also from `backend/`:**

```powershell
.\.venv\Scripts\python.exe -m app.switch_listener --check
.\.venv\Scripts\python.exe -m app.switch_listener
```

`--check` verifies API authentication, Switch connection and room membership, then
exits. Keep **one API worker and one listener** running. API reload does not restart
the listener; a second listener can take over its room claims.

A new listener skips offline events; send uploads/consent after it connects.
Transient reconnects resume within that process. Accepted backend jobs recover
independently. Preserve `backend/.local/` journals.

### Required configuration

| Group | Settings / meaning |
| --- | --- |
| Switch connection | `SWITCH_BRIDGE_URL`; non-AI `SWITCH_REQUESTER_AGENT_ID` / `SWITCH_REQUESTER_API_KEY` (fallback: `SWITCH_AGENT_ID` / `SWITCH_AGENT_API_KEY`); private `SWITCH_AI_ROOM_ID` / `SWITCH_AI_SENDER_ID`. |
| Scope and ingress | `SWITCH_TENANT_ID`, `SWITCH_ROOM_IDS`, matching enabled database mappings, `SWITCH_INGEST_TOKEN`, `SWITCH_BACKEND_URL`. The viewer feed uses ingest authentication, not a human session. |
| Trusted chat adapter | `SWITCH_ADAPTER_TOKEN`: locally generated, at least 32 characters, distinct from ingest/admin/Supabase credentials. Required for document questions and advanced actions. |
| Document uploads | `SWITCH_DOCUMENT_ADAPTER_TOKEN`: separate local secret; `SWITCH_DOCUMENT_DISCORD_BRIDGE_IDS`: exact Switch bridge IDs; `SWITCH_DOCUMENT_BOT_SENDER_ID`: exact non-AI participant ID, not its agent UUID. |
| Storage | `DATABASE_ENGINE`, explicit `SQLITE_DB_PATH` for SQLite; backend-only `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (alias: `SUPABASE_SECRET_KEY`); separate `ADMIN_API_TOKEN` for operator routes. |
| Feature gates | `DOCUMENT_INGESTION_ENABLED`, `SWITCH_DOCUMENT_ATTACHMENTS_ENABLED`, `LOCAL_EMBEDDINGS_ENABLED`, `AUTOMATIC_COMMITMENTS_ENABLED`, `REMINDERS_ENABLED`, `REMINDER_DELIVERY_ENABLED`, `PERSON_PROFILES_ENABLED`, `SWITCH_REPLY_TO_DISCORD`. Room policies also apply. |
| Deadline resolution | `COMMITMENT_TIMEZONE`: confirmed IANA timezone; leave unknown dates unresolved rather than inventing a deadline. |

`SWITCH_DOCUMENT_SLACK_BRIDGE_IDS` was the intended Slack allow-list setting, but
**the current Settings model does not define it**. Adding it alone does not enable
Slack documents. Complete the platform-aware integration; do not place a Slack
bridge in the Discord allow-list. Some example-file comments describe older gates;
module contracts and actual code determine behavior.

For local embeddings, install assets from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-embeddings.txt
.\.venv\Scripts\python.exe -m app.embedding_setup
```

Setup downloads pinned public assets and verifies hashes. `app.switch_setup` is
agent provisioning, not routine startup; review its effects before using it.

## Documents: upload and reference

The shared target is **upload -> uploader consent -> parse -> save versions/chunks
-> embed -> publish -> answer with citations**. Discord implements this contract;
Slack integration remains incomplete.

```text
User uploads proposal.pdf
Bot: Save proposal.pdf to team memory? Reply yes or save, or no to skip.
Uploader: yes
Bot: Processing proposal.pdf...
Bot: Saved proposal.pdf to team memory.
User: @<registered-agent> What documents are saved in this room?
User: @<registered-agent> Summarize proposal.pdf and cite it.
```

Supported parsers: UTF-8 text, Markdown, text-based PDF, DOCX and PPTX; no OCR for
scanned PDFs. Defaults: four files, 8 MiB/file, 256 KiB extracted text. Consent is
uploader-only; ambiguous uploads require a filename. No personal registration is
required. Notifications are channel-only; source/thread metadata is retained.

The saved reply is intended to follow completed chunk embeddings and publication.
A PDF save was reported, with a duplicate processing notice; exact cloud records,
deduplication and cited retrieval still need independent verification. Document
selection is sender/tenant/room-scoped and expires after 15 minutes or API restart.

**Current question blocker:** `general_adapter_ready=False` / `adapter_verified=False`.
Configure the valid general `SWITCH_ADAPTER_TOKEN` and restart both processes.
Upload consent uses a different token, so files can be saved while questions fail.

Slack completion must preserve platform/bridge scope through the existing listener,
reuse consent/storage/indexing, and support scoped document questions. Verify
Switch file permissions and actual source metadata before claiming native links or
thread support. Test each platform with an authorized disposable upload, stored
records/vectors, a paraphrased cited question, replay/recovery and exact cleanup.

## Supabase migrations and deployment safety

The consolidated fresh-install schema is
[supabase/migrations/20261104000100_initial_schema.sql](supabase/migrations/20261104000100_initial_schema.sql).
It includes storage/embeddings, commitments/chat, reminders, documents and profiles.

**It resets Team Memory tables. Never replay it on an existing hosted database or
real team data.** Existing installations require reviewed, non-destructive upgrades
and approval before remote changes. Historical scripts in
`supabase/migrations/history/` are not another fresh-install migration chain.
See [Supabase setup](supabase/README.md). Local SQL does not prove hosted deployment.

Keep secrets in ignored `.env` or approved deployment secrets. Mobile must call
FastAPI and must never contain privileged keys in `EXPO_PUBLIC_*`. Shared adapter,
ingest and admin credentials do not identify individual humans; manual lifecycle
and profile routes fail closed until real user authentication is integrated.

**Deployment blocker:** current viewer environment/settings, migration and storage
controls lack route-level authorization. Treat the API as local development only
until those controls are protected. Document/lifecycle repositories still use
Supabase even when general storage selects SQLite. No full engine parity or
multi-instance delivery guarantee is claimed.

## Troubleshooting

Use content-free logs: `FEATURE_LOGS_ENABLED=true`, `LOG_LEVEL=INFO`.
Keep message/wire logging off; see [feature logging](FEATURE_LOGGING.md).

| Symptom | Meaning / action |
| --- | --- |
| Listener fails before connecting | Check API process, matching port and ingest authentication. |
| Upload stops listener; configuration flags are false | Populate document bridge, participant ID and adapter settings; restart both processes. |
| Document question rejects listener identity | Fix the separate `SWITCH_ADAPTER_TOKEN`; expect `general_adapter_ready=True`. |
| `confirmation_requested` / `queued` | Consent or scheduling succeeded; import/indexing may still be pending. |
| `processing_unavailable` | Inspect document scope/database/model diagnostics; the generic warning does not identify the cause. |
| `embedding_jobs=0` | No general-memory jobs claimed; does not prove a particular document was indexed. |
| Reminder `no_eligible_intention` | No reminder due under the task state and policy; not a delivery error. |

An acknowledgement is not cloud persistence. A posted reply is not independently
verified chat delivery. Inspect final outcomes after fallback errors or Windows
connection resets before assuming the listener stopped.

## Verification

Backend, from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Disposable SQL, from `supabase/tests/`:

```powershell
npm ci
npm test
npm run test:commitments
npm run test:chat
npm run test:reminders
npm run test:documents
```

SQL runners use disposable PostgreSQL/PGlite, not hosted Supabase. Distinguish mock
repositories/transports, synthetic vectors, real inference, SQL and live chat
checks. Historical reports apply to their tested revisions; this documentation
edit does not certify runtime tests or deployment.

## Next milestones

1. Fix Discord document-question authentication and verify published records,
   real vectors and cited answers.
2. Complete and independently verify Slack document uploads and references.
3. Reconcile hosted functions/schema/permissions through approved safe upgrades.
4. Finish user authentication and protect settings/storage controls.
5. Verify lifecycle/reminder policies and delivery; complete intended SQLite parity.
6. Implement document conflicts, reviewed write-back, full mobile/settings,
   recording controls and multi-workspace onboarding.

## Module documentation

- [Commitment capture](SIMPLE_COMMITMENT_CAPTURE.md),
  [lifecycle](COMMITMENT_MANAGEMENT.md),
  [automatic actions](AUTOMATIC_COMMITMENTS.md),
  [open policy](OPEN_COMMITMENTS.md)
- [Reminders](REMINDERS.md) and [person profiles](PERSON_PROFILES.md)
- [Document service](supabase/DOCUMENT_INGESTION.md),
  [Discord uploads](supabase/DISCORD_DOCUMENT_INGESTION.md),
  [document chat](supabase/DOCUMENT_CHAT.md)
- Historical verification: [documents](supabase/DOCUMENT_DEPLOYMENT_VERIFICATION.md),
  [commitment chat](supabase/COMMITMENT_CHAT_VERIFICATION.md),
  [reminders](supabase/REMINDER_VERIFICATION.md)
