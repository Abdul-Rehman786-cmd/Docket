# Docket

Team Memory turns team conversations into source-backed, searchable project memory.
It combines a FastAPI backend, Switch-connected chat, private AI reasoning, local
embeddings, Supabase storage, optional SQLite storage, and a browser viewer. The
Expo mobile app is currently a starter, not a complete team-management interface.

The project covers Discord **and Slack**. They do not yet have equivalent support:
Discord ingestion and document upload have live evidence; Slack connectivity has
been checked, but Slack document upload and cited document conversations are still
incomplete in the current checkout. A feature being in the project scope does not
mean it is deployed or working end to end.

## Functionality and current status

This overview reflects code inspection and the latest user-reported runtime
results. Linked verification reports describe their own snapshots; they do not
prove the current hosted schema, configuration, or every later code revision.

| Module | Functionality present | Remaining work / verification |
| --- | --- | --- |
| Chat ingestion | Switch listener, authenticated FastAPI ingress, configured tenant/room boundaries, source IDs, durable receipt/retry journal, duplicate and self/echo guards. | Keep one listener running. Slack document events need explicit platform support; receiving an ordinary Slack message does not verify file processing. |
| Private AI and replies | Existing Switch-connected Codex runtime interprets messages and answers questions; a separate non-AI client posts application replies. Viewer questions and configured chat replies reuse that runtime. | Verify private-room isolation, reply policy, and current credentials for each deployment. No additional model-provider migration is part of this milestone. |
| Useful memory and decisions | Structured extraction of facts, decisions, commitments and linked summaries; source-backed storage and retrieval. General fact/decision drafts can request save/skip confirmation. | Hosted functions, room mappings and selected storage engine must match the running code. Do not infer durable storage from an ingress acknowledgement. |
| Tagged commitment creation | Explicitly mention the registered bot with a clear personal promise; derive ownership from the delivered sender ID; save source/task/summary and queue embedding. No member registration or manual sender mapping is required for this creation path. | Preserve unresolved deadline wording instead of inventing an instant. Saving a task does not automatically enable every lifecycle or reminder policy. |
| Commitment lifecycle | Separate approve/reject/start/complete/cancel/reassign/reschedule service, scoped reads/history, version checks, idempotency, audit and linked-memory updates. | Public/manual actions fail closed without individual-user authentication. Advanced conversational actions have separate adapter/policy/schema gates; local implementation is not proof of hosted deployment. |
| Embeddings and semantic retrieval | Local CPU `Supabase/gte-small`, 384-dimensional vectors, durable jobs, leases, retry recovery, content-hash/stale-vector protection and scoped search. | Install/verify model assets. A saved record or an idle worker is not evidence that a particular vector is stored and searchable. |
| Reminders | Pure planner, bounded scheduling loops, durable delivery outbox, claims/recovery, current-state checks, quiet hours, cadence and Switch dispatcher. SQLite and Supabase adapters exist. | Global planning, delivery, room policy, acceptance and deadline eligibility must all allow the reminder. Hosted atomic functions and real external delivery need separate verification. |
| Discord documents | Upload consent, parsing, immutable versions/chunks, embedding/publication, metadata listing, document selection, questions, summaries and citations are implemented. | User reported a PDF upload reaching the final saved confirmation. Document questions subsequently returned an adapter-verification error. The complete upload-to-cited-answer flow is **not yet verified working** in the current deployment. |
| Slack documents | Required project feature: use the existing Slack/Switch connection for the same consent, import, indexing and reference workflow. | **Not working end to end.** Current attachment allow-list, event model and document-chat origin checks remain Discord-specific. Restore/integrate platform-aware support, then verify real Slack files, publication, citations and replies. A Slack connection alone is insufficient. |
| Person profiles | Optional sender/tenant/room-scoped, source-backed claims, corrections, evidence and profile retrieval; Supabase and SQLite persistence code. | Individual-user HTTP reads remain gated. Verify current enabled policy and sender isolation with fresh fixtures; do not infer cross-platform identity from names. |
| SQLite and storage management | Local relational storage, atomic extraction, memory vectors/search, profiles/reminders, cloud-to-local migration tooling, retention and space controls. | Not every repository switches engines: current document import/chat/worker and lifecycle adapters still use Supabase. Verify each module; do not describe engine switching as complete parity or automatic two-way sync. |
| Browser viewer/settings | Live feed, AI requests/results, database engine/location, migration, retention/quota and reminder controls exist. | Some current settings/environment/storage handlers lack route-level authentication. These are local development controls pending security work, not production-authorized settings. |
| Mobile app | Expo/React Native starter with backend health-check connectivity. | Full authenticated tasks, documents, search and settings screens remain to be implemented. |
| Document conflicts and write-back | Architecture scope includes detecting decisions that conflict with documents and proposing reviewed edits. | Conflict detection, proposed-update approval and safe document write-back are **not implemented**. Uploading a new version is not this workflow. |
| Broader preferences and onboarding | Configuration and some room/reminder policies exist. | Full user authentication, authorized preference editing, recording controls, multi-workspace onboarding and complete mobile settings remain future work. |

## System flow

```text
Discord / Slack -> Switch -> one listener -> authenticated FastAPI
                                               |
                         +---------------------+-------------------+
                         |                     |                   |
                    Private AI          Commitment service    Document service
                  proposals/answers      validated actions     consent/import
                         |                     |                   |
                         +------ repositories / atomic writes -----+
                                               |
                             Supabase / supported SQLite adapters
                                               |
                              embedding jobs -> scoped retrieval
                                               |
                            non-AI Switch client -> originating chat

Browser / Expo -> FastAPI only
Reminder planner -> durable outbox -> dispatcher -> Switch
```

AI proposes interpretations; backend services enforce scope, validation, versions
and persistence. The AI does not receive unrestricted database access. Ordinary
chat uses the shared orchestration, while lifecycle rules, documents, reminders,
profiles and repositories remain separate modules.

The intended document path on both platforms is:

```text
Upload -> uploader confirms -> bounded extraction -> document/version/chunks
       -> embeddings completed -> publication -> cited questions and summaries
```

This is the target for Slack and the implemented Discord contract, not a claim
that both platforms currently pass live end-to-end verification.

## Repository layout

- `backend/app/controllers/`: HTTP validation, dependencies and responses.
- `backend/app/models/`: validated payloads and domain values.
- `backend/app/services/`: extraction, lifecycle, retrieval, documents, reminders,
  profiles, Switch adapters and storage orchestration.
- `backend/app/repositories/`: persistence, atomic-operation adapters and journals.
- `backend/app/core/`: configuration, security, HTTP pools, embedding model and logs.
- `backend/app/views/`: browser viewer and settings.
- `backend/tests/`: domain, endpoint, transport, parser and regression tests.
- `supabase/migrations/`: database schema; `history/` preserves historical scripts.
- `supabase/tests/`: disposable PostgreSQL/PGlite contracts and fixtures.
- `mobile/`: Expo TypeScript app.
- [architecture.md](architecture.md): design, module contracts and historical plans.
- [AGENTS.md](AGENTS.md): repository development/security rules.

## Run locally on Windows

From the repository root:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Skip virtual-environment creation if it already exists. Preserve existing ignored
configuration; never replace a working `.env` with the example. Do not commit
secrets or place privileged credentials in mobile `EXPO_PUBLIC_*` settings.

Start FastAPI in terminal 1:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

`SWITCH_BACKEND_URL` must use the same port. If it ends in `:8001`, either use
`--port 8001` or deliberately change the local setting to match. Use the configured
port for all browser and health-check URLs.

Open `http://127.0.0.1:8000/` for the viewer and `/docs` for API documentation.
`GET /health` confirms API liveness only, not Switch, Supabase or model readiness.
The authenticated viewer feed uses the local ingest token; that token is not a
human session or an admin credential. Do not paste it into URLs.

With the API running, use terminal 2 from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m app.switch_listener --check
.\.venv\Scripts\python.exe -m app.switch_listener
```

`--check` verifies API authentication, Switch connectivity and configured room
membership, then exits without forwarding messages. Run **one API worker and one
listener**. API reload does not restart the listener. A second listener can take
over room claims from the first.

Every new listener process starts at the current stream head: uploads/messages
sent while it was offline are skipped. Transient reconnects within that process
resume from its acknowledged cursor. Already accepted backend jobs and confirmed
imports recover from their journals. Preserve `backend/.local/`; do not delete
journals as a routine retry step.

### Configuration responsibilities

Keep actual values in ignored `backend/.env` or approved deployment secrets. The
backend also supports root `.env`; prefer one clearly managed source and note that
shell environment variables can override files. Restart both processes after
adapter changes so they load matching values.

| Setting | Purpose / source |
| --- | --- |
| `SWITCH_BRIDGE_URL` | Existing Switch API base URL; not an individual messaging bridge ID. |
| `SWITCH_REQUESTER_AGENT_ID`, `SWITCH_REQUESTER_API_KEY` | Existing non-AI client credentials; listener uses these when configured, otherwise the agent credential pair. |
| `SWITCH_AGENT_ID`, `SWITCH_AGENT_API_KEY` | Existing AI/agent configuration; preserve the established private-runtime separation. |
| `SWITCH_AI_ROOM_ID`, `SWITCH_AI_SENDER_ID` | Private reasoning room and exact AI participant identity. Keep the reasoning room separate from input chat rooms. |
| `SWITCH_TENANT_ID`, `SWITCH_ROOM_IDS` | Configured tenant and allowed Switch rooms, with matching enabled database room mappings. |
| `SWITCH_INGEST_TOKEN` | Private listener-to-FastAPI credential, also used for the local viewer feed. |
| `SWITCH_ADAPTER_TOKEN` | Separate locally generated secret, at least 32 characters and distinct from ingest/admin/Supabase credentials. Required for trusted document-question metadata and advanced actions. |
| `SWITCH_DOCUMENT_ADAPTER_TOKEN` | Separate locally generated listener-only attachment/consent credential. Upload success does not validate the general adapter token. |
| `SWITCH_DOCUMENT_DISCORD_BRIDGE_IDS` | Exact Switch Discord bridge IDs from a read-only bridge lookup, not Discord guild/channel IDs. |
| `SWITCH_DOCUMENT_BOT_SENDER_ID` | Exact participant ID of the non-AI client from Switch participant data, not its agent UUID or display name. |
| `DOCUMENT_INGESTION_ENABLED`, `SWITCH_DOCUMENT_ATTACHMENTS_ENABLED` | Document worker/API and chat attachment gates; enabling alone does not configure storage, transport or models. |
| `LOCAL_EMBEDDINGS_ENABLED` | Enables the local embedding worker when assets/dependencies are available. |
| `DATABASE_ENGINE`, `SQLITE_DB_PATH` | General storage selection and explicit local SQLite location. This does not move every module away from Supabase. |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | Backend-only cloud configuration; `SUPABASE_SECRET_KEY` is also accepted. |
| `ADMIN_API_TOKEN` | Separate local/server operator credential for protected diagnostic/document routes. Never embed in mobile. |
| `COMMITMENT_TIMEZONE` | Confirmed IANA timezone for supported deadline resolution; leave blank when unknown. |
| `AUTOMATIC_COMMITMENTS_ENABLED` | Separately enables advanced conversational lifecycle processing after policy/schema setup. |
| `REMINDERS_ENABLED`, `REMINDER_DELIVERY_ENABLED` | Separate reminder planning and external-delivery gates; applicable room policies are also required. |
| `PERSON_PROFILES_ENABLED` | Optional source-backed profile extraction/retrieval. |
| `SWITCH_REPLY_TO_DISCORD` | Existing chat reply flag; changing it does not configure attachment consent or a missing platform adapter. |

`SWITCH_DOCUMENT_SLACK_BRIDGE_IDS` is the intended Slack document allow-list name
from the earlier local integration. **The current `Settings` model does not define
it.** Adding that variable alone currently has no effect; platform-aware listener,
attachment and document-chat integration must be restored/completed first. Do not
put a Slack bridge into the Discord allow-list as a workaround.

The `.env.example` is a configuration reference, not evidence of working runtime
behavior. Some document comments still describe an older origin-attestation gate;
inspect current code and the module contracts rather than requiring people to
register or guessing participant types from their names.

### Private AI runtime

Reuse the existing Switch-connected AI in its private response room. FastAPI does
not need a second model key just to use that established runtime. The non-AI client
receives input and sends policy-approved application responses. Keep any managed
AI session that directly responds in an input room stopped to avoid duplicate
responses. `python -m app.switch_setup` is a provisioning command, not an ordinary
startup command; review its effects before running it on an existing setup.

### Embedding assets

From `backend/`, when setting up local inference:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-embeddings.txt
.\.venv\Scripts\python.exe -m app.embedding_setup
```

Setup downloads pinned public model assets and verifies their hashes. The normal
worker embeds locally using `Supabase/gte-small` with 384-dimensional vectors.
General memory vectors and document chunk vectors have separate job paths. A
storage confirmation must not be presented as semantic-search readiness before
its required vectors are stored.

## Documents on Discord and Slack

Documents mean **uploaded project files and references to their stored content**,
not just this repository's Markdown documentation.

The implemented Discord workflow supports UTF-8 text, Markdown, text-based PDF,
DOCX and PPTX. Scanned PDFs need OCR outside this implementation. Current default
limits are four files, 8 MiB per file and 256 KiB extracted text; configured lower
limits still apply. Binary parsing is bounded and isolated. Source message/file
references, immutable versions, chunks and citations are retained.

Example of the intended shared conversation:

```text
User uploads proposal.pdf in the configured channel
Bot: Save proposal.pdf to team memory? Reply yes or save, or no to skip.
Uploader: yes
Bot: Processing proposal.pdf...
Bot: Saved proposal.pdf to team memory.
User: @<registered-agent> What documents are saved in this room?
User: @<registered-agent> Summarize proposal.pdf and cite it.
```

Only the uploader's exact sender identity can authorize save/skip. Ambiguous
multiple uploads require filename clarification. Display names are not proof of
ownership. No personal registration or manual member mapping is required for the
upload path. Current document notifications use channel-only delivery; original
thread metadata remains provenance.

The final saved reply is intended to wait for import, chunk indexing and
publication. In this session the user reported a PDF reaching that reply, including
a duplicated processing notification. That does not independently verify database
records, every format, absence of duplicate imports or successful citations.

Document chat can list/find/select a document, answer from published passages,
summarize with coverage limits, and cite stored text lines. Selection is scoped to
the sender, room and tenant and expires after 15 minutes or API restart. An active
conversation permits relevant follow-ups without another mention. It must not
invent document inventory or use unrelated room memories when document access fails.

**Current blocker:** questions returned a listener-identity configuration error
with `general_adapter_ready=False` / `adapter_verified=False`. Configure the valid
separate `SWITCH_ADAPTER_TOKEN` for both processes before retrying. Uploaded files
can remain stored while document reads are blocked.

**Slack completion work:** preserve platform and bridge identity through the
existing adapter, authenticate scoped uploads and consent, reuse the existing
import/embedding/publication service, and route questions and citations through
that same Slack scope. Verify file relay permissions in Switch and actual source
metadata before claiming native Slack links or thread behavior. No direct Slack
bot, separate AI or parallel storage pipeline is needed.

Completion requires an authorized disposable test on **each** platform: upload,
consent, inspect source/version/chunks, verify completed vectors/publication, ask
a paraphrased cited question, test replay/recovery, then clean up only the fixture.
Slack connectivity and local mocked tests are not substitutes for those checks.

## Storage, security and deployment boundaries

Supabase provides relational records, source relationships, atomic functions and
vector search. SQLite adapters support general storage and selected modules;
currently document services explicitly instantiate `SupabaseDocumentRepository`,
and lifecycle persistence uses Supabase RPCs. Cloud-to-local migration tooling is
not continuous synchronization or proof of equivalent behavior across engines.

For a **fresh disposable database only**, see
[supabase/README.md](supabase/README.md). The active consolidated schema is
`supabase/migrations/20261104000100_initial_schema.sql`. It deliberately resets
Team Memory tables. **Never replay it against an existing hosted database or real
team data.** Existing installations require a reviewed additive reconciliation
and approval before remote schema changes. Historical migration files are not a
second fresh-install chain. Local SQL availability does not prove deployment.

Private record/document routes require their configured credentials. Manual
commitment/profile routes require a trusted individual-user identity integration
and fail closed without it. A shared ingest/admin/adapter token does not identify
a human or grant room-management rights to everyone.

The current viewer implementation also contains environment-reading/editing,
reminder-configuration, migration and destructive storage controls without
route-level authorization. Earlier claims that all environment routes return
`410` do not match this checkout. Treat those controls as an outstanding security
and deployment blocker; do not expose this development API publicly. This README
update documents the gap and does not change or exercise those handlers.

The mobile app calls FastAPI only. Privileged Supabase keys and adapter credentials
must never be shipped in the Expo app. No production-ready individual-user
authorization or multi-instance delivery guarantee is claimed.

## Diagnostics

Prefer content-free feature logs (`FEATURE_LOGS_ENABLED=true`, `LOG_LEVEL=INFO`).
Keep raw message/wire-level logging off for normal use. See
[FEATURE_LOGGING.md](FEATURE_LOGGING.md).

| Log/result | Meaning / next check |
| --- | --- |
| `/health` returns 200 | FastAPI is alive; does not prove database, chat or model access. |
| `API acknowledged message` | Input accepted, not necessarily saved in Supabase. |
| `confirmation_requested` then `queued` | Upload consent and durable scheduling succeeded; import/indexing may still be pending. |
| `Saved ... to team memory` | Application reports document publication success; verify exact records and cited retrieval for end-to-end acceptance. |
| `general_adapter_ready=False` | General adapter secret absent, too short or colliding with a shared credential. Document questions/advanced actions fail closed. |
| `bridge_configured=False` / `bot_configured=False` / `adapter_configured=False` | Attachment setup incomplete; current listener stops on an enabled attachment event. Check the actual loaded settings. |
| `Document worker reason=processing_unavailable` | A scope, database, model or processing operation failed; generic warning alone does not identify the cause. |
| `embedding state=idle embedding_jobs=0` | No general-memory jobs were claimed; not evidence that a given document is indexed. |
| `no_eligible_intention` | Planner found no reminder due under current state/policy; not an external-send failure. |
| HTTP 400 followed by a posted reply | Some existing transports try a fallback; inspect the final outcome rather than assuming every initial error is fatal. |
| Windows connection-reset callback | Inspect subsequent acknowledgements/recovery; the traceback alone does not establish that the listener exited. |

## Verification commands and evidence

Backend tests, from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Disposable SQL tests, from `supabase/tests/` after installing its declared dev
dependencies:

```powershell
npm ci
npm test
npm run test:commitments
npm run test:chat
npm run test:reminders
npm run test:documents
```

These runners use disposable PostgreSQL/PGlite, not hosted Supabase. Distinguish
fake repositories, mocked AI/Switch transports, synthetic vector fixtures, actual
local model inference, disposable SQL and hosted/live chat checks. Historical test
counts apply only to their tested revisions. This README edit itself does not
rerun or certify the runtime suites and does not deploy or send chat messages.

Further contracts and historical verification:

- [Tagged commitment capture](SIMPLE_COMMITMENT_CAPTURE.md)
- [Commitment lifecycle and auth](COMMITMENT_MANAGEMENT.md)
- [Automatic conversational actions](AUTOMATIC_COMMITMENTS.md) and
  [open commitment policy](OPEN_COMMITMENTS.md)
- [Reminder planning and delivery](REMINDERS.md)
- [Person profiles](PERSON_PROFILES.md)
- [Document service/API](supabase/DOCUMENT_INGESTION.md),
  [Discord uploads](supabase/DISCORD_DOCUMENT_INGESTION.md), and
  [document conversations](supabase/DOCUMENT_CHAT.md)
- [Historical document deployment checks](supabase/DOCUMENT_DEPLOYMENT_VERIFICATION.md)
- [Historical commitment/chat checks](supabase/COMMITMENT_CHAT_VERIFICATION.md)
  and [reminder checks](supabase/REMINDER_VERIFICATION.md)

## Remaining milestones

1. Complete document-question adapter configuration and verify the reported
   Discord PDF through published records, actual vectors and cited answers.
2. Restore/complete Slack platform-aware document upload/reference integration
   and independently verify the same workflow on Slack.
3. Reconcile required hosted schema/functions and permissions against the actual
   deployment, using approved non-destructive changes and disposable fixtures.
4. Finish individual-user authorization and protect development settings/storage
   controls before any public deployment.
5. Verify automatic lifecycle/reminder policy compatibility and reliable delivery
   for the active engine; do not silently human-approve extracted tasks.
6. Complete/test SQLite parity only where intended, including migration, retention
   and quota behavior; preserve teammate-owned work and existing data.
7. Implement document conflict detection, proposed edits, approval and safe
   write-back as a distinct future workflow.
8. Finish authenticated mobile/product settings, optional recording controls and
   multi-workspace onboarding after the core chat/document paths are reliable.

No commit, push, hosted migration, live chat test or real task/document mutation
is implied by updating this documentation.
