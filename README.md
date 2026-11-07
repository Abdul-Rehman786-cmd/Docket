# Docket — Team Memory

Docket turns team chat and uploaded documents into searchable project memory.
Teammates can record commitments, retain decisions and facts, save documents with
uploader consent, and ask source-backed questions from their chat channel.

Built for the Flip the Switch Hackathon using **Switch, Codex, FastAPI, cloud
Supabase/PostgreSQL, and local 384-dimensional text embeddings**. The repository
also includes a browser viewer and optional SQLite adapters.

## How we used Switch

Switch is the communication hub. FastAPI coordinates the work; Codex supplies AI
reasoning through Switch rather than through a separate model API integration.

| Connected component | What it does |
| --- | --- |
| **`docket`** | Our registered non-AI Switch client. A Python listener receives channel events and forwards them to authenticated FastAPI endpoints. The same client posts application replies through Switch. |
| **`codex-discord`** | The existing Codex AI agent. FastAPI sends classification, extraction and answer-generation requests to it in a private Switch room. Its runtime owns model authentication. |
| **Discord-bot bridge** | Connects the Discord demonstration channel to Switch room **Idk** and carries replies back to Discord. |
| **Slack bridge** | Connects the additional **Slack: bot** room. Platform-aware code exists, but equivalent end-to-end document behavior needs separate verification. |

```text
Discord / Slack -> Switch input room -> Python listener -> FastAPI
                                                            |
                                       private Switch room <-> Codex
                                                            |
                                         Supabase + local embeddings
                                                            |
Originating chat <- Switch bridge <- non-AI client <- FastAPI reply
```

### Channel and room setup

Our demo uses the public Discord-linked Switch room **Idk**, containing `docket`.
The additional input room is **Slack: bot**. `SWITCH_ROOM_IDS` contains their Switch
room UUIDs, not Discord channel IDs. `SWITCH_TENANT_ID` identifies the application
scope; the database needs matching enabled room mappings.

The internal room **team-memory-server-responses** contains only `docket` and
`codex-discord`. It has **no messaging bridge or external channel** and must not be
included in `SWITCH_ROOM_IDS`. Codex stays out of input rooms so it cannot bypass
FastAPI and answer independently. The listener receives all events (`filter=all`);
receiving an event does not mean the bot should reply to it.

Switch supplies room, sender, message, timestamp and addressing metadata. FastAPI
validates scope, manages durable jobs and storage, retrieves evidence and returns
answers through the non-AI client. Privileged database credentials stay on the
backend.

**Name compatibility:** this checkout accepts `docket` and the older
`team-memory-client` name in its input-room guard. However, the one-time
`app.switch_setup` command still creates and expects `team-memory-client`.
For our already-provisioned `docket` installation, reuse its existing credentials
and rooms; do not rerun provisioning just to start the application. New setups
using that command should retain its default name. In chat, address the actual
registered agent or its configured alias; an old Discord mention role may be stale.

## Run locally

Use Python 3.11–3.13; local embeddings have been tested with Python 3.13.
Keep the existing Switch server, Discord connection and Codex runtime available.
FastAPI and the listener are two separate processes. Run **one of each**, with
one API worker, and preserve `backend/.local/` across restarts.

### Repository layout

| Location | Responsibility |
| --- | --- |
| `backend/app/controllers/` | HTTP endpoints and authentication dependencies. |
| `backend/app/models/` | Validation, structured AI output and domain types. |
| `backend/app/services/` | Switch communication, extraction, document processing, retrieval and workers. |
| `backend/app/repositories/` | Supabase/SQLite operations and durable local journals. |
| `backend/app/core/` | Configuration, security, logging and the embedding model. |
| `backend/app/views/` | Browser feed, AI results and development settings. |
| `backend/tests/` | Backend regression, security and workflow tests. |
| `supabase/migrations/` | Timestamped database schema and historical scripts. |
| `supabase/tests/` | Disposable database checks, separate from hosted data. |
| `architecture.md` | Detailed design, boundaries and deployment history. |

### 1. Install dependencies

From the repository root in PowerShell:

```powershell
cd backend
python -m pip install -r requirements-dev.txt -r requirements-embeddings.txt
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
python -m app.embedding_setup
```

If using the existing virtual environment, replace `python` in these commands and
the startup commands below with `.\.venv\Scripts\python.exe`. Creating a new
virtual environment is optional. Model setup downloads pinned public assets;
embedding inference then runs locally on CPU and requires no model API key.

### 2. Configure ignored `backend/.env`

Use [backend/.env.example](backend/.env.example) as the non-secret template.
Root `.env` is also supported; `backend/.env` takes precedence over it, and shell
variables take precedence over both. Never copy real credentials into this README,
client configuration, screenshots or commits.

| Setting | Purpose |
| --- | --- |
| `SWITCH_BRIDGE_URL` | Switch agent API base URL, including HTTPS for a remote/ngrok installation. This is not the local FastAPI URL. |
| `SWITCH_AGENT_ID`, `SWITCH_AGENT_API_KEY`, `SWITCH_AI_AGENT_NAME` | Existing Codex identity and registered name (`codex-discord` in our demo). |
| `SWITCH_REQUESTER_AGENT_ID`, `SWITCH_REQUESTER_API_KEY` | Existing non-AI client (`docket` in our demo); distinct from Codex. |
| `SWITCH_AI_ROOM_ID`, `SWITCH_AI_SENDER_ID` | Private response-room UUID and exact Codex sender/participant ID. |
| `SWITCH_TENANT_ID`, `SWITCH_ROOM_IDS` | Tenant UUID and comma-separated input Switch room UUIDs. |
| `SWITCH_BACKEND_URL` | Local listener-to-FastAPI address. The commands below use `http://127.0.0.1:8001`. |
| `SWITCH_INGEST_TOKEN` | Private listener/API credential. Both processes must use the same value. |
| `SWITCH_ADAPTER_TOKEN` | Separate credential required for document questions and advanced chat actions. At least 32 characters; different from ingest, admin and database credentials. Both processes must match. |
| `SWITCH_DOCUMENT_ADAPTER_TOKEN` | Dedicated upload-consent adapter credential. Configure separately from the general adapter token. |
| `SWITCH_DOCUMENT_DISCORD_BRIDGE_IDS` | Exact Switch Discord bridge IDs for attachment routing; not Discord server/channel IDs. |
| `SWITCH_DOCUMENT_BOT_SENDER_ID` | Exact non-AI sender/participant ID used to suppress its own messages; not its display name. |
| `SUPABASE_URL`, `SUPABASE_SECRET_KEY` | Cloud Supabase project URL and backend secret key. `SUPABASE_SERVICE_ROLE_KEY` is also accepted; prefer configuring one key source. No JWKS URL is needed for this server-key storage path. |
| `DATABASE_ENGINE` | `supabase` for the cloud workflow. A blank value auto-selects Supabase when its configuration is present. SQLite is optional and does not provide full document/lifecycle parity. |
| `ADMIN_API_TOKEN` | Separate operator credential for protected database-status and record endpoints. It is not a Supabase-issued key or a human login. |
| `COMMITMENT_TIMEZONE` | Confirmed shared IANA timezone for relative deadlines and reminder policies. Some reminder paths default to UTC if it is blank. |

For document upload and questions, enable `DOCUMENT_INGESTION_ENABLED`,
`SWITCH_DOCUMENT_ATTACHMENTS_ENABLED`, `LOCAL_EMBEDDINGS_ENABLED` and
`SWITCH_REPLY_TO_DISCORD` after database/room setup. Template feature defaults are
conservative; they are not a copy of our private deployment's enabled flags.
`AUTOMATIC_COMMITMENTS_ENABLED`, `REMINDERS_ENABLED`, `REMINDER_DELIVERY_ENABLED`
and `PERSON_PROFILES_ENABLED` control separate features; room policies and deployed
schema also matter. Reminder lead time is configured with `REMINDER_LEAD_TIME`,
`REMINDER_LEAD_UNIT` and the equivalent `REMINDER_LEAD_MINUTES` value.

This pipeline does not use `GROK_API_KEY` or `SUPABASE_PUBLISHABLE_KEY` from the
backend environment. Model authentication belongs to Codex. Do not replace or
rotate working credentials merely to follow the example file.

### 3. Prepare the database

The consolidated [initial schema](supabase/migrations/20261104000100_initial_schema.sql)
includes relational memory, vector/embedding jobs, commitments, reminders and
versioned documents. **It resets project tables: use it only for a fresh/disposable
database. Never replay it or blindly run `db push` against existing hosted data.**
An existing project needs its schema checked and any upgrade reviewed separately.
Historical SQL under `supabase/migrations/history/` is not a fresh-install chain.
See [database setup](supabase/README.md) and [deployment guidance](supabase/DEPLOYMENT_PROPOSAL.md).

### 4. Start FastAPI and the listener

Set `SWITCH_BACKEND_URL=http://127.0.0.1:8001` in your ignored `.env` first.
From `backend/`, terminal 1:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

From `backend/`, terminal 2:

```powershell
python -m app.switch_listener --check
python -m app.switch_listener
```

The check should report API authentication, Switch connectivity and room membership
success. Leave both processes running. Restart both after changing adapter settings.
Do not start another listener with the same identity on a teammate's PC.

Open [the viewer](http://127.0.0.1:8001/) or [API docs](http://127.0.0.1:8001/docs).
`GET /health` checks API liveness, not database or AI readiness. The viewer feed and
`/messages`/`/ai/requests` use `X-Ingest-Token`; protected record/status routes use
`X-Admin-Token`. These are development/server credentials, not individual-user auth.

**Keep this backend on loopback.** This checkout includes browser credential/settings
and storage controls whose routes lack independent authentication. The feed's token
does not protect those controls. Do not expose this FastAPI server via ngrok or a
public deployment until those routes are secured. The remote Switch server is a
separate service and does not require exposing local FastAPI.

## Try the workflow

In the configured Discord channel, mention the registered client (currently `docket`):

```text
@docket I will finish the API documentation by Friday.
@docket What documents are saved in this room?
@docket Explain what is written in the FYP proposal document.
```

For a document, upload a supported file, wait for the save prompt, then have the
**original uploader** reply `yes` or `save` (`no` or `skip` declines). Multiple pending
uploads may require a filename. Accepted documents are parsed, versioned, chunked,
embedded and published before the saved confirmation. Questions use published
source evidence and validated stored-text-line citations.

Supported: Markdown, UTF-8 text, text-based PDF, DOCX and PPTX. Defaults: 8 MiB per
file, 256 KiB extracted text, four files per upload. Scanned PDF OCR, legacy Office
and macro-enabled files are unsupported. Summary replies sample at most eight
passages and disclose partial coverage. Document selections expire after 15 minutes
or API restart; same-sender follow-ups can continue within an active exchange.

New listener processes skip messages sent before connection. Transient reconnects
resume the current process's acknowledged cursor. Already accepted ingestion and
confirmed document jobs recover from durable state; do not delete `.local/` to
skip messages. The local reply watermark can also suppress older messages.

## How messages become searchable knowledge

The listener authenticates to FastAPI and supplies the configured tenant, source
room, sender, message ID and available platform metadata. Eligible messages enter a
durable journal before acknowledgement. **Accepted delivery does not mean that
cloud storage or search indexing has finished.**

Codex proposes structured output; FastAPI validates it before executing database work:

| AI action | Backend behavior |
| --- | --- |
| `store_only` | Save extracted facts, decisions or commitments with source evidence. |
| `query_only` | Retrieve saved information and synthesize an answer from evidence. |
| `store_and_query` | Save useful information, then retrieve context for the question. |
| `none` | No knowledge write/lookup; an addressed message may still receive a conversational reply. |
| `clarify` | Return a clarification instead of executing an unsupported interpretation. |

Malformed or contradictory output fails explicitly rather than silently becoming
`none`. The atomic storage path saves source evidence and extracted records
together. Stable identities protect repeated deliveries from duplicating records.
Commitments and decisions generate their own searchable summaries; additional fact
memories should not duplicate them. AI extraction does not imply human approval;
lifecycle actions and reminder eligibility have separate state/policy rules.

The listener receives all events, and eligible ordinary messages can be evaluated
without a command. Background processing and permission to speak are separate.
Tagged requests and active same-sender document follow-ups use the application
reply path; the bot is not meant to answer every ordinary message.

### Records and document versions

`tenants` and `rooms` establish scope. `messages` retain source evidence and available
author, platform, channel, thread and timestamp metadata. `commitments` hold tasks,
ownership, deadlines and status; `decisions` hold decision and approval state.
`memories` hold source-linked searchable text, embeddings and indexing state.

Documents use `documents`, `document_versions`, `document_chunks` and
`document_imports`. An import creates a version and chunks with duplicate protection.
The worker embeds chunks before publication. During replacement, the previous
published version remains available until the replacement is ready. Failed imports
must not publish incomplete versions. Retrieval rechecks publication and scope
before delivering an answer.

Questions use extracted chunk text rather than downloading the original file again.
Citations refer to stored-text line ranges, not invented PDF pages or slide numbers.
Question answering does not automatically edit the original document.

### Local embeddings and retrieval

The CPU model is **`Supabase/gte-small`**, using pinned ONNX/tokenizer assets under
ignored `.local/models/`. Attention-mask mean pooling and L2 normalization produce
**384-dimensional** vectors. Queries and stored text use the same model pipeline.

Useful text persists before indexing. Embedding jobs use content hashes, claim
tokens, leases and bounded retries so edits invalidate old vectors and stale
workers cannot attach results to changed text. The model accepts at most **512
tokens**, including special tokens. Oversized standalone memories fail explicitly
rather than silently truncating; document ingestion has a separate chunking step.

Retrieval restricts candidates by tenant and authorized room. Document queries also
restrict publication/version and, when selected, document identity. Room-memory
retrieval includes source references and structured task/decision context. If local
embeddings are unavailable, room-memory retrieval can return a labelled keyword
fallback; document processing/search has its own feature requirements. Large-corpus
production performance has not been established by the project's local tests.

## HTTP interfaces

Send credentials in headers, never in URLs. These are backend/development interfaces,
not a replacement for individual-user authentication.

| Route | Access and purpose |
| --- | --- |
| `GET /health` | Public API liveness only. |
| `POST /messages` | `X-Ingest-Token`; receive a scoped message. The listener also supplies `X-Adapter-Token` for trusted chat workflows. |
| `GET /messages/recent`, `GET /messages/status` | Ingest token; bounded previews and latest-message status. |
| `POST /ai/requests`, `GET /ai/requests` | Ingest token; queue a question or inspect recent AI results. |
| `POST /messages/document-attachments` | Ingest plus `X-Document-Adapter-Token`; upload events and consent. |
| `GET /api/v1/supabase/status` | `X-Admin-Token`; database connection/status check. |
| `GET /api/v1/messages`, `/api/v1/commitments`, `/api/v1/decisions`, `/api/v1/memories` | Admin token; inspect stored records. |
| `/api/v1/documents` | Admin-protected imports, search, document/version/chunk reads and archiving; document feature and room scope required. |
| `GET /api/v1/document-attachments/{request_ref}` | Admin token; inspect upload/import status. |
| `/api/v1/tenants/{tenant_id}/rooms/{room_id}/commitments` | Scoped lifecycle reads/actions require trusted individual identity and authorization. |

`POST /ai/requests` accepts exactly one question source. Example body (fixture ID,
no credentials):

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "question": "What did we decide about the API framework?"
}
```

Alternatively provide `source_message_ref` instead of `question`. Submission returns
HTTP `202` with a queued result. Repeated ID/content reuses a result; conflicting
content returns `409`. Result states are `queued`, `waiting`, `answered` and `failed`.
Viewer-only questions/results are process-local, unlike durable accepted chat jobs.

Missing credentials return `401`, invalid scope `403`, invalid payload `422`, a full
queue `429`, and missing required configuration often `503`. Check the route's
contract instead of treating every error as a model failure.

### Browser viewer

Open `/`, enter the ingest credential in the masked feed-access field and unlock
the feed. The authenticated `/viewer/ws` socket delivers preview/result updates.
The viewer retains at most 100 in-memory previews; restarting FastAPI clears that
preview history. Ask Codex submits through FastAPI and the private AI room.

The Live indicator proves only that the viewer socket is connected, not that
Discord delivery, cloud writes or the model are ready. The separate settings and
storage controls have the local-only limitation described in the setup instructions.

## Durable state and recovery

| Local state | Purpose |
| --- | --- |
| `.local/ingestion.sqlite3` | Accepted jobs, replay identities and reply fingerprints; needed even with Supabase as the main database. |
| `.local/document_attachments.sqlite3` | Uploader consent, import coordination and document-reply recovery. |
| `.local/switch-listener.json` | Listener connection/cursor information; fresh processes still start at the live head. |
| `.local/last_replied_message.txt` | Reply watermark; messages at or before it can be suppressed. |
| `.local/models/` | Downloaded local embedding assets. |

Chat recovery allows at most three automatic attempts with a 30-second failure
backoff. Completed jobs clear payloads while retaining replay identities. A
validated extraction can be reused after a storage failure. Document and embedding
workers retain their own durable processing states. Keep `.local/` private and
persistent; do not share one journal between independent running API processes.

From `backend/`, inspect jobs with:

```powershell
python -m app.ingestion_jobs
```

For an exhausted job, stop FastAPI, investigate the cause, then run
`python -m app.ingestion_jobs --retry MESSAGE_REF` with its 24-character reference.
Restart FastAPI afterward. Clearing journals or advancing the watermark can
suppress work rather than repair it. Already accepted document imports should
finish through their existing worker state.

## Verification and troubleshooting

```powershell
# From backend/
python -m pytest -q
python -m pip check
python -m app.switch_listener --status
```

`--status` shows the latest redacted sender/message; avoid sharing private output.
Look for matching `API acknowledged message` references in listener logs and
processing results in FastAPI. `FEATURE_LOGS_ENABLED=true` enables content-free
feature events; leave `LOG_DISCORD_MESSAGES=false` unless intentionally debugging
redacted message previews. Do not enable HTTP/WebSocket wire logging.

For real local-model checks after installing the assets:

```powershell
$env:RUN_LOCAL_MODEL_TESTS = '1'
python -m pytest -q tests/test_local_embeddings.py
Remove-Item Env:RUN_LOCAL_MODEL_TESTS
```

The default backend suite isolates developer credentials and mocks external HTTP
unless a test supplies its own transport. Passing it does not prove hosted schema
readiness or real chat delivery. Disposable SQL tests are separate: from the repo
root, enter `supabase/tests`, run `npm ci`, then `npm test`. Additional package
scripts include `test:documents`, `test:commitments`, `test:chat` and `test:reminders`.
They target a disposable test engine, not the team's hosted project. Live fixtures
need deliberate scope, approval where applicable and cleanup.

- **No messages:** confirm the Discord channel maps to an allowed Switch room, the
  bridge receives messages, and exactly one listener owns the connection.
- **Document identity error:** match `SWITCH_ADAPTER_TOKEN` on listener and backend;
  the upload token is a different setting. Restart both processes.
- **HTTP 429 / slow answers:** one worker serializes up to four running/queued jobs.
  Background chat and multiple AI calls can delay tagged questions. Failed jobs
  have bounded retries with a 30-second delay. A local queue 429 is not proof that
  the model's account allowance is exhausted.
- **Wrong port / `No module named app`:** run from `backend/` and match the server
  port to `SWITCH_BACKEND_URL`. The code's unset default is 8000; this guide/template
  explicitly use 8001.

Implementation, database deployment and live end-to-end verification are separate.
Do not infer that every module is active from `/health` or from a past test count.
For detailed contracts and historical verification, see [architecture.md](architecture.md),
[document ingestion](supabase/DISCORD_DOCUMENT_INGESTION.md),
[document questions](supabase/DOCUMENT_CHAT.md), [commitment capture](SIMPLE_COMMITMENT_CAPTURE.md)
and [reminders](REMINDERS.md).
