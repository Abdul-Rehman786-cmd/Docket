# Operations and module guide

PostgreSQL migration layout: all module definitions now live in
`supabase/migrations/20261104000100_initial_schema.sql`. Older module filenames
and checksums below are historical provenance, recoverable from Git history;
SQL links point to the consolidated baseline, not those original bytes. Keep the
separate SQLite upgrade and test/audit SQL; never replay the reset on hosted data.

Use [README](../README.md) to start and [architecture.md](../architecture.md) for
the main architecture reference. This guide consolidates setup, API contracts,
feature policies and troubleshooting. It introduces no deployment approval.

Sections preserve the original module contracts and their dated rollout notes.
Simple tagged capture is the current creation path; advanced lifecycle and
reminders have separate gates. Earlier verification counts or deployment blockers
are implementation records, not a new live-status check. Consult
[verification history](VERIFICATION.md) and recheck the configured environment.
Paths and commands use the repository root or their stated working directory,
not this docs folder. Former source names below explain historical relative paths.

## Contents

- [Application setup and troubleshooting](#application-reference)
- [Tagged commitment capture](#commitment-capture)
- [Commitment lifecycle and HTTP contract](#commitment-lifecycle)
- [Open commitment actions](#open-commitments)
- [Automatic lifecycle policy and evidence](#automatic-commitments)
- [Reminder planning and delivery](#reminders)
- [Person profiles](#person-profiles)
- [Feature logs](#feature-logging)
- [Document ingestion and operator APIs](#document-ingestion)
- [Chat uploads and file-format support](#document-attachments)
- [Document discovery and questions](#document-chat)

<a id="application-reference"></a>
## Application setup and troubleshooting

Consolidated from `README.md`.

<a id="application-reference-team-memory-â-clean-foundation"></a>
### Team Memory â€” Clean Foundation

A project scaffold for **FastAPI + Supabase + React Native (Expo)** with a Switch listener for verifying Discord-to-API message delivery.

The implemented flow receives Discord messages through Switch, journals human input durably, displays bounded redacted previews, and evaluates useful information through the existing private Codex runtime. Atomic storage and versioned embedding jobs are defined in the single consolidated development baseline. The hosted non-destructive upgrade is prepared for review and approval; never replay the baseline reset on the hosted database. FastAPI and the Switch listener run as separate processes. See [architecture.md](../architecture.md) for runtime boundaries, recovery, storage, retrieval and reply behavior.

<a id="application-reference-database-setup-one-script"></a>
#### Database setup — one script

For a **fresh database**, run only
[`supabase/migrations/20261104000100_initial_schema.sql`](../supabase/migrations/20261104000100_initial_schema.sql).
It is standalone: core storage/embeddings, commitments and chat, reminders,
documents (all five formats), simple capture and person profiles are included in
one atomic transaction. No additional module SQL scripts are needed afterward.

Older module files are retained in `supabase/migrations/history/` for historical
review only. Do not run them after the original script. SQLite has a separate
migration dialect and is unchanged.

**The original script resets Team Memory tables. Never run it on an existing
hosted database or real team data.** Existing installations keep their migration
history and use separately reviewed additive upgrades. This consolidation did
not change the hosted database. See [single-script setup](DATABASE.md#database-setup).

<a id="application-reference-listener-startup-policy"></a>
#### Listener startup policy

Each new listener process starts at Switch's current stream head using a fresh
connection ID. It skips events from before that connection opens, including
messages and save/skip commands sent while the listener was offline. Send an
upload or confirmation again after the listener reports connected if necessary.
Transient reconnects within the same process resume from its acknowledged cursor,
with duplicate protection. Restarting FastAPI alone does not reset a running
listener's cursor. Already accepted API jobs, confirmed document imports,
embedding jobs and document replies recover independently from durable storage.
Keep `backend/.local/`; do not clear its journals to skip chat history.

<a id="application-reference-viewer-administration-and-maintenance"></a>
#### Viewer administration and maintenance

The Settings panel supports credential and server-setting updates after unlocking
with `ADMIN_API_TOKEN`. This token stays only in tab memory, and is cleared on
save, lock, navigation away and page exit. The message-viewer/ingest token does
not grant administrative access.

Authenticated `GET /viewer/env` returns documented non-secret settings and only
configured/not-configured flags for credentials. Secret inputs start blank:
leave blank to keep an existing credential, or enter a replacement. Authenticated
`POST /viewer/env` validates documented setting names, rejects multiline values,
and atomically writes quoted values to ignored `backend/.env`. Saved keys are
never returned, exported or cached in the browser. Older browser credential caches
are removed on page load. Set the initial admin token locally before using the UI.
Restart affected services after changing connection settings or startup-only flags.

Reminder status/configuration, database migration, storage status, retention and
SQLite quota APIs under `/viewer/` also require `X-Admin-Token`. Reminder policy
synchronization uses its dedicated configuration API, rather than arbitrary edits
to the environment fields.

AI orchestration separates document handling, validated decision normalization,
storage/confirmation, retrieval and reply delivery. Recovery, scope checks and
storage-before-reply ordering are unchanged. Meeting confirmations share timezone
formatting; ordinary feature logs do not include raw model output.

<a id="application-reference-structure"></a>
#### Structure

Optional source-backed person profiles are implemented and disabled by
default (`PERSON_PROFILES_ENABLED=false`). Profiles use an internal UUID and unique
tenant/room/Switch-sender identity, with atomic claim/evidence storage in Supabase
and SQLite, explicit corrections and cited retrieval. Profile HTTP reads remain
closed until verified individual-user authentication is integrated. The additive
profile migration was applied to the configured Supabase project with user approval;
its tables, backend-only grants and API exposure were verified. Profiles are enabled
in the active local backend configuration; the corrected sender-specific query
path still needs fresh live messages from distinct users. Future remote migrations require separate approval. See
[PERSON_PROFILES.md](OPERATIONS.md#person-profiles) for supported statements and verification.

Personal profile questions now use the incoming sender's ID within the current
tenant and room, bypassing shared-room search. An unknown user receives a no-saved-
profile response instead of someone else's preference. Confirmation drafts are
sender-scoped too. This correction needs no database migration; reload or restart
the backend and restage any pending save from before the change.

Content-free feature logs are enabled with `FEATURE_LOGS_ENABLED=true` (default). Restart the API/listener and look for `[feature]` events for disabled gates, queued work, persisted actions, replay/conflicts, embeddings and reminder outcomes. See [FEATURE_LOGGING.md](OPERATIONS.md#feature-logging) for event meanings and commands. Logs do not enable business features or prove unverified hosted/Discord delivery.


Reminder planning, a durable outbox (for Supabase and local SQLite), and a Switch notification dispatcher are implemented. Administrators can enable room reminders and set advance notice through `POST /viewer/reminders/configure` with `X-Admin-Token`; the service synchronizes `.env` and room reminder policies. The AI identifies meetings and schedules, routes upcoming meeting reminders directly to the rooms, and dispatches them at the user-configured lead time before the meeting.


The commitment creation flow is **tag the bot → extract → save atomically in Supabase → embed the linked memory → scoped semantic search**. It uses Switch's existing `addressed` metadata and the delivered sender ID, with no registration, membership/mapping lookup, role setup or human/bot classification. Unresolved deadlines retain their wording with no invented timestamp. Confirmation follows durable cloud storage and says indexing is pending. Creation does not require lifecycle/reminder activation. See [SIMPLE_COMMITMENT_CAPTURE.md](OPERATIONS.md#commitment-capture) for the current contract, verification and deployment steps. Existing lifecycle/history/reminder services and protected manual HTTP routes remain separately controlled.


Commitment lifecycle management is implemented separately with scoped reads/actions, audit history, versions and atomic idempotency. New routes fail closed until trusted individual-user authentication is supplied. The additive migration is prepared for review, not deployed. See [COMMITMENT_MANAGEMENT.md](OPERATIONS.md#commitment-lifecycle) for contracts and deployment dependencies.


The original versioned Markdown/text document ingestion is deployed and enabled in the configured
backend. It adds restricted admin imports, durable version/chunk jobs, automatic
publication and document search/citations. Hosted API/vector/failure checks passed
and isolated fixtures were cleaned. New installations default to disabled until
their own approved migration and verification. See
[document contracts, API and deployment](OPERATIONS.md#document-ingestion).
Current results are in [hosted document verification](VERIFICATION.md#document-deployment).

The Discord document revision supports channel-only replies, uploader-only
`yes`/`save` or `no`/`skip` consent, and filename clarification when several uploads
are pending. Markdown/text, text-based PDF, DOCX and PPTX reuse the versioning,
embedding and publication pipeline. Defaults are 8 MiB uploads and 256 KiB extracted
text; existing lower local overrides remain unchanged. Binary parsing is isolated
and bounded. Internal references remain in operator APIs/logs, never new chat replies.
No registration or personal uploader allowlist is required.

Document discovery and questions are now implemented locally through the same
Discord/Switch message pipeline. Mention the bot with `List our documents`,
`Find the presentation about our launch`, or `What does planning.pdf say about
the deadline?`. Choose a numbered result; follow with `Summarize it` or `Who is
responsible?`. Selection is sender/room/tenant-scoped and expires after 15 minutes
or API restart. Replies cite stored text lines; summaries explicitly disclose
partial coverage. No new migration or dependency is required. Operator rollout
and live verification remain pending. See [document chat setup and limits](OPERATIONS.md#document-chat).

After an addressed document exchange starts, the same sender can continue without
another mention while that room-scoped conversation is active, including a choice
plus question such as `1 can you explain whats in it`. The AI interprets genuine
follow-ups; unrelated chatter receives no unmentioned document reply. Expiry or
API restart requires a new mentioned request to establish context again.

The existing private AI gateway now interprets document intent by meaning,
including inventory requests and selected-document follow-ups. Routing no longer
depends on a catalog of matching phrases. The backend validates the structured
intent and performs authorized metadata reads or cited retrieval; the AI does not
invent an inventory or choose access scope. Ordinary document chat requires the general listener
adapter credential, separately from the upload-consent credential. Startup logs
report `general_adapter_ready` to distinguish the two; missing chat verification
returns a setup explanation rather than querying unrelated room memories.

The revision is verified locally. The approved format migration is now applied to
Supabase and backend dependencies are installed in the project venv. Shared service
reload and live binary verification remain outstanding; deployment settings were
not changed and no live test was performed. See
[document setup, limits and rollout](OPERATIONS.md#document-attachments).
The former browser environment editor now returns `410`; edit ignored backend
configuration locally so privileged credentials never reach that viewer.

- `architecture.md`: design and boundaries.
- `AGENTS.md`: mandatory AI development and security rules.
- `backend/`: FastAPI MVC-inspired layers and tests.
- `supabase/migrations/`: ALL versioned SQL schema migrations.
- `mobile/`: Expo TypeScript starter screen (backend health-check only).

<a id="application-reference-1-backend-setup-windows-powershell"></a>
#### 1. Backend setup (Windows PowerShell)

The FastAPI foundation runs independently: `/health`, `/docs`, and `/openapi.json` do not require database or LLM credentials. The directories for controllers, models, services, repositories, and configuration are already in place.

From the repository root, install the backend and test dependencies into a local virtual environment:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Use `python`, since the optional Windows `py` launcher may not be installed. If `.venv` already exists, skip creating it. These commands use the virtual environment's interpreter directly, so PowerShell activation and execution-policy changes are unnecessary. Keep the server terminal open; press `Ctrl+C` to stop it. The launch command follows the [FastAPI server guide](https://fastapi.tiangolo.com/deployment/manually/).

Open `http://127.0.0.1:8000/` for the browser message viewer, or `http://127.0.0.1:8000/docs` for Swagger. Try `GET /health`; it returns `{"status":"ok"}`. `/health` checks that the API is running, not database connectivity. You can also check it from another PowerShell terminal:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/health
```

The `/api/v1/supabase/status`, `/api/v1/messages`, `/api/v1/commitments`, `/api/v1/decisions`, and `/api/v1/memories` endpoints require the `X-Admin-Token` header. Before using them, set `ADMIN_API_TOKEN` in the ignored `backend/.env` to a unique private value and configure `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Without authentication, they return `401`; with valid authentication but no database configuration, they return `503`. When `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are configured in `backend/.env`, `POST /ai/requests` executes the LLM's 4-way database decision (`store_only`, `query_only`, `none`, or `store_and_query`) against Supabase. **This admin token is for local/server-side testing only, never for mobile.** Leave Supabase settings blank for the initial health-check setup.

The diagram's `/ask`, `/docs/sync`, `/docs/apply-update`, and `/memories` routes are planned features and are not implemented in this foundation.

Run tests from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

<a id="application-reference-receive-discord-messages-through-switch"></a>
##### Receive Discord messages through Switch

```text
Discord -> Switch room -> Python WebSocket listener -> authenticated POST /messages
                                                             |
                                                             v
                                                   durable jobs + bounded previews
                                                             ^
                                                             |
Browser at / <- authenticated /viewer/ws live updates
```

**Keep both FastAPI and exactly one listener running.** Starting or reloading FastAPI does not start the listener. The browser's **Live** indicator confirms the authenticated viewer socket only; `/health` confirms API liveness only. Neither verifies that Discord messages are being delivered.

Use an existing Switch agent that permits a custom WebSocket client and is assigned to the Discord room. The listener does not register an agent or post replies; FastAPI can post policy-approved answers through the separate Switch client. Configure these settings in the ignored `backend/.env`; the placeholder-only `.env.example` lists them. Existing credentials should be preserved.

| Variable | Purpose |
| --- | --- |
| `SWITCH_BRIDGE_URL` | Switch bridge HTTP(S) base URL, without credentials or query parameters; this is usually different from the Gateway URL. |
| `SWITCH_AGENT_ID` | Existing custom agent ID. |
| `SWITCH_AGENT_API_KEY` | Private key belonging to that agent. |
| `SWITCH_INGEST_TOKEN` | Separate private random token shared by the listener and FastAPI. It grants no access to commitment/decision reads. |
| `SWITCH_TENANT_ID` | UUID for the single demo tenant. |
| `SWITCH_ROOM_IDS` | Comma-separated Switch room IDs mapped to that tenant. Unknown rooms are ignored. |
| `SWITCH_BACKEND_URL` | FastAPI base URL; defaults to `http://127.0.0.1:8000`. |
| `SWITCH_REPLY_TO_DISCORD` | When `true` (default), responses generated by FastAPI (for questions, memory queries, or addressed messages) are posted back to Switch and relayed to Discord. |

All settings load from `backend/.env`, independent of the current working directory. Keep the API running in the first terminal. From `backend/`, use a second PowerShell terminal:

```powershell
.\.venv\Scripts\python.exe -m app.switch_listener --check
.\.venv\Scripts\python.exe -m app.switch_listener
```

`--check` verifies API authentication, the Switch WebSocket, and configured room membership, then exits without claiming rooms or forwarding messages. The second command keeps listening; press `Ctrl+C` to stop. Run one listener for this agent: it declares the configured rooms at connection open for reliable replay, which takes over those room claims from any other client of the same agent. If Switch is on another machine, it does not need to call your PC: the Python listener opens the outbound Switch connection and forwards to the local API.

Send a harmless marker such as `delivery-test-001` in the configured Discord channel. A delivered message produces matching `ref=...` entries in the API and listener terminals:

```text
Switch message received ref=<receipt reference> duplicate=False
API acknowledged message ref=<same receipt reference> duplicate=False
```

To see messages **in the browser**, open `http://127.0.0.1:8000/`, enter your local `SWITCH_INGEST_TOKEN` in the masked access-token field, and select **Open feed**. Each unlocked tab opens its own authenticated WebSocket at `/viewer/ws` and displays each sender's display name and message text, newest first. Names and text are redacted before retention; the latest 100 messages are kept in memory. Missing or blank names appear as `Unknown sender`; participant IDs are never parsed to invent a username. Keep FastAPI and the listener running, then send a new Discord message. Restart the listener after an update so it forwards Switch's `sender_name` field. Text received before this viewer was added is not available. **Lock** closes the socket and clears the token, pending requests, and displayed messages from the tab. The token is sent in the first socket authentication frame, and in the authentication header for HTTP question submissions. It is not embedded in the page, stored in browser storage, or included in URLs. The page shell is public; `GET /messages/recent` requires authentication and filters previews by the configured tenant and room allow-list. Responses are marked `no-store`.

To also print the incoming sender and message in the **FastAPI terminal**, set `LOG_DISCORD_MESSAGES=true` in the ignored `backend/.env` and restart the API. New deliveries print a line such as `Discord message ref=... sender="Ali" text="Hello from Discord!"`. This local debug option defaults to `false`; configured credential values, common credential formats, and URLs are masked, control characters are escaped, and previews are limited to 2,000 characters. Set it back to `false` to stop printing text. Browser previews work independently of this terminal logging option.

Default delivery logs use receipt metadata to correlate API and listener activity. `LOG_LEVEL` controls application diagnostics (`INFO` by default; `DEBUG` adds matched route names, status codes, and timings without request contents). It does not enable HTTP or WebSocket wire logging. Keep HTTP and WebSocket wire-level debugging disabled because those traces can expose authentication headers.

Check the latest sender and message in a third terminal, also from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m app.switch_listener --status
```

The protected `GET /messages/status` and the `--status` command return the latest authorized message as `{"sender_name":"Ali","message":"Hello team"}`, replacing the previous counter response. Both values are `null` until a message arrives. Preview reads and latest-message status filter by tenant and allowed rooms. The CLI reads the token internally, so it never has to appear in terminal commands. All `/messages` endpoints require `X-Ingest-Token`. `POST /messages` also checks the tenant and room mapping; an invalid mapping returns `403`. Payload validation returns a generic `422` without reflecting chat text. Text logging runs only after authentication and authorization succeed.

<a id="application-reference-ask-the-existing-switch-ai-and-receive-answers-only-in-the-viewer"></a>
##### Ask the existing Switch AI and receive answers only in the viewer

The viewer now has **Ask Codex** beside each received message and a question box above the feed. Select a message or enter a question. The answer appears in the **Ask Codex** panel automatically. This reuses the existing `codex-discord` AI and its model authentication; FastAPI needs no model API key. Questions go through a separate non-AI Switch client to an internal room with no Discord bridge. Eligible incoming chat also receives automatic AI evaluation for the database decision.

For a new local installation:

In **Switch Console**, ensure any session binding `codex-discord` to an input room is stopped, leaving only the session for `team-memory-server-responses` running. `team-memory-client` must remain registered and running as the input listener.



1. Preserve `SWITCH_AGENT_ID` and `SWITCH_AGENT_API_KEY` for the existing Codex agent. Set `SWITCH_AI_AGENT_NAME` to its registered name (default `codex-discord`). Keep its Switch Console or managed runtime available.
2. Add `SWITCH_REGISTRATION_TOKEN` to ignored `backend/.env`, using a Switch registration key created under the **same owner** as Codex. Do not paste credentials into commands, chat, or screenshots.
3. From `backend/`, run `.\.venv\Scripts\python.exe -m app.switch_setup`. It registers `team-memory-client` once, saves its returned credentials directly into ignored `.env`, creates the private internal response room, and keeps only the non-AI client in the configured input rooms. It removes `codex-discord` from those input rooms so incoming Discord messages cannot trigger independent AI replies there. Other agents and the Discord bridges are preserved. A harmless private readiness request learns Codex's exact opaque sender ID. No existing agent is overwritten and no addressing permissions are widened. The setup is already completed for the current workspace.
4. Restart FastAPI after setup. Stop the previous listener and start exactly one listener again so it uses the new non-AI identity. Refresh and unlock the viewer at `http://127.0.0.1:8000/`.

Setup writes `SWITCH_REQUESTER_AGENT_ID`, `SWITCH_REQUESTER_API_KEY`, `SWITCH_AI_ROOM_ID`, and `SWITCH_AI_SENDER_ID` in `.env`. `SWITCH_AI_TIMEOUT_SECONDS` defaults to 180 seconds. Once the returned credentials are saved, subsequent setup runs reuse them and the room. A registration conflict stops rather than rotating credentials; restore the saved client credentials if needed. If readiness times out, start Codex's existing runtime and rerun setup. The registration key is used only by the setup command, never by HTTP requests from the viewer.

`POST /ai/requests` accepts `{request_id, question}` or `{request_id, source_message_ref}` and returns `202` with a queued result. Use a UUID for `request_id`; repeating the same ID/question reuses that result, while reusing it for different content returns `409`. `GET /ai/requests` remains available to read the latest redacted results; the browser receives changes over its socket. Both require `X-Ingest-Token`, enforce the configured scope, and return `Cache-Control: no-store`. Client callers cannot choose a destination room or an AI identity. Invalid or expired source-message references return `404`. These are local development interfaces, not mobile user authentication.

The API serializes AI execution and permits four running/queued jobs. Automatic incoming human messages are committed to ignored `backend/.local/ingestion.sqlite3` before acknowledgement, then recovered after restart. Keep this directory on private persistent storage and run one API worker. Completed jobs clear their redacted payload and retain replay identities/counts. Failed jobs retain redacted evidence; retries reuse validated extraction and stop after three attempts, with thirty-second backoff. Messages accepted before AI setup remain durably pending. Explicit viewer questions and the latest 100 results/aliases remain in memory; viewer-only work is not recovered after restart.

The four-way parser requires valid, consistent structured output. Invalid output is an explicit extraction failure, never `none`. Commitments/decisions generate one corresponding memory each; additional memories contain facts only. Relative deadlines remain unresolved unless date/timezone evidence supports an exact timestamp. Newly extracted records remain pending human approval. Useful `store_only` results and ordinary chatter remain silent in Discord.

Every request verifies that the internal room has no bridge or external channel and contains only Codex and the client. It also checks Codex's reported sessions for live input-room bindings, and checks that every configured input room contains the listener without Codex. Missing runtime evidence, an active input-room session, or re-invitation causes a diagnostic before another question is posted. Stop the Discord-attached Codex session in Switch Console first, then rerun `python -m app.switch_setup`. Setup refuses to report success while an active input-room session is reported. Requests never fall back to an input room. Codex remains available in the private response room. Ask questions using the viewer's question box or **Ask Codex about this message**. Keep the internal room unbridged. The listener uses `SWITCH_REQUESTER_*`, preserving `SWITCH_AGENT_*` for the actual AI. Every listener process restart starts at the current stream head; it does not replay offline events.

Automated tests cover sender/thread correlation, responsive heartbeats, authentication, scope isolation, redaction, request deduplication, and setup recovery.

<a id="application-reference-observe-other-messages-and-agent-replies"></a>
##### Observe other messages and agent replies

`GET /messages/recent` now adds `sender_kind` (`ai` or `message`) and nullable `thread_ref` to each preview. `thread_ref` is a hash of the Switch thread identifier scoped by tenant and room, using the same reference format as messages. When it matches a retained message, the viewer displays that message as thread context. Otherwise it says that earlier context is unavailable. It never guesses which question an unthreaded answer belongs to, and a thread reference identifies conversation context rather than an exact question/answer pair. Raw participant and thread IDs are not returned by the feed. `/messages/status` retains its existing response shape and can return the latest message from either a person or an agent.

Messages in the existing feed have the same authorization, redaction, 2,000-character preview limit, 100-message retention, and restart behavior as before. `SWITCH_AI_SENDER_IDS` can additionally label other exact agent participant IDs; the learned `SWITCH_AI_SENDER_ID` is included automatically. The badge identifies a configured sender rather than proving that every post was generated by a model. These observed room messages are separate from the server-only request results above.

The transport and identity behavior follow the official Switch [event catalog and participant IDs](https://docs.switchagents.ai/switch-rooms/internals/agent-events), [agent operations](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol), and [room routing](https://docs.switchagents.ai/switch-rooms/internals/rooms-and-resources). For an unavailable responder, see [agent troubleshooting](https://docs.switchagents.ai/switch-rooms/using/agent-not-answering).

**Receipt means durable local acceptance, not successful Supabase storage.** Human source text is credential-redacted before journaling or prompting. The viewer keeps at most 100 previews, capped at 2,000 characters; ingestion retains the admitted redacted source up to 64,000 characters. Preview history resets on API restart, while jobs and replay identities survive. The listener saves progress only after acknowledgement and keeps heartbeats independent of processing. Do not delete its checkpoint or the ingestion journal as a routine recovery step.

Inspect safe processing state from `backend/`:

```powershell
python -m app.ingestion_jobs
```

For an exhausted job, stop the API, run `python -m app.ingestion_jobs --retry MESSAGE_REF`, and restart it. This resets attempts for the same job and preserves saved extraction/counts. Completed jobs cannot be reset. Removed/unauthorized rooms are not recovered. A disk failure returns a retryable ingestion error before acknowledgement.

When `SWITCH_REPLY_TO_DISCORD=true`, `AIService` maintains a persistent timestamp watermark in `backend/.local/last_replied_message.txt`. If the backend is restarted after an offline period and Switch replays unacknowledged messages, any message with a timestamp at or before the last replied watermark is recognized as an older/replayed message; its Discord reply and synthesis are skipped, preventing the bot from re-answering past questions.

If Switch refuses the connection, verify the bridge URL and agent key, and use a custom agent rather than a controller-managed agent. For Discord setup, confirm the Message Content Intent and room membership. See the official [Switch agent connection](https://docs.switchagents.ai/switch-rooms/internals/agent-connection) and [Discord setup](https://docs.switchagents.ai/switch-rooms/deploy/messaging-apps/discord) guides.

<a id="application-reference-troubleshoot-an-empty-incoming-messages-screen"></a>
##### Troubleshoot an empty Incoming messages screen

1. Confirm FastAPI is running. From `backend/`, run `.\.venv\Scripts\python.exe -m app.switch_listener --check` to verify API authentication, the Switch WebSocket, and configured room membership. This command exits after checking; it does not keep listening.
2. Confirm one listener for the agent is running. If none is running, start `.\.venv\Scripts\python.exe -m app.switch_listener` in a second terminal. Avoid starting another copy while a listener is active, because it can take over room claims.
3. Look for configured room coverage and `API acknowledged message ref=...` in listener output, with the matching `Switch message received ref=...` in the API terminal.
4. Open or refresh `http://127.0.0.1:8000/`, unlock the feed, and send a new harmless marker in the configured Discord channel.

| Result | Meaning / next check |
| --- | --- |
| `/health` returns `200`, but the feed is empty | Check the listener and delivery acknowledgements; API liveness alone does not confirm ingestion. |
| Authenticated `/messages/recent` returns `[]` | No authorized previews are currently retained. Check for an API restart or an inactive listener. |
| Message endpoint returns `401` | Check the local ingest token used by the browser/listener and API. |
| `POST /messages` returns `403` | Check the configured tenant and room allow-list. |
| Message endpoint returns `503` | Check ingestion configuration and API availability. |

The listener is a local process with no automatic service wrapper; ensure it is running in a terminal before testing Discord delivery.

<a id="application-reference-live-viewer-websocket"></a>
##### Live viewer WebSocket

Unlocking a tab opens `/viewer/ws`; each connection receives a backend-generated UUID, distinct from its token, tenant, and room IDs. The token authenticates the existing shared local feed; it does not identify a user or select a Discord server. No token is embedded in socket URLs or browser storage. Browser Origin must match the backend origin, query parameters are rejected, and the first frame must authenticate within five seconds:

```json
{"type":"authenticate","token":"YOUR_LOCAL_INGEST_TOKEN"}
```

After authentication, the server sends a `snapshot` containing `connection_id`, authorized `messages`, and `answers`. It then pushes `message_added`, `message_removed`, `ai_queued`, `ai_waiting`, `ai_answered`, `ai_failed`, and `ai_removed` events. Messages remain restricted to the configured tenant and room allow-list; AI results use the existing response scope. Question submission stays on authenticated `POST /ai/requests`.

The server sends an application-level `ping` about every ten seconds; the browser responds with `{"type":"pong"}`. Each outgoing event and heartbeat rechecks current credentials and scope. Token or scope changes close the connection; the user must unlock again. Refresh sends `{"type":"resync"}` for a fresh snapshot. After transient disconnection, the viewer clears stale data and reconnects with capped backoff (one to thirty seconds), reauthenticating and receiving a fresh snapshot. Reconnection creates a new UUID. A missed update outside retained history cannot be recovered. Lock and page exit close the tab's connection.

There are at most 64 simultaneous viewer connections, including unauthenticated handshakes, and at most 32 buffered events per connection. Slow clients are disconnected; send operations have a five-second deadline, and missing application pongs expire the connection. Keep one API worker: broadcasts and retained history are process-local. HTTPS deployments use `wss`; forward the public host and scheme correctly through the trusted proxy. Keep WebSocket frame-level debug logging disabled, since the first frame contains the local credential.

Backend socket tests run with the normal pytest suite. Offline JavaScript lifecycle tests use Node's built-in runner, from the repository root:

```powershell
node --test backend/tests/viewer_socket.test.cjs
```

<a id="application-reference-resource-lifecycle-and-overload-handling"></a>
##### Resource lifecycle and overload handling

FastAPI startup creates separate reusable asynchronous HTTP pools for Supabase and Switch. Shutdown stops worker/recovery tasks before closing pools. Automatic queued work remains recoverable in the journal. Explicit viewer questions remain ephemeral. Exactly one API worker and one listener are supported.

When configured AI processing has four running/queued jobs, new ingestion returns `429` with `Retry-After: 2` before durable admission. Already accepted duplicate deliveries are acknowledged. Viewer submissions share the same admission lock/capacity. Journal errors return `503`; the listener does not advance its checkpoint. Processing logs contain safe references, state, validated actions, stored counts and failure categories, without chat or model responses.

Database storage is a single backend-only transaction: source evidence and all extracted records succeed together, and only then is extraction marked completed. A source replay returns existing counts without altering approvals. A delivery failure after processing does not repeat storage; replies remain best effort, without a durable delivery outbox. Sender-ID and persisted reply-fingerprint guards prevent own-message loops. Room mappings must already exist and be enabled in Supabase; ingestion never creates/reassigns them.

Offline regressions mock Codex and database transports and use real disposable SQLite journals. See [storage verification](VERIFICATION.md#useful-storage) for live evidence, remaining gates and the exact integration test.

<a id="application-reference-2-supabase"></a>
#### 2. Supabase

Cloud connectivity has been checked through FastAPI's protected status route:
unauthenticated access and the ingest token are rejected; the separate admin
token returns `connected`. Connection success does not prove writes work. The
hosted atomic storage function is still missing, and one of two configured rooms
has no database mapping. See [current cloud storage findings](VERIFICATION.md#cloud-storage).

`SUPABASE_SERVICE_ROLE_KEY` accepts a backend `sb_secret_...` key or a legacy
service-role JWT. New secrets are sent in `apikey`; legacy JWTs also use bearer
authentication. `SUPABASE_SECRET_KEY` remains a supported compatibility alias;
`SUPABASE_ANON_KEY` is not a backend-secret alias. Keep the database credential,
`ADMIN_API_TOKEN`, and `SWITCH_INGEST_TOKEN` separate in ignored `backend/.env`.
The missing admin token and canonical database variable were configured locally
without exposing values or rotating the existing cloud/ingest credentials.

The smallest storage-only proposal is printed by `python -m app.schema_upgrade
--storage-only` from `backend/`. It installs only the missing atomic function and
backend execution grants, leaving tables, RLS, embeddings and existing data
unchanged. It still requires approval before remote application. After approval,
`python -m app.verify_cloud_storage --run-approved-fixture` exercises the real
authenticated FastAPI storage path and cloud writes with a clearly simulated
extraction, replay checks and exact-scope cleanup. It sends no Discord messages.
Useful-text storage does not require the larger embedding upgrade below.

The initial migration is a **destructive development reset**, not an incremental
upgrade. It recreates eight tables, including vector memories, and deletes their
existing rows. Do not rerun it against the database already configured through
the SQL Editor, or against real team data. SQL Editor changes do not automatically
record CLI migration history; reconcile that history before using `db push`.

The schema includes team/room mappings, room approval roles, message extraction
tracking, commitments, decisions, reminder deliveries, and `memories` with
384-dimensional `Supabase/gte-small` embeddings. The backend-only
`search_memories` function filters by team and room before ranking results.
Embeddings must be generated by the backend; SQL does not generate them. Editing
memory content clears its old vector automatically, and the backend must generate
a replacement before that memory appears in search. Keep commitment/decision
summaries synchronized with their structured records and
deactivate obsolete memories. FastAPI must authorize every requested room;
service-role database access bypasses RLS.

The only core migration is [20261104000100_initial_schema.sql](../supabase/migrations/20261104000100_initial_schema.sql). It includes atomic storage, durable versioned embedding state, lease/hash guarded completion, invalidation and authorized vector search. The earlier untracked additive migration was confirmed absent from the connected project's migration history and live functions, copied exactly into the baseline, and removed locally. No hosted migration history was edited.

**The consolidated baseline is destructive. Do not run it or `db push` against the hosted project.** See [the non-destructive deployment proposal](DATABASE.md#deployment-proposal). `python -m app.schema_upgrade` from `backend/` prints the exact reviewable delta, derived from the baseline; it never connects or applies it. Hosted schema changes still require approval. Until the new functions are installed, writes fail explicitly and remain recoverable.

<a id="application-reference-local-embeddings-and-semantic-retrieval"></a>
##### Local embeddings and semantic retrieval

Use Windows/Linux CPython 3.11-3.13 (tested here on Windows 3.13). From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -r requirements-embeddings.txt
.\.venv\Scripts\python.exe -m app.embedding_setup
```

The optional dependency file pins NumPy 2.3.4, ONNX Runtime CPU 1.23.2 and Tokenizers 0.22.2. `requirements-local.lock` records the tested environment including transitive dependencies. Model setup downloads about 133 MB of public ONNX weights plus its tokenizer, pinned to revision `93b36ff09519291b77d6000d2e86bd8565378086` and verified against SHA-256 checksums. Assets live only in ignored `backend/.local/models/`. Runtime inference is entirely local, uses one lazily loaded CPU model and never sends chat to a model download server. No embedding account or API key is needed.

`LOCAL_EMBEDDINGS_ENABLED=true` enables the independent worker. Useful memory text commits before embedding work starts. Memory rows carry pending/processing/completed/failed state, a content hash, a five-minute lease, claim token and three-attempt retry limit with thirty-second backoff. Restart recovery uses database state. Content edits clear the vector, reset attempts and reject stale worker results. Replays do not re-embed unchanged completed memories. A single worker claims at most eight rows, and one inference thread handles bounded inputs without blocking FastAPI or listener heartbeats.

Storage and questions share the same tokenizer, FP32 ONNX model, attention-mask mean pooling and L2 normalization. Vectors must have 384 finite values and unit norm. The model limit is 512 tokens including special tokens. Oversized text is preserved fully in the memory and marked `failed/input_too_long`; it is never silently truncated for inference. Deliberate chunking is not implemented. Oversized questions use labelled keyword fallback.

Semantic retrieval filters by tenant and enabled authorized rooms before limiting results; Discord questions remain in their origin room. Completed, active, current-version embeddings alone are eligible. Matches include source message UUIDs, and corresponding structured ownership/deadline/status fields and source metadata are retrieved separately. The synthesis prompt requests `[source:<message_id>]` citations. If embeddings are unavailable or no vector matches exist, results explicitly say `keyword_fallback`, without implying vector similarity. There is no HNSW index for the currently tiny corpus; exact cosine search avoids approximate filtering loss. No 100k-1M capacity claim has been tested.

Run real local-model tests explicitly:

```powershell
$env:RUN_LOCAL_MODEL_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_local_embeddings.py
```

The disposable PostgreSQL/pgvector schema runner is prepared under `supabase/tests/` (`npm install`, then `npm test`). Its dependency installation was declined in this session, so fresh-schema and SQL behavior execution remain pending. It never loads project credentials or connects remotely. See [verification evidence](VERIFICATION.md#useful-storage) for exact distinctions between mocked tests, real local inference and unverified hosted/Discord work.

<a id="application-reference-3-mobile-expo"></a>
#### 3. Mobile (Expo)
Use Node.js 20.19.4 or newer and npm. The mobile dependencies follow the [official Expo SDK 54 TypeScript template](https://github.com/expo/expo/blob/sdk-54/templates/expo-template-blank-typescript/package.json): Expo 54, React Native 0.81.5, React 19.1.0, and `expo-status-bar` 3.0.9. See the [SDK 54 release notes](https://expo.dev/changelog/sdk-54) for platform and tool requirements. Use an Expo Go build compatible with SDK 54 or a matching development build.

From the repository root:
```powershell
cd mobile
npm ci
Copy-Item .env.example .env
# Edit mobile/.env with a PUBLIC API URL only
npx expo install --check
npx expo-doctor
npx tsc --noEmit
npx expo start
```
`npm ci` installs the exact versions in `package-lock.json` and replaces any existing `node_modules`. Registry access is required for packages missing from the local npm cache. Keep the lockfile with `package.json` so installs remain reproducible. If Metro still has stale cached files after reinstalling, start with `npx expo start --clear`.

For dependency updates, use `npx expo install <package>` or `npx expo install --fix`, then repeat the checks above. Review `npm audit` separately; do not force audit upgrades across Expo SDK versions. Expo's CLI and Metro must come from the same supported dependency set: mixing an older Expo CLI with newer Metro can cause `ERR_PACKAGE_PATH_NOT_EXPORTED` for `metro/src/lib/TerminalReporter`. See [Expo's upgrade guide](https://docs.expo.dev/workflow/upgrading-expo-sdk-walkthrough/).

A physical phone cannot reach the PC backend using `localhost`; use the PC's local network IP and allow the appropriate firewall access. The provided screen only calls the public health endpoint; private data access requires a real user-auth flow first.

<a id="application-reference-4-codex-mcp"></a>
#### 4. Codex / MCP
Codex uses `AGENTS.md` automatically when operating inside this project. Documentation MCPs can help confirm current FastAPI, Supabase and Expo APIs, but connecting a documentation MCP does not grant access to running services. You can start Codex from this folder using `codex`.

Suggested first Codex prompt:
> Read AGENTS.md and architecture.md. Review the current project without introducing any external messaging integrations. Verify the MVC separation, security rules, environment files, migration placement, and tests. Report specific issues before changing any files.

<a id="application-reference-5-before-every-push"></a>
#### 5. Before every push
```powershell
git status --short
git check-ignore backend/.env mobile/.env
git diff --cached --name-only
```
The two `.env` files should be reported as ignored when they exist. Inspect staged contents and never force-push. If any credential was previously committed, revoke/rotate it; `.gitignore` alone cannot remove history.

<a id="application-reference-current-limits"></a>
#### Current limits

Local model inference is verified. The hosted non-destructive schema upgrade, versioned vector storage, and full Discord -> Switch -> Codex -> Supabase -> semantic answer flow remain pending deployment approval and live verification. Human input is durably journaled before acknowledgement; embedding failures preserve useful text. Oversized memories remain explicit failed embedding jobs. Fresh-schema execution is pending the declined disposable-database test dependency installation. Conflict checks, reminders, document workflows and mobile private-data authentication remain outside this task. No hosted data reset or migration-history rewrite was performed.


Attachment terminal diagnostics use `document stage=...` with safe request references
and failure reasons. Both native Switch media handles and legacy MXC handles are
supported through the authenticated media endpoint. Keep one listener; see
[terminal tracing and native media fix](OPERATIONS.md#document-attachments)
for log stages, room ownership warnings and retry instructions.


Document schema deployment update (2026-11-06): the approved binary-format constraint
migration is applied in the configured Supabase project. Backend dependencies are
installed in the project venv. The original consolidated SQL is synchronized for
fresh installs only; never replay it on hosted data. Reload and live binary-flow
verification remain outstanding. See the document rollout report.

<a id="commitment-capture"></a>
## Tagged commitment capture

Consolidated from `SIMPLE_COMMITMENT_CAPTURE.md`.

<a id="commitment-capture-tagged-commitment-capture"></a>
### Tagged commitment capture

Current creation policy, 2026-11-06:

```text
Tag bot → private AI extraction → atomic Supabase source/task/summary storage
                                             ↓
                                  existing durable embedding job
                                             ↓
                              existing gte-small worker → scoped search
```

Example: `@<registered-name> I’ll finish the report by Friday.` saves a task owned by the delivered sender ID. Its source and linked summary retain “Friday”; `due_at` is null unless an exact instant is clear. The reply is **“Task saved in Supabase. Search indexing is pending.”** It is sent only after Supabase acknowledges storage. This does not claim that a vector is already stored.

<a id="commitment-capture-what-is-required"></a>
#### What is required

The existing listener/API ingest credential, configured tenant and allowed room, enabled Supabase room mapping, existing non-AI Switch credentials/private AI runtime, and storage/embedding functions are required. Privileged Supabase credentials stay on the backend. The existing protected `/messages` endpoint and room boundaries remain intact; no new public endpoint exists.

Creation does **not** require a member record, membership query, user registration/linking/invitation, role, sender mapping, separate adapter attestation, positive human/non-bot classification, lifecycle feature flag or reminder configuration. The source sender is used directly as `owner_switch_user_id`; display names only label that owner. Someone named in the text is never selected as assignee. Exact known-self IDs and existing reply fingerprints still suppress application echoes.

<a id="commitment-capture-addressing-and-extraction"></a>
#### Addressing and extraction

Switch already computes `payload.addressed`; the listener forwards it. Capture requires that flag and an explicit mention at the start of a clear personal promise. It accepts the existing Switch name/alias form and native Discord `<@id>` / `<@!id>` syntax. No registration-name lookup or new mention-verification service is used by creation. An addressed follow-up without a fresh mention cannot create a task. A plain name substring without supported addressing metadata is insufficient.

Supported initial English grammar is a single `I’ll`, `I will` or `I commit to` promise. The existing private AI must extract a commitment; deterministic normalization takes the description from the source statement and the owner from source metadata. Greetings, questions, tentative/hypothetical/quoted statements and unmentioned commitments cannot create tasks, even if the AI incorrectly proposes one. Existing structured create proposals are normalized into the same extraction path; the worker no longer calls the advanced lifecycle creation RPC for these statements. Facts, decisions and unrelated questions retain their existing processing. Commitments bypass the older draft/yes confirmation flow.

Original deadline wording is preserved in source evidence and `Deadline wording:` in the searchable summary. `COMMITMENT_TIMEZONE` is an optional **confirmed** IANA timezone shared by configured rooms. Leave it blank if that assumption is invalid. An original timestamp plus a clear weekday and explicit time can resolve an instant; absolute aware timestamps are also supported. Missing timezone/time/original timestamp, uncertain dates and DST gaps/folds leave the exact deadline null while still saving the clear promise. The AI's guessed deadline is not accepted. No default time or timezone is invented.

The summary records `Acceptance: explicit sender statement`. Human approval stays pending, with no fabricated manager/reviewer or approval timestamp. Neither saving nor semantic search requires a separate approval command. This path does not assert advanced lifecycle/reminder eligibility; those services and their existing policies remain separately controlled.

<a id="commitment-capture-atomic-storage-and-retry"></a>
#### Atomic storage and retry

The existing `store_ai_memory` / `storage_items` / `persist_extraction` services and durable ingestion journal remain in use. One required `p_capture=true` argument selects a five-argument overload of **the same** `store_message_extraction` RPC. The overload calls the working four-argument function and initializes existing stable owner columns in the same SQL transaction. Source, commitment, linked summary and pending embedding state commit together or roll back. The existing four-argument fact/decision/legacy extraction path is unchanged.

The additive migration is `supabase/migrations/history/20261106000100_simple_commitment_capture.sql`. It adds only a backend-only function overload, no table, column, embedding model or infrastructure. A function change is necessary because the older storage RPC ignores stable owner IDs. A missing overload causes storage failure; the backend never silently falls back to owner-less success. Anonymous/authenticated client execution is revoked. Existing table constraints, owner columns, scope/source locks and unique keys are reused. No migration history was rewritten for this simplification.

Stable `capture-` extraction keys and the existing source lock prevent duplicate tasks/summaries/jobs. A changed sender/body/deadline conflicts rather than claiming or rewriting old evidence. Replays do not overwrite later ownership changes/history. A lost acknowledgement can retry the saved extraction after restart without another AI call; the original source/task/memory are returned. The API's received receipt confirms journal acceptance, not cloud storage.

<a id="commitment-capture-embeddings-and-search"></a>
#### Embeddings and search

The linked `memories` row is the durable job. Existing claim/lease/hash/retry machinery handles it. `Supabase/gte-small`, pinned revision `93b36ff09519291b77d6000d2e86bd8565378086`, masked-mean/L2 recipe and **384 dimensions** remain unchanged. No embedding is stored in commitments or a new table. The CPU worker generates a vector and persists it through `finish_memory_embedding`; only completed, recipe/hash-matching active vectors enter scoped `search_memories` retrieval. Failure leaves the saved task intact and retries the same memory within existing limits. Three exhausted attempts remain available for reviewed recovery; they are not called searchable. Keyword fallback remains explicitly labelled.

The existing optional dependency file now pins ONNX Runtime 1.24.1 for Python 3.14 because 1.23.2 has no matching wheel; older Python retains 1.23.2. This is the same runtime dependency/model, not a provider migration. Public weights were downloaded and checksum-verified locally for testing, under ignored `.local` storage.

<a id="commitment-capture-local-verification"></a>
#### Local verification

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-embeddings.txt
.\.venv\Scripts\python.exe -m app.embedding_setup
.\.venv\Scripts\python.exe -m pytest tests/test_simple_capture.py tests/test_capture_database.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

The capture database tests require the repository's existing Node/PGlite development dependencies in `supabase/tests` (`npm ci` if absent). They create fresh disposable PostgreSQL databases, never a hosted reset. They run actual storage/embedding/search SQL as `service_role`, the actual existing embedding worker and real pinned ONNX inference, with mocked AI/HTTP/Switch. A paraphrased report query must rank the report above a food distractor. Tests also check rollback, source/owner/summary consistency, duplicate replay, source spoofing, embedding failure/retry, scopes and restart after committed storage loses its acknowledgement. This proves local integration, not hosted PostgREST or real chat delivery.

For the existing optional model-specific test, set `RUN_LOCAL_MODEL_TESTS=1` and run `tests/test_local_embeddings.py`. The ordinary full suite skips that opt-in test; the capture database integration separately runs real inference when installed assets/dependencies are present. Test reports must state which executed versus skipped.

<a id="commitment-capture-remaining-deployment-steps"></a>
#### Remaining deployment steps

1. Review the actual hosted storage/embedding definitions, columns, triggers, scope mappings and grants. Earlier metadata exposed storage/embedding support, but is not proof of current complete definitions. Review/apply the **additive capture overload** only after approval. Do not replay the destructive initial schema or earlier applied lifecycle migrations. Advanced chat/lifecycle/reminder migrations are not prerequisites for capture.
2. Refresh hosted schema discovery as required and verify `store_message_extraction(uuid,text,jsonb,jsonb,boolean)` is available only to the backend. `python -m app.verify_cloud_storage --check` is read-only and reports capture-overload exposure separately; OpenAPI does not verify exact SQL/grants.
3. Keep backend Supabase URL/service secret, Switch ingest credential, existing non-AI requester/listener identity, private AI room/runtime and tenant/allowed-room configuration in ignored `.env` / approved host secrets. Set a confirmed `COMMITMENT_TIMEZONE` only when known. Enable `LOCAL_EMBEDDINGS_ENABLED=true` and install the optional requirements/assets on the actual API host. Retain one API worker with persistent private `.local` journal storage.
4. Enable `SWITCH_REPLY_TO_DISCORD=true` if confirmations are wanted. Creation does not require `AUTOMATIC_COMMITMENTS_ENABLED`, `SWITCH_ADAPTER_TOKEN`, room member mappings or reminder flags. Existing advanced features keep their separate controls and authorization rules.
5. Obtain authorization before running `python -m app.verify_cloud_storage --run-approved-fixture`; it performs real disposable cloud writes/cleanup with mocked AI and no external chat. Hosted vector persistence/search needs its own approved disposable verification. Never use real team tasks.
6. Obtain separate authorization before sending real Switch messages. Verify actual mention normalization, extraction, durable source/owner/summary/vector and a scoped paraphrase query. No hosted or live Switch success is claimed from the local tests.

Current source includes legacy lifecycle/history/reminder modules unchanged in purpose. They are not removed, silently enabled or automatically provisioned by this simpler creation flow.

<a id="commitment-lifecycle"></a>
## Commitment lifecycle and HTTP contract

Consolidated from `COMMITMENT_MANAGEMENT.md`.

<a id="commitment-lifecycle-commitment-management-contract"></a>
### Commitment management contract

For current task creation, see [SIMPLE_COMMITMENT_CAPTURE.md](OPERATIONS.md#commitment-capture): tagged extraction/storage/indexing does not require these manual lifecycle/authentication/approval prerequisites. This document continues to describe separately controlled lifecycle operations.

Implementation date: 2026-11-05. Local implementation and verification; no hosted deployment or individual-user authentication is claimed.

<a id="commitment-lifecycle-existing-components-reused"></a>
#### Existing components reused

The current [open chat integration](OPERATIONS.md#open-commitments) adds mentioned sender-owned creation/actions without manual member mapping and records explicit owner acceptance separately. Legacy mapped contracts remain for other integrations. Rules below describe the public/manual API; its human-authentication dependency remains closed, and chat does not unlock those routes. The existing chat migration was revised locally by user instruction; no remote reconciliation has been applied.


The schema already separates approval from task progress and stores reviewer, approval time, completion time, stable Switch assignee ID, deadline and reminder bookkeeping. Existing `room_members` roles are `member`, `approver` and `admin`. Rooms have internal UUIDs, tenant ownership, an opaque Switch ID and an enabled flag. Commitments retain a composite foreign key to their source message. Extraction continues to create pending, not-started records; it cannot approve work.

The existing admin-only `GET /api/v1/commitments` remains diagnostic. Ingest/admin tokens authorize server operations and do not identify people. Switch listener, private Codex extraction, SQLite ingestion journal, viewer and AI orchestration are preserved. No bot, LLM mutation step, scheduler or notification transport was added.

<a id="commitment-lifecycle-independent-module-boundaries"></a>
#### Independent module boundaries

| Layer | Contract / implementation |
| --- | --- |
| HTTP | `backend/app/controllers/commitments.py`: explicit request types, UUID scopes, bounded pagination |
| Domain models | `backend/app/models/commitments.py`: states, actions, actor, scope, events and results |
| Business rules | `backend/app/services/commitment_service.py`: authorization policy, transitions, reminder eligibility and deterministic summaries |
| Storage port | `backend/app/repositories/commitment_contract.py`: context, list, history and atomic apply operation |
| Supabase adapter | `backend/app/repositories/commitment_repository.py`: backend-only RPC transport, safe errors, response scope checks |
| Identity port | `backend/app/core/commitment_identity.py`: `resolve_commitment_actor(Request) -> Actor` FastAPI dependency |
| Transaction | `supabase/migrations/history/20261105000100_commitment_lifecycle.sql`: locked authorization, transition checks, linked memory, event and receipt |

An authentication teammate can implement the identity port independently. It must verify a session, map its immutable subject to `room_members.switch_user_id`, and derive the actor without accepting client actor/role/tenant claims. Room membership and role are loaded from storage. A storage teammate can implement the repository port with the same atomic semantics. The fake repository in tests is an example for development, not deployment persistence. This task does not modify the SQLite ingestion module.

**All new routes return 503 until a trusted individual-user resolver is integrated.** Shared tokens, role headers and actor headers do not unlock them. Dependency overrides simulate trusted identity only in tests. Future authentication should return 401 for absent/invalid sessions; authenticated users outside a scope receive 404. No admin-token impersonation or development bypass exists. No frontend setting enables these routes.

<a id="commitment-lifecycle-http-contract"></a>
#### HTTP contract

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

<a id="commitment-lifecycle-policy-and-transitions"></a>
#### Policy and transitions

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

<a id="commitment-lifecycle-atomicity-concurrency-and-history"></a>
#### Atomicity, concurrency and history

The service performs a read-only preflight. That read is **not** part of the write transaction. The adapter then makes one `apply_commitment_action` RPC. PostgreSQL obtains a transaction-scoped advisory lock for `(tenant, room, actor, idempotency key)`, locks the enabled room and actor membership for sharing, and locks the scoped commitment for update. It revalidates permissions, key content, expected version and transition under those locks; reassignment also locks the target membership. Concurrent removal of a checked membership waits for the transaction. The database uses its own transaction timestamp, not a client clock.

In the same transaction a change updates state, increments `version`, clears `next_reminder_at`, reconciles the linked memory, appends an event and records the original result. Any failure rolls everything back. A future planner must recalculate next delivery times after changes. `last_chased_at` remains historical.

Keys are scoped to tenant/room/actor and bind commitment ID, action, expected version and normalized payload. Same key/content returns the saved result without another event/update even if the original version is now stale. Permissions are rechecked before replay. Different content under the same key conflicts. A fresh key with a stale version conflicts, including an equivalent action. A fresh equivalent action with the current version records a `changed=false` event/receipt without incrementing the version. Event sequences may have gaps after rollback.

Events retain commitment/tenant/room, trusted actor, action, database time, expected version, idempotency key, reason and before/after lifecycle values. Actors remain recoverable after membership removal. Backend grants on event/receipt tables permit select/insert only; client roles have neither table nor function privileges. The service credential is privileged infrastructure, not human authorization. No API edits events or deletes receipts. Receipts currently have no expiry; pruning requires a separately defined retry window.

<a id="commitment-lifecycle-search-and-reminder-integration"></a>
#### Search and reminder integration

Summaries use the existing `(tenant_id, room_id, message_id, memory_type='commitment', extraction_key)` relationship. Extraction and lifecycle writes generate explicit assignee, approval, task and UTC deadline lines deterministically. Source text, external message links, extraction key and original unresolved external assignee evidence remain intact.

Text changes invoke the existing embedding trigger: vector and in-flight claim are invalidated; content hash changes; the embedding worker recovers active pending summaries. Rejected/completed/cancelled summaries remain stored with explicit historical states but become inactive in ordinary semantic retrieval. History/detail remains accessible. Pending active summaries explicitly say `Approval: pending`. Migration backfill repairs existing linked summaries and may queue substantial re-embedding work.

`reminder` is a structured **candidate contract**, not a scheduled notification. `eligible=true` requires approved, nonterminal work, a stable assignee ID and a deadline. Reasons include `pending`, `rejected`, `completed`, `cancelled`, `unassigned`, `no_deadline` and `eligible`. A future planner consumes result/event, invalidates its cached version, then rereads current state, membership and policy immediately before planning/delivery. Replayed results are historical and cannot authorize delivery. Missing members suppress delivery. No scheduler, reminder endpoint or notification delivery is implemented.

<a id="commitment-lifecycle-deployment-review"></a>
#### Deployment review

The additive migration adds `version`, `cancelled_at`, two tables, indexes and backend-only functions. Existing approval/task/reviewer/assignee/deadline columns and constraints are reused. Assignee validation is transactional rather than a new FK that could block unresolved extraction or invalidate legacy rows. Existing cancelled rows retain unknown cancellation times; no historical actors/times are fabricated. Scope constraints and composite FKs bind audit/receipts to commitments. Review pre-existing inconsistent lifecycle rows before enabling actions; no data is silently approved or reopened.

Do not run destructive `20261104000100_initial_schema.sql` on hosted Supabase. Follow `supabase/DEPLOYMENT_PROPOSAL.md`: compare hosted definitions, approve/apply non-destructive storage/embedding prerequisites first, then review/apply this additive migration once. The prerequisite is the full embedding recovery schema and trigger, not merely an older vector-clearing trigger. Deployment tooling must record it once; it is not a replayable reset. **No remote SQL was applied in this task.**

Before approval, review scope mappings, membership data, inconsistent states, backfill/re-embedding cost and grants. After approved deployment, refresh PostgREST schema discovery and run disposable exact-scope fixtures with dependency-ordered cleanup/rollback. Never modify real tasks or send Discord messages. Enabling routes additionally requires reviewed trusted human authentication. Until both dependencies are satisfied, the module is locally verified with fail-closed routes.

<a id="commitment-lifecycle-verification-commands-and-limits"></a>
#### Verification commands and limits

```powershell
cd D:\Hackathon\team-memory\backend
.\.venv\Scripts\python.exe -m pytest tests/test_commitment_lifecycle.py tests/test_commitment_transport.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd ..\supabase\tests
npm ci --ignore-scripts
npm run test:commitments
```

Domain tests use a fake repository and controllable clock. HTTP tests override identity; transport tests mock PostgREST. The Node runner uses pinned PGlite PostgreSQL + pgvector, creates a fresh disposable local database, applies the baseline and additive migration, and rolls fixtures back. It tests actual SQL functions, backend-role execution, permissions, stale versions, idempotency, source retention, embedding lease invalidation and rollback after receipt insertion failure. It does not test hosted PostgREST or real multi-connection lock contention; concurrent service tests use the locked fake. Hosted lifecycle actions remain unverified. See `supabase/COMMITMENT_VERIFICATION.md` for executed results.

<a id="open-commitments"></a>
## Open commitment actions

Consolidated from `OPEN_COMMITMENTS.md`.

<a id="open-commitments-open-mentioned-commitments"></a>
### Open, mentioned commitments

**Creation is now simpler:** [SIMPLE_COMMITMENT_CAPTURE.md](OPERATIONS.md#commitment-capture) supersedes the creation, positive human/bot proof and exact-deadline requirements in this document. Tagged capture uses existing ingestion auth/addressing and the delivered sender ID, atomic extraction storage and the embedding worker. The lifecycle/history/reminder machinery and this older internal contract remain separately controlled; creation does not depend on them.

Current local chat policy, 2026-11-06. This supersedes mapped-member onboarding and untagged task updates in AUTOMATIC_COMMITMENTS.md. That legacy contract remains available to other internal integrations. No hosted task or external chat message was changed.

<a id="open-commitments-identity-and-mentions"></a>
#### Identity and mentions

An authenticated listener delivery in an enabled, configured room establishes a previously unknown human sender. No account linking, registration, invitation, manually assigned role or sender mapping is required. Ownership is `openchat:` plus SHA-256 of the JSON tuple `[tenant UUID, internal room UUID, "switch", immutable Switch sender ID]`. Switch puppet identity is the namespace; external IDs and display names never merge accounts or grant authority.

The database automatically materializes a default, nonprivileged participation reference in existing `room_members` for lifecycle/FK/reminder compatibility. This is backend bookkeeping, not human onboarding or manager authority. It does not create or overwrite `commitment_chat_identities` mappings. Open actions enforce ownership even if that participation row accidentally acquires an admin role. Existing name-only tasks cannot be claimed by saying “my”; they need authorized internal reassignment.

`X-Ingest-Token` protects ingestion; a distinct `X-Adapter-Token` authenticates source metadata. Shared viewer/admin/ingest credentials alone cannot assert a human identity. The listener fetches its actual registration using `get_agent_detail` and forwards the registered agent ID/name, original sender, timestamp, routing and optional structured mention/bot metadata. The backend compares the registration ID to its configured listener identity. Structured `mentioned_agent_ids`, when present, are authoritative. Otherwise a fallback requires both Switch `addressed=true` and the exact registered `@name` token with boundaries. An addressed follow-up, arbitrary name substring or mention of another account is insufficient. Commands currently put the registered-name mention first.

**Adapter limitation:** Switch's documented basic message event guarantees sender/addressed/timestamp, but does not guarantee structured mention targets or a human/bot flag. Missing `sender_is_bot` remains unknown, never assumed human. If the adapter cannot supply verified `sender_is_bot=false`, tasks stay unchanged and the reply explains the issue without asking for registration. The deployment's metadata capabilities still need verification. Do not fill a constant false for all senders. Native platform mention syntax not normalized to the registered `@name` prefix and unverified aliases are safely declined. These are adapter capabilities, not per-person mapping requirements.

<a id="open-commitments-conversation-contract"></a>
#### Conversation contract

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

<a id="open-commitments-atomic-lifecycle-and-reminders"></a>
#### Atomic lifecycle and reminders

Creation keeps human approval pending with no reviewer/time, task not started, and records `owner_accepted_at` plus immutable `owner_acceptance_source_id`. This is explicit owner acceptance, not manager approval or blanket acceptance of AI extraction. The older automated-completion evidence stays separate. Owner-accepted pending tasks can start/complete through open chat and satisfy shared reminder eligibility. Internal reassignment clears the previous owner's acceptance.

The repository port adds `create_chat`. Backend-only SQL `create_owner_commitment` locks scope/source/policy, checks a scoped receipt, preserves/inserts source evidence and atomically writes task, deterministic linked memory, create audit and receipt. New-record audit uses expected version 1 with an empty before-state. Updates reuse the lifecycle service/transaction engine, versions, audit, receipts and embedding invalidation. Audit preserves initiating owner identity, backend actor and redacted source evidence.

Creation keys derive from scope/source message; update keys also include task/action. The existing durable journal saves plan/proposal before execution. Lost acknowledgements replay receipts; changed source/payload conflicts. Uncertain or denied actions are not technical retries. Original conversations are not rewritten. Confirmations reuse the existing Switch reply policy/watermark and remain best effort.

The existing reminder planner shares the new owner-acceptance eligibility rule. Automatic participation references satisfy existing membership checks without manual setup. Reminders need no fresh mention. Task version changes supersede queued deliveries through existing triggers; dispatch rechecks eligibility. Quiet hours, cadence, limits, claims/recovery and ambiguous-send handling are unchanged. Global and room reminder defaults remain disabled.

<a id="open-commitments-migration-status-and-remaining-operator-steps"></a>
#### Migration status and remaining operator steps

All schema/function changes are in the existing `supabase/migrations/history/20261105000200_commitment_chat.sql`. It adds owner-acceptance columns/pair constraint/scoped source FK, create audit value/RPC and open preparation/authorization/eligibility. Existing owner/scope indexes, audit uniqueness and receipts are reused. No new SQL file was created.

Before editing, a read-only hosted PostgREST probe exposed storage/embedding support but no lifecycle/chat/reminder functions or columns. Exact migration history, grants/definitions, schema-cache freshness and other environments could not be verified. This is not proof the migration was never applied anywhere. Editing locally does not update hosted Supabase; replaying a previously applied version can fail or be unsafe. No migration was marked deployed, no history changed and no reset executed.

1. Review the real hosted catalog/history and storage/embedding prerequisites. Review a non-destructive hosted reconciliation from the existing migration diff, preserving data. Obtain approval before remote changes. Do not replay the destructive core baseline or overwrite migration history. This task creates no replacement SQL file.
2. Verify approved disposable hosted creation/actions/receipts/history/memory/reminder invalidation, exact grants and cleanup. No real team tasks are authorized.
3. Retain existing tenant/allowed-room mappings. Enable only reviewed room chat-policy rows and confirm their IANA timezone. Do not create/overwrite real policies automatically. There is no per-person configuration step.
4. Configure a distinct private adapter secret locally; retain non-AI listener and private-runtime credentials. Enable `AUTOMATIC_COMMITMENTS_ENABLED=true`, restart API/listener, and verify registration lookup, immutable human identity and explicit mention metadata. Missing proof blocks task changes. The backend appends the expanded proposal contract to the existing private runtime.
5. Keep `SWITCH_REPLY_TO_DISCORD=true` when confirmations are desired. Enable reminders separately through `REMINDERS_ENABLED`, `REMINDER_DELIVERY_ENABLED` and reviewed room reminder policy. Existing single-worker deployment limits apply.
6. Obtain authorization before real Switch delivery verification. Fake replies/sinks and local database success do not establish external delivery.

Tests: backend `python -m pytest -q`; from `supabase/tests`, `npm run test:chat` and `npm run test:reminders`. Python uses fake persistence, mocked AI/HTTP and a real disposable SQLite recovery journal. PGlite runs real disposable PostgreSQL SQL for creation/replay/ownership/version conflicts/rollback, source/audit/memory consistency and reminder supersession. Its single connection does not prove multi-connection lock contention or multi-instance safety. No real hosted action, model run or external send is claimed.

<a id="automatic-commitments"></a>
## Automatic lifecycle policy and evidence

Consolidated from `AUTOMATIC_COMMITMENTS.md`.

<a id="automatic-commitments-automatic-commitment-management-from-chat"></a>
### Automatic commitment management from chat

For current creation, use [SIMPLE_COMMITMENT_CAPTURE.md](OPERATIONS.md#commitment-capture). Registration/member mapping/human classification and lifecycle activation are not prerequisites for tagged capture. This document describes the separately controlled advanced action integration.

**Current open chat policy:** [OPEN_COMMITMENTS.md](OPERATIONS.md#open-commitments) supersedes the mapped-member onboarding and untagged-action requirements below. The older mapped contract is retained for other internal integrations and its historical tests; the ingestion worker uses `prepare_open`, not this legacy onboarding path. Open chat derives sender ownership without registration, requires a genuine agent mention, supports creation/start/own-task reads, and records explicit owner acceptance. The existing local chat migration has been revised by user instruction; editing it does not deploy or safely replay an applied migration.

Implemented locally on 2026-11-05. Hosted migration, live sender onboarding and external chat verification have not been performed. No real tasks or Discord messages were changed.

<a id="automatic-commitments-execution-path-and-reused-components"></a>
#### Execution path and reused components

The subsequent [reminder milestone](OPERATIONS.md#reminders) adds separate planning/outbox/dispatch modules and extends the shared room policy. It does not change chat acceptance or public authentication rules. Hosted lifecycle/chat/reminder deployment remains pending review; the earlier no-scheduler scope statements describe this chat module's original task.


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

<a id="automatic-commitments-identity-and-authorization"></a>
#### Identity and authorization

The viewer's shared ingest token is not evidence of a person. Listener forwards now additionally carry `X-Adapter-Token`, configured through `SWITCH_ADAPTER_TOKEN`. It must be separate from the ingest/admin/database credentials and at least 32 random characters. The controller verifies it before internally marking a durable delivery `adapter_verified`; that field is not accepted in incoming message JSON. The listener preserves the actual Switch sender, optional external platform author, message ID, original timestamp and thread. The adapter token authenticates our trusted listener, not each individual speaker. Upstream platform verification remains Switch's responsibility.

`commitment_chat_identities` is a backend-maintained mapping of `(tenant, internal room, Switch sender, external author or empty string)` to a stable `room_members.switch_user_id`. Missing mappings, missing timestamps, missing/disabled policies or unresolved membership cannot execute actions. Names in message text and `sender_name` never establish initiator identity. Do not infer mappings from a participant name or opaque ID prefix. If platform author metadata is unavailable, onboard the exact Switch puppet ID with an empty external author; if it is available, match both fields exactly. Map verified humans only; model/application senders remain excluded by the existing loop guards.

Runtime privileges cannot provision identities or policies. A trusted operator must review the mapping and actual membership in an approved backend onboarding step. The database rechecks the mapping, enabled policy, room, member role and task ownership under locks on every write. The worker's fixed execution identity is `switch-ingestion-worker`; audit records separately identify the initiating member and exact source. The model cannot supply either authority or scope.

The existing unauthenticated `/viewer/env` read/write routes were disabled with 503 because they exposed/replaced backend credentials. Otherwise a public caller could obtain the adapter credential and impersonate the listener. Configure secrets locally; do not use viewer environment APIs. The message feed, WebSocket and AI viewer remain in place. The old environment panel cannot read or save configuration.

<a id="automatic-commitments-automatic-policy"></a>
#### Automatic policy

| Action | Chat authority and evidence |
| --- | --- |
| Complete | Owner of the matching eligible task, or room admin for already approved work; clear first-person completion evidence |
| Cancel | Owner or room admin; explicit cancellation statement |
| Reschedule / clear deadline | Owner or room admin; explicit change, resolved instant or explicit removal |
| Reassign | Room admin only; explicit settled handoff and a uniquely resolved current in-room member |

An `approver` role does not gain admin authority in this chat policy; it may update its own tasks. Public/manual lifecycle policies remain unchanged, including their stricter assigned-user completion rule. Chat never approves, rejects, starts or reopens tasks. Rejected or terminal work cannot be changed by a new conversational action. Equivalent eligible actions remain idempotent through the existing service.

The supported handoff policy is deliberately **`admin_only`**. An owner's statement that “Rehman is taking over the report” is an unaccepted proposal and makes no change. A mapped room admin's identical settled statement satisfies the policy. The target must resolve uniquely against trusted backend labels or a stable platform mention such as `<@platform-id>`, and its stable ID must match the proposal and current room membership. Duplicate labels or an out-of-scope target leave ownership unchanged. Labels locate references; they do not prove who is speaking. Target-acceptance conversations and owner-only handoff negotiation are not implemented.

<a id="automatic-commitments-pending-approval-compatibility"></a>
##### Pending approval compatibility

New extraction remains `pending` / `not_started`; no human approver is fabricated. Pending tasks may be cancelled, reassigned or rescheduled under the chat authority policy. Completion of a pending task additionally requires its actual owner and the room's explicit `accept_pending_owner_completion=true` policy. This option defaults to false. Admin authority alone cannot complete another member's pending task.

An allowed pending completion leaves `approval_status=pending` and the reviewer/time unchanged, while setting `task_status=completed`, the completion time, `automated_acceptance_at` and `automated_acceptance_source_id`. The summary explicitly identifies automated owner completion evidence. This is an observation of the owner's completion statement, not human approval. Public/manual completion still requires approval. Rejected records are never accepted automatically.

Existing commitments need a stable `owner_switch_user_id`. Extraction currently retains unresolved owner names; this change does not silently assign those names to people or backfill ownership. Unassigned/name-only commitments remain unchanged until a trusted stable assignment exists. This is a deliberate authorization limitation, not proof that every extracted task is immediately manageable.

<a id="automatic-commitments-proposal-and-evidence-contracts"></a>
#### Proposal and evidence contracts

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

<a id="automatic-commitments-deadlines"></a>
##### Deadlines

Absolute dates require an explicit offset. Relative weekdays use the original source timestamp and a confirmed room IANA timezone. A weekday means its next strictly future occurrence within the coming week; same-day weekday wording is ambiguous. Time must be explicitly supplied as `at HH:MM` or preserved from the task's existing deadline in that confirmed timezone. A missing time, missing/unknown timezone, unsupported date expression, or ambiguous/nonexistent DST clock time leaves the task unchanged. The backend recomputes the instant and compares it with the model's proposal. It never uses worker processing time to resolve a date. Explicit deadline clearing requires clear/remove wording and null, not an unresolved date.

`tzdata` supplies timezone definitions on systems without an IANA database, including Windows; see [Python's ZoneInfo data-source documentation](https://docs.python.org/3/library/zoneinfo.html#data-sources). Stored deadlines remain consistent UTC instants. A policy/timezone change while reasoning conflicts instead of applying an outdated interpretation.

<a id="automatic-commitments-durability-atomicity-and-outcomes"></a>
#### Durability, atomicity and outcomes

The existing private SQLite ingestion journal adds `chat_plan` and `chat_outcome` columns through a local additive upgrade. Validated decisions, original versions and evidence plans survive technical failures/restarts. Completed jobs clear the plan/payload/decision and retain safe outcome/source references. This remains a single-host, single-worker journal, not a distributed queue. Existing jobs without adapter attestation cannot gain chat authority retrospectively.

Idempotency UUIDs derive from tenant, room, original source message, commitment and action. One source can produce at most one lifecycle audit event, enforced by a database unique index and source-row lock. Replayed source/proposal content returns the stored receipt, with no additional update, event or vector invalidation. A new interpretation under a consumed source conflicts, even if local journal history were lost. Changed source content or identity conflicts. Version conflicts do not silently reselect a newer task or overwrite it. A new user message is required to clarify/reconfirm.

The candidate/source registration RPC and the preflight read are separate from the mutation transaction. They are **not** described as one atomic transaction. `perform_chat` calls one backend-only write RPC, using the shared lifecycle transaction engine. Under locks it validates source scope, current mapping/member/role, policy snapshot, target membership, idempotency, version and transition, then writes task state, linked memory, audit event and receipt atomically. A failure at receipt insertion rolls all lifecycle writes back; already registered source evidence remains available.

Audit adds backend actor, initiating member, source evidence ID, exact redacted evidence, derived outcome and before/after lifecycle values. Source messages and original message links are retained. Linked summary edits use the existing invalidation trigger, clearing stale vectors/claims; completed/cancelled summaries become inactive and ineligible for reminders. No scheduler runs.

Outcomes are `applied`, `no_change`, `ambiguous`, `unauthorized`, `conflict` or `failed`. They appear in the AI response contract and retained journal diagnostics. Nontechnical declines finish the job and are not retried. Technical failures use the existing three-attempt/thirty-second-backoff recovery; the saved proposal/version is reused. Disabling the global feature also blocks cached proposals during recovery. A replayed `applied` result describes the original accepted mutation, not another state change.

Untagged automatic actions and ambiguity remain silent. Addressed messages may receive a deterministic brief result or clarification under the existing reply setting/watermark policy. Confirmations do not use model-generated claims of success. Existing AI/application sender and persisted echo guards remain active.

<a id="automatic-commitments-files-and-deployment"></a>
#### Files and deployment

Core files: `models/commitment_chat.py`, `services/commitment_chat_evidence.py`, `services/commitment_chat_service.py`; the existing lifecycle models/service/repository gain an internal chat contract. The AI decision, prompt and worker, ingestion controller/journal, security/configuration and listener are extended. Unsafe viewer environment routes are disabled. `requirements.txt` adds timezone data. Tests cover fake domain state, actual local journaling, mocked AI/HTTP and disposable SQL.

Additive `supabase/migrations/history/20261105000200_commitment_chat.sql` adds policy, trusted-identity and immutable-source tables; automated-acceptance columns; audit source/backend/outcome columns; a one-source/one-action index; source/candidate preparation and internal mutation functions. It factors the existing lifecycle write into one shared engine with manual/chat wrappers, preserving manual authorization and transaction behavior. The new outcome column classifies existing no-op events without changing old actor/state evidence. Previous migration files are not rewritten. Source/policy/identity tables use RLS and backend-only grants; narrow lock-marker column privileges permit row locks without granting runtime modification of identity, policy or evidence fields.

Deployment remains gated:

1. Review actual hosted definitions and approve non-destructive storage/embedding recovery prerequisites, then the lifecycle and chat migrations in order. **Never run the destructive core baseline remotely.** No hosted SQL was applied here.
2. Onboard actual tenant/room/member/sender tuples with a trusted operator. Add an enabled room policy and confirmed timezone; explicitly decide whether pending owner completion is allowed. Do not map bots or infer people from names.
3. Generate a distinct adapter-only token into ignored local `.env`, configure the listener/backend, install requirements, and set `AUTOMATIC_COMMITMENTS_ENABLED=true` only after those checks. The default is false; `.env.example` contains placeholders only. Keep secrets out of the viewer, mobile app and commands/transcripts.
4. Restart backend/listener and verify using disposable exact-scope fixtures after deployment approval. Hosted concurrent lock contention and real Switch-to-member identity mapping still need live verification. Do not send external messages or alter real tasks without authorization.

No human-login integration is needed for this trusted chat path. Public/manual lifecycle endpoints still return 503 until individual-user authentication is integrated separately.

<a id="automatic-commitments-verification"></a>
#### Verification

```powershell
cd D:\Hackathon\team-memory\backend
.\.venv\Scripts\python.exe -m pytest tests/test_commitment_chat.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd ..\supabase\tests
npm run test:chat
```

The SQL runner creates a fresh disposable PGlite PostgreSQL database, applies all three migrations and runs both the prior manual lifecycle fixture and new chat fixture. It never reads `.env` or contacts hosted services. Python tests use fake lifecycle persistence and mocked AI responses, but a real disposable SQLite journal for recovery tests. SQL tests verify actual resulting records, audit, memory and rollback, including service-role execution. These are stronger than proposal-only tests, but are not real hosted or external-chat verification. See [the execution report](VERIFICATION.md#commitment-chat).

<a id="reminders"></a>
## Reminder planning and delivery

Consolidated from `REMINDERS.md`.

<a id="reminders-reminder-planning-and-delivery"></a>
### Reminder planning and delivery

[Simple tagged capture](OPERATIONS.md#commitment-capture) saves/indexes tasks without enabling reminders or asserting reminder-policy eligibility. Existing acceptance/approval and delivery controls below remain separate from creation.

Implemented locally on 2026-11-05. Defaults are disabled. Hosted metadata was inspected read-only; commitment/chat/reminder deployment is blocked pending review and approval. No real tasks or external chat messages were changed.

The current [open chat policy](OPERATIONS.md#open-commitments) also permits active commitments with source-backed explicit owner acceptance while human approval stays pending. Automatically materialized participation references satisfy the existing membership check without manual onboarding. Older statements below that only human-approved tasks are eligible are superseded by this addition; existing automated-completion-only acceptance remains terminal/ineligible. Quiet hours, cadence and outbox behavior are unchanged.

<a id="reminders-current-deployment-findings"></a>
#### Current deployment findings

The actual configured hosted PostgREST metadata exposes `store_message_extraction`, `claim_memory_embeddings` and `finish_memory_embedding`, and the required memory hash/status/claim/lease columns. The older report saying those functions were absent is superseded by this observation.

The metadata does **not** expose `read_commitment_lifecycle`, `apply_commitment_action`, `prepare_commitment_chat`, `apply_commitment_chat_action` or any new reminder RPC. Commitment `version`, cancellation and automated-acceptance columns, chat policy/identity tables and reminder occurrence/version/send-start columns are absent from the exposed schema. The hosted API therefore cannot currently support the implemented workflow. Metadata alone cannot rule out a stale schema cache or verify full function bodies, constraints, migration history, RLS and exact client grants.

Local configuration has two allowed Switch rooms, a configured tenant and non-AI identity, automatic commitments disabled, no adapter credential, and both reminder flags disabled. Credential values and private records were not printed. This confirms configuration presence, not Switch runtime health or bridge delivery.

`python -m app.commitment_deployment_check` repeats the read-only metadata probe. The approved operator can use `supabase/reminder_deployment_review.sql` for a read-only catalog review of function hashes, signatures, privileges, constraints and triggers. That catalog script has **not** been run on the host. See [verification/deployment status](VERIFICATION.md#reminders).

<a id="reminders-reused-architecture-and-authorization"></a>
#### Reused architecture and authorization

The planner calls the existing `reminder_state()` eligibility function. Database guards use the existing `commitment_reminder()` function. Approved, nonterminal tasks with deadlines and stable assignees are candidates; additional checks require an enabled Discord room, enabled reminder policy, confirmed IANA timezone and current room membership. Completed, cancelled, rejected, unassigned and unapproved work is ineligible. Automated owner acceptance remains distinct from human approval; its completed/pending records are not reminder targets. No approval rules or lifecycle action meanings were changed.

The existing backend-managed `commitment_chat_policies` row is extended with reminder preferences. Its chat `enabled` flag still controls chat management; `reminders_enabled` independently controls reminders. No competing preferences table or public settings endpoint was created. Runtime credentials can read policies but cannot change their protected columns. Changes require a trusted database operator until real user-facing settings authentication exists. Adapter credentials do not authorize human preference changes.

The viewer's old reminder values were only browser-local. Its reminder controls are now read-only, and reminder settings are no longer saved/restored from localStorage. Those controls do not display live server policy or enable delivery. Environment-reading/editing routes remain disabled. No room policies or identity mappings were automatically created or overwritten.

<a id="reminders-defaults"></a>
#### Defaults

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

<a id="reminders-modules-and-contracts"></a>
#### Modules and contracts

| Module | Responsibility |
| --- | --- |
| `models/reminders.py` | Policies, history, work snapshots, intentions, delivery states and notification envelopes |
| `services/reminder_planner.py` | Pure `plan_reminder(work, now)` and quiet-hour calculations; no I/O, wall clock or LLM |
| `repositories/reminder_repository.py` | Scoped backend-only RPC transport and response validation |
| `services/reminder_scheduler.py` | Bounded tick, non-overlap lock, cursor and idempotent persistence |
| `services/notification_dispatcher.py` | Current eligibility, routing/rendering hooks, echo protection, send-start and result orchestration |
| `services/switch_notifications.py` | Existing non-AI Switch identity and pooled transport; one post attempt, no unsafe fallback |
| `services/reminder_worker.py` | Application lifecycle and separate planning/dispatch loops |

An intention contains tenant/room/task, stable recipient, task/policy versions, type, ordinal, logical base time, deferred scheduled time and a stable UUID derived from the cycle/type/ordinal. Its identity does not change with each tick. The planner consumes current successful history and pending delivery state, never an LLM deadline interpretation.

The dispatcher accepts readiness/render hooks and a notification-sink port. Future notifications can supply their own envelope/policy adapter while reusing the delivery boundary. Existing AI reply behavior was not migrated. Source-backed fixed reminder text includes task description, UTC deadline, source message UUID and occurrence reference. Source text is redacted and mention characters escaped. It is not an instruction to the model, and routine reminder processing makes no LLM call.

<a id="reminders-persistence-claims-and-recovery"></a>
#### Persistence, claims and recovery

The additive `20261105000300_reminder_outbox.sql` extends the existing `reminder_deliveries` table. Existing columns for scheduled time, status, attempts, retry, claim/lease, Switch reference and sent time are reused. New fields bind an occurrence to recipient, task/policy versions, kind/ordinal, logical base, durable send-start, invalidation time and frozen notification text. Unique indexes enforce occurrence and logical-cycle identities and one active pending/sending delivery per task. The old time-only unique constraint is replaced so different version cycles can share a timestamp without being conflated.

Unversioned unsent legacy rows are retained as `superseded` with `legacy_unversioned`; they cannot safely prove current intent. Legacy sent rows remain historical and contribute to the last-success gap. Review this classification and affected counts before deployment. No original task, source conversation or lifecycle audit evidence is rewritten.

Scheduler reads and persist calls are separate operations. Each persistence RPC locks the task, rechecks scope/current policy/version, and inserts idempotently. It does not count a successful send. Claiming uses row locks and `SKIP LOCKED`, rotates a claim token and sets a lease. Claiming alone does not increment the external-attempt counter.

The dispatcher reloads task/policy/history, applies quiet hours and cadence, records the existing journal echo fingerprint, then calls `begin_reminder_dispatch`. That short transaction rechecks eligibility, scope, membership, recipient, versions, room spacing and quiet hours, and records send-start/attempt before returning. The transaction ends **before** waiting for Switch. A room advisory lock plus visible in-flight markers prevents two begins from bypassing room spacing. No database transaction spans an external request.

Successful Switch acknowledgement is saved by another atomic RPC, together with `sent_at`, Switch reference, `last_chased_at` and next-cadence estimate. Bookkeeping does not increment the commitment's lifecycle version. The planner uses authoritative delivery history, not the cached next-reminder field alone. Receipt persistence failure cannot safely count as a saved success.

Lifecycle version changes atomically invalidate stale unsent intentions through a trigger; reminder policy changes automatically increment the policy version and invalidate its old intentions. Expired upcoming work is superseded rather than sent late. Room/member removal is rechecked before sending. In-flight sends retain their marker so a later acknowledgement can record what actually happened even after invalidation; stale success does not advance the current task's cadence cache.

Expired **unstarted** claims return to pending with a new token. Expired **started** claims become `ambiguous`, not automatic retries. An explicit ambiguous transport result is also quarantined. A late acknowledgement with the original retained token can record a recovered started attempt. Unknown outcomes preserve text/source/occurrence references for controlled operator investigation. No public endpoint retries or reconciles them.

There is an unavoidable race between the final transaction and the external post: a task may change just after the recheck. The system cannot retract an already accepted message, and no transaction is held open to pretend otherwise. The audit retains that historical outcome. Multi-instance deployment and exactly-once external delivery are **not claimed**.

<a id="reminders-switch-delivery-semantics-and-limits"></a>
#### Switch delivery semantics and limits

Notifications use the existing `team-memory-client` credentials and `SwitchAPI` connection/pool configuration, through `/ops/post_message`, never a direct Discord API or an additional AI responder. The sink refuses the private AI room, unconfigured destinations, disabled delivery and an identity equal to the AI identity. Original source-thread IDs are preserved where configured. Direct messages, cross-room routing and assignee pings are unsupported in this initial implementation.

Connect/pool failures before posting and explicit 429 rejection are retryable. Explicit 4xx rejection is permanent. Read/write timeouts/errors, 5xx, invalid successful responses and missing message references are ambiguous, because Switch could already have accepted the post. The new sink does not use the existing reply helper's broad fallback after an error; unrelated reply code stays unchanged.

The documentation review did not establish a verified caller-supplied idempotency or automatic history-reconciliation contract for this post operation. The implementation therefore quarantines uncertain outcomes instead of inventing unsupported parameters or blindly posting twice. Operator reconciliation remains pending. See Switch's [posting protocol](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol#writing-a-client) and [thread correlation](https://docs.switchagents.ai/switch-rooms/internals/life-of-a-message#thread-correlation).

`sent` means a valid Switch room-message acknowledgement was received and recorded. It does **not** prove the downstream Discord bridge delivered or that a human read it. Existing configured sender and durable echo-fingerprint guards prevent the application from processing its own reminder as another human task update.

Reminder delivery has its own flag, independent of the existing `SWITCH_REPLY_TO_DISCORD` AI-answer setting. Keeping AI answers server-only does not authorize notifications; both reminder flags and reviewed room policy remain off until explicitly enabled.

<a id="reminders-supported-deployment-and-approval-sequence"></a>
#### Supported deployment and approval sequence

Supported model: one FastAPI process/worker on one host, existing persistent private `.local` journal, one non-AI Switch listener, hosted Supabase outbox. Both application loops recover from the database after restart. Tests exercise competing claim calls and expired leases locally, but not real multi-connection hosted contention; do not deploy several application instances based on these tests. Polling and bounded batches provide best-effort timing, not an exact-deadline SLA.

Before enabling:

1. Review the hosted catalog and actual prerequisites. Metadata confirms storage/embedding presence, not exact implementation/grants. Do not replay the destructive core migration or rewrite migration history.
2. Approve/apply only the reviewed additive lifecycle `20261105000100`, chat `20261105000200` and reminder `20261105000300` migrations in that order if absent. Inspect legacy reminder rows and actual constraint names first. Refresh PostgREST schema discovery and record genuine applied history only.
3. Review actual room platform/mapping, membership, timezone and reminder policy. No rows are automatically provisioned or enabled. User-facing policy mutation remains unavailable until trusted human authentication exists.
4. Enable planning first (`REMINDERS_ENABLED=true`, delivery false), verify disposable scoped intentions and cleanup after deployment approval, then separately authorize a disposable external-delivery test before enabling `REMINDER_DELIVERY_ENABLED`.
5. Keep the existing non-AI identity in input rooms and Codex isolated in its private room. Confirm original room/thread bridge behavior. Review ambiguous deliveries rather than auto-resetting them.

No hosted schema changes, hosted fixture writes, real team-task changes, external reminders, paid infrastructure or compute changes were made in this task. Full hosted workflow and real Switch/Discord delivery remain blocked/pending, even though local implementation and tests pass.

<a id="reminders-verification-commands"></a>
#### Verification commands

```powershell
cd D:\Hackathon\team-memory\backend
.\.venv\Scripts\python.exe -m app.commitment_deployment_check
.\.venv\Scripts\python.exe -m pytest tests/test_reminders.py tests/test_reminder_transports.py -q
.\.venv\Scripts\python.exe -m pytest -q
cd ..\supabase\tests
npm run test:reminders
```

The hosted probe is GET-only metadata. Unit/integration tests use controllable clocks, fake task/preferences/outbox repositories, a real disposable local journal and fake notification sinks. Transport tests mock Switch and PostgREST. The SQL runner uses disposable local PostgreSQL/PGlite, applies all four migrations and verifies previous manual/chat lifecycle fixtures plus the reminder fixture. It never reads `.env` or contacts a hosted service. See the [executed verification report](VERIFICATION.md#reminders) for exact results and limitations.

<a id="person-profiles"></a>
## Person profiles

Consolidated from `PERSON_PROFILES.md`.

<a id="person-profiles-optional-person-profiles"></a>
### Optional person profiles

Profiles collect explicit statements from existing authorized message ingestion.
They are disabled by default. This module adds no messaging integration, model
account, identity lookup, mobile UI or historical backfill.

<a id="person-profiles-identity-and-evidence"></a>
#### Identity and evidence

`person_profiles.id` is a UUID. `(tenant_id, room_id, switch_author_id)` is unique.
Two people with the same display name remain distinct; renamed senders retain their
identity; the same sender in another room has a separate profile. Never merge by
display name or assume the Switch sender ID is a Discord user ID.

Personal reads such as "what is my preferred name?" use that exact sender identity
from the incoming message, within the configured tenant and origin room. They
bypass room-wide keyword/vector search and the shared model context. The backend
returns only the sender's active evidence, preserves source citations, and reports
missing identity/data or conflicting preferred-name reports explicitly. An unknown
sender never inherits another user's saved preference. Usernames and display
names cannot select the requesting person's profile.

This is a trusted server-ingestion identity boundary, not a replacement for
individual-user authentication on the protected HTTP profile endpoints. The
integration must supply distinct sender IDs for distinct participants. Safe
retrieval logs expose only a hashed sender reference to help diagnose that mapping.

Save-confirmation drafts are keyed by tenant, room, thread and sender, so users in
the same room cannot overwrite or approve each other's drafts. Previously staged
room/thread drafts are not reused; submit those statements again after reloading
the backend. No schema or profile ID changes are needed for this correction.

Each claim has an allow-listed attribute, literal value and derived state. Each
evidence row links its claim to an exact redacted source quote, message UUID,
timestamp and `fact` memory UUID. Composite foreign keys prevent cross-room links.
Claims are reported statements rather than independently verified personal facts.

<a id="person-profiles-supported-first-version"></a>
#### Supported first version

The existing private reasoning model proposes profile facts; backend validation
accepts only the full current source in these conservative forms, optionally
preceded by an agent mention:

| Statement | Attribute |
| --- | --- |
| `I maintain the billing service.` | responsibility |
| `I specialize in Python.` / `I am skilled in Python.` | skill |
| `I prefer asynchronous updates.` | preference |
| `My timezone is Asia/Karachi.` | timezone |

Explicit corrections use `My responsibility is now X.`, `My specialty is now X.`,
`My preference is now X.` or `My timezone is now X.`. The model must return the exact
source and value; it cannot supply subject IDs. Quotations, questions,
hypotheticals, third-party attribution and unsupported wording are rejected as
profile proposals. Other existing extraction paths retain their own rules.
Adding supported grammar requires positive and negative tests.

Repeated evidence supports an existing attribute/value claim. An explicit
correction supersedes earlier evidence for that attribute by source timestamp,
independent of processing order. Equal-time alternatives remain visible. Different
active timezone values are flagged as conflicting; responsibilities, skills and
preferences can have multiple values. Timezone strings are reported text, not
validated scheduling configuration.

<a id="person-profiles-storage-and-retrieval"></a>
#### Storage and retrieval

Source message, existing extracted items, profile, claim, evidence and searchable
memory commit together. Supabase calls the backend-only, separately named
`store_profile_message_extraction` RPC. SQLite uses a native immediate transaction.
Successful replays return the saved counts; altered replays fail, and deleted
evidence is never recreated by replay. Existing messages cannot be silently
backfilled. Four-count storage acknowledgements remain compatible with ingestion.

The embedding worker uses existing 384-dimensional embeddings. Superseded memories
are excluded from search. Profile retrieval carries sources, conflicts and historical
states; answers request source citations, with only retrieved source IDs preserved
through credential redaction. More than 200 context evidence rows fails explicitly.

Pruning source messages or evicting linked memories removes evidence and recomputes
claims in the same database transaction. Unsupported claims and empty profiles are
deleted. If correction evidence is removed, older retained reports may become
active again. History is limited to retained sources.

The Supabase-to-SQLite importer includes profiles, claims, evidence and membership,
preserves UUIDs, avoids `REPLACE` cascades, and recomputes state after import.
Extraction replay receipts are engine-local. Importing does not enable backfill.

<a id="person-profiles-http-reads"></a>
#### HTTP reads

- `GET /api/v1/tenants/{tenant_id}/rooms/{room_id}/profiles?limit=50&after=<UUID>`
- `GET /api/v1/tenants/{tenant_id}/rooms/{room_id}/profiles/{profile_id}`

Listing returns `items` and `next_cursor`. Details return the profile, source-backed
claims, evidence, `conflicting_attributes`, `verification=reported_statements` and
`truncated`. Details retain at most 200 claims/evidence rows. Do not infer absence
of conflicts from a truncated result.

`resolve_profile_actor` currently returns 503. A trusted individual-user identity
provider must replace it before HTTP reads work; never derive actor IDs from shared
admin/ingest tokens, request bodies or identity headers. A resolved actor still
requires stored room membership and the configured tenant and room allow-list.
No profile mutation API is exposed. All profile HTTP responses disable caching.

<a id="person-profiles-deployment-and-local-verification"></a>
#### Deployment and local verification

1. Review the additive `supabase/migrations/history/20261106000200_person_profiles.sql`
   against the target schema. It requires existing atomic storage and simple capture.
   Do not replay the destructive baseline. Remote application requires approval.
2. SQLite runs its separate timestamped migration under `supabase/migrations/sqlite/`
   once per explicitly chosen database. Include that directory when deploying the
   backend. No default local database directory is introduced.
3. Enable `PERSON_PROFILES_ENABLED=true` only after the active engine supports the
   profile contract. No local `.env` file is changed automatically. Existing reply
   policy and addressed-message save confirmations continue to apply.
4. Run offline checks from `backend/`:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q tests/test_profiles.py tests/test_sqlite_storage.py
   ```

Tests use disposable SQLite databases, mocked HTTP/AI, and the existing disposable
PostgreSQL/pgvector runner when its dependencies are installed. They do not connect
to hosted databases, use project credentials or send external messages. Hosted
migration, live model behavior and live delivery require separate verification.

Supabase function permissions follow the official
[database function guidance](https://supabase.com/docs/guides/database/functions):
fixed search paths, invoker security and explicit backend-only execution grants.

<a id="person-profiles-verification-recorded-for-this-implementation"></a>
#### Verification recorded for this implementation

- All 10 profile tests passed, including real disposable PostgreSQL transactions,
  timezone-independent replay, SQLite upgrades/imports, rollback, evidence cleanup,
  ingestion, identity/scope isolation and citation redaction.
- The broader backend run passed 389 tests with one skip and one deselection using
  `pytest -q --ignore=tests/test_message_viewer.py --deselect=tests/test_commitment_chat.py::test_authenticated_adapter_boundary_and_env_routes_fail_closed`.
- The complete suite remains blocked by committed merge-conflict markers in
  `test_message_viewer.py`. The unchanged `/viewer/env` access test expects 503 but
  receives 200. The five unchanged JavaScript viewer tests fail because their DOM
  stubs lack `classList.add` / `appendChild`. These issues were left outside this module.
- `pip check` and `git diff --check` passed. These initial implementation checks
  did not change hosted schema or local secret files and did not verify live
  profile extraction or external delivery.

<a id="person-profiles-approved-hosted-deployment"></a>
#### Approved hosted deployment

The user approved applying the additive profile migration on 2026-11-06 (Pakistan
time). Supabase recorded it as `20261105232021_person_profiles`; the applied SQL is
the reviewed local `supabase/migrations/history/20261106000200_person_profiles.sql`.
The generated hosted version differs from the local planned timestamp; no existing
migration history was rewritten. Do not reapply the same migration under its local
timestamp without reconciling migration history first.

The connected MCP project matched the backend's configured project. Preflight
confirmed the existing storage/capture functions and source constraints and the
absence of profile tables. After application, read-only checks confirmed:

- All four profile tables and the context view exist.
- RLS is enabled on every profile table.
- Anonymous and ordinary authenticated clients cannot read profile tables or
  execute the storage RPC; the backend service role has the required access.
- The evidence-maintenance trigger exists.
- The authenticated Supabase API advertises the profile storage RPC, tables and view.
- The active backend uses Supabase with `PERSON_PROFILES_ENABLED=true`.

No synthetic team records or external messages were created during these checks.
Live chat extraction/storage remains unverified. Retry a supported preference as a
new message and confirm it when prompted; the earlier failed confirmation draft
was removed by the existing confirmation flow.

<a id="person-profiles-sender-isolation-correction"></a>
#### Sender-isolation correction

A user-provided live transcript showed two participants receiving the same answer
to "my preferred name". The initial answer path used room-wide retrieval and did
not bind that question to the sender. The correction uses exact sender-scoped
reads and deterministic source-backed personal answers. It also separates pending
confirmations by sender. Existing profile UUIDs and stored data are preserved;
this correction introduces no migration or remote write.

The focused regression run passed 120 tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_profile_identity.py tests/test_profiles.py tests/test_useful_storage.py tests/test_ai.py tests/test_simple_capture.py
```

These checks include distinct IDs with identical display names, changed labels,
unknown senders, room isolation, conflicting/superseded names, Supabase subject
filters, personal queries that bypass the shared model/search, and independent
confirmation drafts. The corrected path still needs a fresh live query from each
participant after backend reload/restart. An integration delivering the same sender
ID for different people must be corrected at ingestion; usernames are not a safe
replacement for stable identifiers. Automatic nickname use in every unrelated
reply remains separate from source-backed preferred-name lookup.

<a id="feature-logging"></a>
## Feature logs

Consolidated from `FEATURE_LOGGING.md`.

<a id="feature-logging-feature-logs"></a>
### Feature logs

Feature tracing is enabled locally with `FEATURE_LOGS_ENABLED=true` and `LOG_LEVEL=INFO` in ignored `backend/.env`. The example file documents these non-secret switches. No business feature, delivery flag, policy, database migration or external integration was enabled by this logging change.

Restart the API and the separate Switch listener to load the console configuration:

```powershell
# Terminal 1, from backend/
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
# Terminal 2, from backend/
.\.venv\Scripts\python.exe -m app.switch_listener
```

Look for `[feature] component=... state=...`. Events are INFO-level on `team_memory.features`. Normal feature logging is separate from HTTP/WebSocket wire debugging, which remains disabled. There is no new file log or database logging table.

| Component | What the event proves |
| --- | --- |
| `startup` | Process starting/stopping; configuration summary only |
| `switch_listener` | Room connection, API acknowledgement, duplicate delivery or reconnect attempt |
| `ingestion` | Received, ignored agent/echo, durable journal persisted, duplicate, recovered, completed or failed processing |
| `ai` | Job queued/started, parsed decision and extraction counts, processing success or safe failure category |
| `confirmation` | Draft staged, saved after confirmation, cancelled or failed; saving a draft is not human approval of a task |
| `storage` | A storage acknowledgement returned with counts, or configuration/storage failure |
| `chat_commitment` | Candidate context prepared, proposed action, ambiguous evidence or denied authority |
| `commitment` | Authorized reads/history counts, requested action, conflict/denial, successful persisted change, equivalent no-op or receipt replay |
| `embedding` | Worker availability, claims, completed embeddings, stale claims or failures |
| `retrieval` | Semantic results or keyword fallback, with result counts |
| `viewer` | Authenticated socket connection, refusal and disconnection |
| `reminder_planner` | Worker availability, intentions persisted, deduplicated/rechecked work or no eligible work |
| `reminder_dispatch` | Claim, deferral, stale-state skip, durable send-start, retry/failure/ambiguity or saved acknowledgement |
| `reply` | Existing reply helper returned or failed; not proof of Discord receipt |

Examples of event shapes (illustrative, not evidence of hosted execution):

```text
[feature] component=commitment state=blocked reason=human_auth_unavailable status=503
[feature] component=chat_commitment state=disabled reason=flag_disabled
[feature] component=ai state=prepared action=store_only commitments=1 decisions=0 memories=0 proposed=0
[feature] component=commitment state=applied action=complete task=<hash> changed=true version=2
[feature] component=commitment state=replayed action=complete task=<hash> changed=true version=2
[feature] component=reminder_planner state=persisted ref=<hash> task=<hash> policy_version=1 version=2
[feature] component=reminder_dispatch state=deferred reason=quiet_hours_or_cadence ref=<hash> wait_seconds=3600
[feature] component=reminder_dispatch state=acknowledged ref=<hash> attempts=1
```

`configured` means settings are present, not that hosted functions, grants, mappings or external delivery were verified. `prepared` means parsing/context preparation, not a mutation. `commitment applied` is emitted only after the repository returns a validated lifecycle result. `replayed` describes the saved earlier result, so `changed=true` on a replay does not mean another write occurred.

`reminder send_started` means the durable marker was saved before calling Switch. It is not success. `acknowledged` is emitted only after a valid Switch receipt and successful database result save; it does not prove downstream Discord delivery or human receipt. A timeout or interrupted attempt can be `ambiguous` and is not blindly resent. `ingestion succeeded` means processing finished; a staged confirmation or `none` decision can finish without a Supabase write. Use `storage succeeded` and counts for storage evidence.

`ref` and `task` are stable short SHA-256 correlations. Raw user/actor identifiers, display names, task descriptions, message bodies, prompts, answers, reasons entered by users, credentials, URLs and remote exception bodies are not included in these new events. Only known event/reason/action values and bounded numeric/boolean metrics are emitted; unknown diagnostics become `other`. Legacy opt-in redacted message-preview logging remains separate and was not enabled. Do not turn on transport wire debugging.

Repeated empty-worker heartbeats are limited to once per minute per component. Disabled/blocked worker availability is logged on state changes; active work has individual transition events. Failed durable jobs are visible through later recovery/queue events and attempt counts, within the existing retry limits. Logging does not add database probes, model calls, sends or retries.

Automatic chat management, public human authentication and reminder deployment keep their existing gates. Disabled or blocked lines explain why those paths currently cannot run; enabling their logs does not activate them. See [REMINDERS.md](OPERATIONS.md#reminders) and [AUTOMATIC_COMMITMENTS.md](OPERATIONS.md#automatic-commitments) for deployment dependencies.

Set `FEATURE_LOGS_ENABLED=false` to turn off these extra events. Existing application diagnostics remain governed by their existing configuration. Tests use mocked/fake operations to check that failure/replay/ambiguity is not logged as a fresh success and that secrets/content cannot be reflected into trace fields.

<a id="document-ingestion"></a>
## Document ingestion and operator APIs

Consolidated from `supabase/DOCUMENT_INGESTION.md`.

> Local format/consent revision (2026-11-06): PDF, DOCX and PPTX extraction,
> channel-only replies and simple consent are implemented locally. The approved
> binary-format constraint migration is applied; service reload and live binary
> verification remain outstanding.
> [Current limits, citation semantics and rollout](OPERATIONS.md#document-attachments)
> supersede the original Markdown/text-only limits below.

<a id="document-ingestion-versioned-document-ingestion"></a>
### Versioned document ingestion

Document discovery and cited questions now reuse this module through the existing
authenticated Discord/Switch pipeline. See [document chat](OPERATIONS.md#document-chat) for
commands, sender-scoped selection, bounded retrieval, verification and rollout.
The standalone HTTP document API below retains its existing authentication.

The optional consent-based Discord collection adapter is now implemented locally
and disabled pending trusted setup/live approval. It reuses this deployed document
module; see [Discord attachment setup and verification](OPERATIONS.md#document-attachments).

Implemented and deployed, 2026-11-05. Hosted upload, actual vectors, automatic
publication, semantic citations and failure safety passed isolated verification.
The backend feature is enabled; its source-code default remains disabled for new
installations. See [deployment evidence](VERIFICATION.md#document-deployment).
Message and commitment records and the ingestion journal remain separate.

<a id="document-ingestion-access-input-and-contracts"></a>
#### Access, input and contracts

All `/api/v1/documents` routes require existing `X-Admin-Token` authentication.
This is a restricted development-admin facility, not individual-user upload
authorization. Never put that token in a URL or mobile application. Required
`room_id` query parameters identify a Switch room. The backend resolves its
configured tenant, allow-list and enabled database mapping. Callers cannot choose
a tenant or importing actor. Recorded actor is `development-admin`, not a human.
Every database operation rechecks enabled tenant/room scope.

Upload raw UTF-8 `.md`/`.txt` bytes, maximum 262,144 bytes, using
`application/octet-stream` or `text/plain; charset=utf-8`. Metadata query fields:
`import_id` UUID, `source_key` (stable source identity), `filename`, `title`,
`expected_version`, and `room_id`. Strings are limited to 200 characters.
Filenames are metadata, never server paths. A client reads a local file and
uploads bytes; the server never follows links, downloads embedded resources,
executes instructions/frontmatter, or renders trusted HTML. Source text and line
endings are preserved. Invalid UTF-8, binary NULs, empty input, unsupported suffixes,
excessive size and excessive processing are rejected. No document text is logged.
Two imports may process concurrently; further admissions receive `429`.

| Interface | Contract |
| --- | --- |
| `models/documents.py` | Trusted scope and explicit import, document, version, chunk, search and citation models |
| `DocumentRepository.call(scope, operation, data)` | Persistence boundary shared by in-memory tests and Supabase RPC adapter |
| `DocumentService.import_bytes(scope, metadata, raw)` | Validate, chunk and atomically persist; return stable identifiers and processing state |
| `chunk_text(text, tokenizer)` / `local_chunks(text)` | Exact slices, SHA-256 hashes, character offsets and one-based inclusive line locations |
| `DocumentWorker.process_once(scope)` | Claim <=4 document chunks; finish with lease token, content hash and model-recipe guards |
| `DocumentService.search(scope, DocumentSearch)` | Dedicated published-document retrieval adapter with machine-verifiable source references |

Repository operations: `import`, `import_status`, `documents`, `document`,
`versions`, `version`, `chunks`, `chunk`, `archive`, `claim`, `finish`, `search`.
The chat-only `catalog` adapter operation adds a scoped metadata read; it is not
a new SQL RPC operation.
Runtime uses `SupabaseDocumentRepository`. `MemoryDocumentRepository` is for tests
only and does not acknowledge durable production storage.

Reused components: admin auth, trusted room resolution, privileged-key headers,
HTTP pool, pinned actual tokenizer, 384-D vector validation, loaded local model,
model lock and single inference executor. No new dependency or second model
session is added. The adapter remains independently callable through the search
API. The document chat service also uses it for explicit document questions;
general room-memory retrieval remains separate.

<a id="document-ingestion-identity-chunking-recovery-and-publication"></a>
#### Identity, chunking, recovery and publication

Documents are unique by `(tenant_id, room_id, source_key)`. Source format stays
stable. Imports use idempotency UUIDs and request hashes; reusing a UUID with
different parameters returns `409`. `expected_version` is the latest **accepted**
number, initially zero. Concurrent imports with the same expected number cannot
silently overwrite each other. Exact request replay returns the original version.
Unchanged latest content/hash/recipe/title reuses its version and chunks. Changed
content creates a numbered version. Returning to older content creates a new
version; identical text in different documents retains independent provenance.
Title changes are versioned; filename-only changes do not rewrite history.
Recipe changes deliberately create a new version on reimport.

One transaction persists source, version, chunks/jobs and import identity.
HTTP `202` means recoverable acceptance, **not** publication. Source survives
embedding failure and application restart.

Recipe `source-slices:v1:target448:overlap32:paragraph-heading` targets 448 actual
tokens, including special tokens, with a hard 512-token model limit and at most
32 source-token overlap. Paragraph/heading boundaries are preferred where practical.
No heading/context is added to the embedding input: supporting text is exactly
the embedded source slice. Oversized paragraphs split deliberately. Section labels
are bounded metadata; complete headings remain in source. Maximum 1,024 chunks
and 64,000 characters per inference input. Pathological whitespace that cannot
form a bounded nonempty input fails explicitly before acceptance, never silently
losing source text. Embeddings use the existing pinned FP32 ONNX, mean-pooling,
L2-normalization recipe from `core/embedding_model.py`.

Chunk jobs have five-minute leases, unique claim tokens, three attempts and
thirty-second backoff. Expired leases recover with new tokens; exhausted claims
report `lease_exhausted`. Failures use allow-listed categories. Only the latest
accepted version claims or finishes work. New imports mark unfinished older
versions `failed/version_superseded`; historical completed chunks remain intact.
Stale tokens, hashes, model recipes, versions and expired leases cannot publish.

After all chunks complete, the latest version is published atomically. The previous
published version stays searchable during replacement and failure. History and
source are retained indefinitely in this first release. Archive excludes retrieval
without deleting evidence. There is no permanent-deletion endpoint. Foreign keys
use `ON DELETE RESTRICT`; any separately authorized deletion must clear the
publication pointer, then explicitly handle imports, chunks, versions and documents.

<a id="document-ingestion-api-and-citation-verification"></a>
#### API and citation verification

Paths below start `/api/v1/documents`. All require admin auth and authorized
`room_id`. Lists use `limit` 1–50 (default 20), `offset` 0–10,000 and stable ordering.
Private responses use `Cache-Control: no-store`.

| Method/path | Result |
| --- | --- |
| `POST /imports` | Raw bytes; `202` import/document/version IDs, duplicate flag, processing status |
| `GET /imports/{import_id}` | Durable import status |
| `GET` on the prefix | Scoped document list |
| `GET /{document_id}` | Identity, latest accepted number, published pointer and active status |
| `GET /{document_id}/versions` | Historical versions and source |
| `GET /{document_id}/versions/{version_id}` | Source/hash/recipe, processing status, safe error |
| `GET /{document_id}/versions/{version_id}/chunks` | Exact slices and job state |
| `GET /{document_id}/versions/{version_id}/chunks/{chunk_id}` | Inspect a citation's supporting text |
| `POST /{document_id}/archive?expected_version=N` | Compare version, then archive |
| `POST /search` | JSON `query`, optional `document_id`, `limit` 1–20 |

Errors: `401`/`403` for authentication/scope, `404` for inaccessible identities,
`409` for stale versions/idempotency conflicts, `413` for size, `415` for format,
`422` for invalid input, `429` for saturated import admission, `503` for disabled
or unavailable processing.

Search filters tenant, enabled room, optional document ID, active status,
published version, completed state, content hash and model recipe **before**
ranking/limiting. Results contain exact text, title, section, line range, score,
`source_type="document"` and citation
`document:{document_id}:{version_id}:{chunk_id}`. This cannot be confused with a
message UUID citation. Verify identifiers through chunk inspection and compare
supporting text; a model-generated identifier alone is not provenance proof.
Exact cosine search uses `<=>`. No ANN index or production-scale claim is made.
Benchmark realistic scoped latency/recall before adding HNSW `vector_cosine_ops`.

<a id="document-ingestion-deployment-contract-and-completed-execution"></a>
#### Deployment contract and completed execution

Read-only hosted inspection found existing memory embedding functions but no
document/version/chunk tables. The additive migration is
`migrations/history/20261105163303_versioned_documents.sql`: four new tables, composite
scope/source constraints, queue/scope indexes and backend-only `document_operation`
RPC. It changes no existing message or commitment object. All document tables
enable RLS and revoke `PUBLIC`/`anon`/`authenticated` privileges. Backend
`service_role` gets access. The RPC is `SECURITY INVOKER`, has fixed search path
and backend-only execution. No public policies are introduced.

1. Review this migration and current hosted state. Coordinate with commitment
   work; this workspace currently contains no commitment lifecycle migration.
2. Obtain approval for this migration only. Never replay the destructive core
   baseline, run indiscriminate `db push`, rewrite migration history or change
   paid resources. Record only the actual additive deployment.
3. After separate approval, run from `backend/`:
   `python -m app.verify_document_fixture --run-approved-fixture`.
   The harness preflights all document tables and RPC before writing. It exercises
   actual authenticated FastAPI handlers, mapping checks, local vectors,
   paraphrase search, citations, unchanged dedupe and safe replacement. It cleans
   only its generated tenant/room and reports cleanup failure safely. It never
   sends Switch/Discord messages or opens the message ingestion journal.
4. Only after verification, set `DOCUMENT_INGESTION_ENABLED=true` in ignored
   backend configuration and restart FastAPI. Keep `LOCAL_EMBEDDINGS_ENABLED=true`
   and pinned model assets installed. No credentials belong in client config.

These deployment steps were completed with approval in the configured project;
see the execution record above. New environments still require their own review,
approval and verification. Future changes to applied SQL require new migrations.

<a id="document-ingestion-verification-and-limitations"></a>
#### Verification and limitations

Executed: domain/API tests with fake embeddings and a controllable clock; real
local tokenizer/model import → publication → paraphrased search → exact citation
verification using the in-memory repository; disposable PostgreSQL/pgvector
schema, transaction, lease, isolation and permission tests. Missing hosted schema
preflight is tested to create zero rows. Existing message-storage and embedding
SQL suites also pass with the additive document schema installed.

Final results: **223 backend tests passed**, including both opt-in real-model
tests (one existing test-library deprecation warning). The document suite accounts
for 21 tests. `node document-schema.mjs` passed the document contract suite and
both existing useful-storage/embedding SQL suites. Syntax and diff checks pass.

Two broader baseline checks failed in unchanged files: `fresh-schema.mjs` stops
inside `database_audit.sql` on a message/commitment `ON DELETE RESTRICT` fixture
cleanup error, before loading any document migration. All five viewer JavaScript
tests fail because their DOM mocks lack existing viewer controls (`classList`
and `appendChild`). No document module code runs in that viewer harness. These
separate audit/viewer files were not modified for this task; those checks are
not reported as passing.

Run backend tests with `python -m pytest -q`; set `RUN_LOCAL_MODEL_TESTS=1` for
actual ONNX/tokenizer tests. From repository root run
`npm run test:documents --prefix supabase/tests` and
`npm test --prefix supabase/tests`. Disposable runners never load credentials or
connect remotely.

Hosted document writes/search and automatic processing are now verified and
enabled; current counts/results supersede the earlier local-only totals in this
document. No frontend upload UI, public individual-user routes, providers,
PDF/OCR, edits/approval, conflict detection, automatic Discord document synthesis,
commitment lifecycle changes, permanent deletion, retention cleanup, manual
failed-job retry API or throughput/corpus benchmark is included.

<a id="document-attachments"></a>
## Chat uploads and file-format support

Consolidated from `supabase/DISCORD_DOCUMENT_INGESTION.md`.

<a id="document-attachments-discord-document-consent-and-ingestion"></a>
### Discord document consent and ingestion

Current local implementation, 2026-11-06. This revision changes channel replies,
consent and supported formats. Its approved format migration is applied to the
configured Supabase project, and backend dependencies are installed in the project
venv. Shared service reload and live binary-flow verification remain outstanding.
No live messages, real team-document changes, deployment setting changes or shared
service restarts were performed. Earlier Markdown/text ingestion was reported
working by the user; that does not verify the binary formats in the hosted flow.

<a id="document-attachments-conversation"></a>
#### Conversation

```text
User: [uploads planning.pdf]
Bot: Save planning.pdf to team memory? Reply yes or save, or no to skip.
User: yes
Bot: Processing planning.pdf…
Bot: Saved planning.pdf to team memory.
```

All document prompts, updates, errors, ambiguity explanations and success replies
go to the originating room/channel. No thread/reply parameter is sent to Switch.
Original upload/thread metadata remains in the request's provenance. Delayed or
retried outbox entries from older versions are rendered again from request state;
their stored reference-bearing text and thread destination are ignored.
Unrelated conversational replies retain their existing routing.

The Switch documentation MCP confirms thread correlation is explicitly carried
by the replied-to event ID. The verified `post_message` operation accepts `body`
and optional `thread_id`: omission is a top-level post. The adapter now sends only
`body`, retaining its authenticated connection/session headers and the existing
per-room session bindings. It opens no separate connection and has no alternate
send fallback after an uncertain result. See
[thread routing](https://docs.switchagents.ai/switch-rooms/internals/collaboration-bridge#threads-edits-and-deletes).

<a id="document-attachments-deterministic-uploader-only-consent"></a>
#### Deterministic uploader-only consent

Complete phrases, case-insensitive with terminal punctuation:

- Save: `yes`, `save`, `save it`, `yes please`.
- Decline: `no`, `skip`, `cancel`, `don't save`.
- Choose a filename: `save planning.pdf` or `skip planning.pdf`.

Words embedded in longer sentences do not authorize anything. No LLM infers
consent. The listener and API share the same parser. With this feature enabled,
complete consent commands go to the private document route; a command without a
pending upload is ignored and cannot create an import or fall into AI processing.

Eligibility binds the authenticated sender's exact opaque ID, tenant, room and
bridge. It does not depend on display names, personal registration, member mappings,
a human/agent classification or the thread where the response was typed. Known
self/bot identities remain excluded. Ingest authentication plus the distinct
listener-only adapter credential and configured room/bridge boundaries remain.
A shared proxy ID can only prove ownership by that shared identity, not distinguish
people behind it. Never give adapter credentials to end users.

One complete, unexpired pending request is selected. A complete attachment group
is one request listing its files. Multiple requests prompt a filename choice;
a filename occurring in more than one pending request leaves all unchanged.
The bot explains that these requests must expire before uploading one copy to
resolve duplicate-name ambiguity. No internal reference is required or displayed.
Internal references remain in logs, idempotency keys and protected operator APIs.
Legacy reference commands are accepted only for compatibility with prompts already
delivered before the upgrade; new replies never advertise them.

<a id="document-attachments-atomic-recovery-atom"></a>
#### Atomic recovery (ATOM)

A local SQLite `BEGIN IMMEDIATE` transaction selects the eligible request,
stores the confirmation message identity/fingerprint and selected request, changes
state, and enqueues the status reply together. An identical confirmation replay
returns its original outcome even after publication or a restart; it cannot
consume another upload. Conflicting reuse of a message identity is rejected.
Ambiguous and no-pending outcomes are also bound durably, so an old `yes` does
not become valid after another upload arrives. Rollback leaves all state unchanged.

The confirmation ledger is bounded at 100000 entries and fails closed at capacity;
request/outbox limits remain. This is a private, persistent single-host SQLite
ledger, with one API worker and one listener required. Do not erase it to retry.
The live-only listener policy is preserved: a newly started listener skips offline
events; transient reconnects resume within that process. Accepted imports and
outbox work recover independently. Incomplete or expired requests cannot be saved.

Imports reuse the existing Supabase RPC transaction for source, version, chunks
and idempotency. Embedding publication remains atomic. A failed replacement leaves
the previously published version available. Success is sent only after all files
are completed, active and current. Queued messages may be superseded by final state.
An uncertain outbound send can repeat a reply; exactly-once chat delivery is not
claimed. Confirmation replay does not create duplicate documents.

<a id="document-attachments-formats-extraction-and-limits"></a>
#### Formats, extraction and limits

Supported: UTF-8 `.md`/`.txt`, text-based `.pdf`, modern `.docx` and `.pptx`.
Original filenames and formats remain in document/version metadata, and Switch
source identities remain in attachment provenance. Only searchable extracted text
is stored; original binary files are not separately archived in Supabase Storage.

PDF uses pinned `pypdf==6.21.4`. DOCX/PPTX use standard-library ZIP and XML parsing,
not new Office dependencies. DOCX body paragraphs/tables and PPTX slide text/tables
are extracted in document/slide order. Headers, notes, chart data, images, OCR,
legacy `.doc`/`.ppt`, and macro-enabled files are outside this implementation.
PDF layout/reading order depends on its text structure.

Actual signatures, XML roots, package content types and relationships are checked.
Password-protected/encrypted, malformed, macro/embedded-object packages, DTD/entity
XML and unsupported formats are rejected. External links are never resolved or
fetched; macros and embedded objects are never executed. Empty/scanned documents
receive a no-searchable-text explanation. An extension/MIME declaration alone is
not accepted as proof of file format.

Limits are enforced across HTTP upload, download and parsing:

| Boundary | Limit |
| --- | --- |
| Original upload/download | 8 MiB hard maximum/default |
| Extracted UTF-8 text | 256 KiB and 262144 characters |
| PDF pages / PPTX slides | 100 / 200 |
| DOCX paragraphs | 10000 (rendered page count is not inferred) |
| ZIP members / total expanded bytes | 1024 / 32 MiB |
| Individual archive member / decoded PDF stream | 4 MiB |
| Parser processes / wall time / memory per process | 2 / 20 seconds / 512 MiB |
| Chunk count | Existing 1024 maximum |
| Files per request | Existing default 4, configurable up to 8 |

Binary parsing runs outside the event loop in disposable subprocesses. Windows
Job Objects / Unix address-space limits enforce the memory bound. Timeouts kill
and reap the parser. Cancellation waits for its bounded completion/timeout before
releasing the process slot. ZIP members are bounded and read only
in memory, never extracted to disk. Resource-limit setup failure rejects parsing.
No download, extraction, content storage or embeddings happen before consent.

`SWITCH_DOCUMENT_MAX_BYTES` may lower the 8 MiB maximum; the same configured limit
applies to HTTP imports and Discord download checks. Existing ignored `.env`
settings were not changed; a previously configured 256 KiB override remains lower
until the operator explicitly updates it. Extracted text keeps the existing
Postgres 256 KiB constraint. Parser docs:
[pypdf extraction](https://pypdf.readthedocs.io/en/stable/user/extract-text.html),
[Python XML security](https://docs.python.org/3/library/xml.html#xml-security).

Citations refer to stored text lines, including extracted-text lines for binary
formats. Search responses explicitly carry `location_basis=stored_text_lines`.
They do **not** claim original PDF pages, DOCX pagination or PowerPoint slide
positions. Original source metadata is retained separately. Document retrieval
through the existing scoped search API remains available independently. Explicit
document commands and supported sender-specific follow-ups now use the local
[document chat pipeline](OPERATIONS.md#document-chat); ordinary room-memory replies remain separate.

<a id="document-attachments-applied-migration-and-remaining-service-rollout"></a>
#### Applied migration and remaining service rollout

Applied with explicit user approval:
`supabase/migrations/history/20261106095848_document_binary_formats.sql`.
It transactionally expands the existing `documents.source_type` check to
`markdown`, `text`, `pdf`, `docx`, `pptx`. It does not replace the RPC, delete
rows, reset tables, change RLS/grants or alter existing source/version/chunk data.
The earlier versioned-document migration was not edited. The original consolidated
baseline now declares the same formats for fresh installs, as explicitly requested;
never replay that destructive baseline on hosted data.

Schema/dependencies are ready. Before live use, review any lower local upload
override and explicitly authorize reloading/restarting the shared services. Keep one listener
and one API worker. Then approve a disposable Discord fixture test: upload each
format, reply `yes`, verify a channel-only prompt/update/success, scoped search
and a citation, another sender's rejection, filename ambiguity and a decline.
Do not use a legacy failed reference for a new upload. The extraction tests use
non-sensitive fixtures under `backend/tests/fixtures/documents/`.

<a id="document-attachments-local-verification"></a>
#### Local verification

The local verification report distinguishes real SQLite transactions, actual PDF/
OOXML parser subprocesses, real local ONNX inference, disposable PostgreSQL and
mocked Switch/hosted document repositories. No live Discord/hosted binary ingestion
has been verified. Terminal `document stage=...` logs retain internal references
and fixed failure categories but never file contents, credentials, media handles
or raw parser/remote exception text.


Final verification: **570 backend tests passed** with `RUN_LOCAL_MODEL_TESTS=1`.
This includes a real PDF parser process through local ONNX embeddings, publication
and cited search using mocked Switch and an in-memory document repository; actual
DOCX/PPTX parser processes; real SQLite replay/concurrency/rollback tests; and
existing messaging, commitments and reminders. A missing already-declared local
`tzdata==2026.6` dependency was restored before the final run; no date logic changed.
One pre-existing test-client deprecation warning remains.

Disposable PostgreSQL/PGlite checks passed document contracts, the additive format
migration and all three format/idempotency cases, useful storage and embedding-job
regressions. The schema harness detects documents already present in the consolidated
baseline instead of replaying their earlier creation migration. No applied migration
was edited. `git diff --check` passes. No Git commit or push was made.


<a id="document-attachments-approved-hosted-schemadependency-update-2026-11-06"></a>
#### Approved hosted schema/dependency update — 2026-11-06

The user explicitly approved applying the format migration and updating the original
consolidated SQL. Supabase recorded `20261106095848_document_binary_formats.sql`.
Only that additive transaction ran remotely; the destructive original baseline
was never replayed. The original `20261104000100_initial_schema.sql` now declares
the same five source formats for **fresh installations only**, under the user's
explicit exception to migration immutability. The already-applied versioned-document
migration remains unchanged. Never rerun the original baseline on hosted data.

Backend/dev/local-embedding requirements are installed in the existing project
`.venv`; `pip check` reports no broken requirements. PDF parsing is pypdf 6.21.4.
Use that environment to run the backend. The backend-only RPC grants were verified
unchanged. No feature settings, ignored `.env` limits, services or real team documents
were changed, and no live message was sent during this deployment step. Shared
service reload and live binary ingestion verification remain separate steps.
This section supersedes the earlier pending-migration statements above.


<a id="document-attachments-windows-uvicorn-reload-parser-fix-2026-11-06"></a>
#### Windows Uvicorn reload parser fix — 2026-11-06

A live DOCX import downloaded successfully but reached a generic worker warning.
The active backend uses Uvicorn `--reload`; its Windows Selector event loop does
not support `asyncio.create_subprocess_exec`. Tests using `asyncio.run` previously
used the Proactor loop and missed that deployment-specific exception.

Binary parsers now launch with `subprocess.run` inside `asyncio.to_thread`. The
same two-process semaphore, 20-second hard timeout, child memory limits and output
limits remain. Timeout kills/reaps the child. Cancellation retains the slot until
the bounded worker has finished, preventing orphaned work or concurrency overruns;
a cancelled import does not proceed to storage. No new dependency or schema change
is required. A real DOCX parser regression runs explicitly under the Windows
Selector loop. Unexpected worker diagnostics now include only the exception class,
never raw exception contents or tracebacks.

An existing in-flight request can recover when its five-minute lease expires;
confirmation and stable import identity are retained. Never delete the journal
or create another listener to retry it.


Verification for the Windows fix: **571 backend tests passed** with actual local
inference enabled, including the Windows Selector-loop parser regression. The
user-confirmed live DOCX request `78c12dc332b4` recovered automatically on attempt 2,
reached `published`, and all four request outbox entries were marked sent. The
journal/confirmation was not reset and no upload/consent was fabricated. This
provides a real hosted DOCX publication result beyond mocked parser tests; it does
not establish live PDF or PPTX verification. Only publication metadata was checked,
not the user's document text. No migration or feature-setting change was needed.

<a id="document-chat"></a>
## Document discovery and questions

Consolidated from `supabase/DOCUMENT_CHAT.md`.

<a id="document-chat-document-discovery-and-questions-in-discord"></a>
### Document discovery and questions in Discord

Implemented locally, 2026-11-06. No hosted changes, shared-service restarts,
real document edits or live Discord verification were performed.

<a id="document-chat-conversation"></a>
#### Conversation

Address the existing bot using the normal supported mention mechanism:

```text
You: @team-memory-client List our documents.
Bot: 1. Launch plan (planning.pdf)
     2. Launch deck (launch.pptx)
     Reply with a number or filename.
You: @team-memory-client Find the presentation about our launch.
Bot: Selected Launch deck (launch.pptx). Ask a question about it.
You: @team-memory-client What does planning.pdf say about the deadline?
Bot: [Answer from the retrieved passages]
     [Document: Launch plan (planning.pdf); stored text lines …]
You: @team-memory-client Who is responsible?
Bot: [Answer about that same document, with a document citation]
You: @team-memory-client Summarize it.
Bot: [Summary with citations]
     Partial summary of sampled passages; some sections may be omitted.
You: @team-memory-client clear selection
Bot: Document selection cleared.
```

Discovery may instead show several numbered choices or report no convincing
match. Choose a displayed number or an unambiguous filename/title. Use `select
Launch plan`, `use planning.pdf` or `switch to planning.pdf` to change documents.
Quote filenames containing spaces: `find "Launch Plan.pdf"`. Use `more documents`
for the next list page. Numbers refer to the most recently displayed page/choices
for that sender. Bare `yes`/`save` remain upload/draft consent, never selection.

Mention the bot to begin a document conversation. While that same sender's
tenant/room conversation is active, subsequent replies need no mention. Both `1`
and `1 can you explain whats in it` go through AI document routing. A combined
choice/question uses an ask/summary intent with the displayed choice and can
answer in the same turn. Unrelated chatter receives no unmentioned document reply.
After expiry, clear or API restart, begin again with a mentioned request; previously
displayed choices are not restored from old channel messages.

Requests are interpreted by the existing AI gateway rather than matched against
fixed phrases. For example, “what is in our shared library?” can mean a document
inventory, “the second one” can choose a displayed document and “give me the gist”
can request a summary. Those are examples, not required wording. Inventory still
comes from scoped database metadata, without inventing uploader ownership or
historical upload filters the repository does not support.

<a id="document-chat-reuse-and-boundaries"></a>
#### Reuse and boundaries

The path remains Discord → Switch → existing listener → authenticated `/messages`
→ AIService → DocumentChat → existing Switch channel reply. No new endpoint,
registration, member mapping, sender classification or messaging integration was
added. Existing self/AI echo guards and authenticated adapter metadata remain in
force. Beginning a conversation requires the normal bot mention/addressing
behavior. Unmentioned continuations additionally require the sender's own active
conversation; the model cannot create that authorization by proposing an intent.

Existing imports, immutable versions, chunk embeddings, publication, document-only
semantic search, room resolution and private `ask_switch(..., synthesis=True)`
gateway are reused. General memory, commitment, profile and attachment-consent
paths remain separate. Before general reasoning, an enabled addressed-message
or authorized active-continuation path asks the existing private gateway for a structured document intent using the
bounded message, selection/choice availability and two short context turns. No
document content or raw tenant/room/document IDs are provided for classification.
The `DocumentIntent` model rejects unknown actions/fields, conflicting targets,
invalid choice numbers and oversized values. A named reference must be copied
from the current message. Backend name resolution, scope, current-publication
checks and cached choice validation remain authoritative.

The classifier can propose list, more, find, select, ask, summarize, clear,
clarify or none. Only a valid none proposal permits ordinary reasoning to resume;
malformed routing requests clarification rather than silently querying memories.
A selected document does not force unrelated questions into document mode. Known
profile/task workflows take precedence, and deterministic attachment/draft consent
remains outside classification. Interpretations are probabilistic: these checks
validate authority and structure, not the model's understanding of every message.

Listing uses metadata, never AI-generated inventory. A small backend-only
repository `catalog` operation checks the existing RPC's room authorization,
then reads active documents joined to their current completed published version
and enabled room. Queries explicitly constrain tenant and room and return
title/filename/publication IDs, without source text or vectors. It requires the
existing Data API tables, grants and `documents_published_version_fk` relationship;
no database migration is needed. No privileged client credentials are exposed.

Discovery combines bounded title/filename metadata and existing semantic hits,
grouped by document. Exact names resolve directly. Vague candidates are judged
from their labels/passages by the existing reasoning gateway, rather than an
invented similarity threshold. This judgment is probabilistic and can be wrong;
use an exact filename if the proposed choice is incorrect.

Questions retrieve only the selected document's current published version.
Scope, activity and publication are checked before retrieval and after synthesis.
Publication changes during synthesis discard the answer and ask for a retry;
lost access, archiving, deletion or unpublication clears selection. A replacement
published between questions resets conversational history. Unpublished replacement
work does not hide the previous successfully published version.

The model receives bounded passages and at most two short document-related turns.
Historical generated answers are explicitly context, not evidence. Prompts treat
all supplied text as untrusted data, never instructions. A structured response
must identify allowed evidence references; missing/unsupported/malformed sources
produce a no-reliable-answer response. Readable source labels and exact stored
text lines are rendered by the backend, then use the existing secret redaction
and reply cleaning. No original page/slide coordinates are inferred. Reference
validation does not independently prove every model-generated claim is correct.

<a id="document-chat-state-limits-and-recovery"></a>
#### State, limits and recovery

- Selection/choices: process-local, keyed by tenant, room and stable Switch sender.
  Expires after **15 minutes of document activity**, API restart or bounded-cache
  eviction. At most 200 sender scopes; no full documents retained.
- Two context turns, each question/answer capped at 500 characters. A new document
  or published version clears history. Failed requests do not commit partial state.
- Source-message replay guard: 2,048 identities for 30 minutes. An old selection
  message cannot act on a newer displayed list within this window; the existing
  durable ingestion journal also prevents normal duplicate delivery/recovery.
- Use **one API process** for consistent conversational selection. Multiple
  independent workers do not share selections. Uploaded documents remain durable
  regardless of selection expiry or process restart.
- Lists: five entries/page, bounded offset up to 10,000. Discovery reads at most
  50 initial metadata entries plus a bounded exact-name lookup, and 20 semantic
  chunk hits. At most eight grouped candidates are considered and five shown.
  Discovery is bounded, not an exhaustive full-corpus search.
- Questions: up to eight retrieved passages, each at most 2,000 characters;
  total evidence at most 16,000 characters. At most three actual passage citations.
  Enabled addressed messages and authorized active continuations use one bounded intent-classification gateway call.
  Listing/exact selection require no further AI call; a resolved question adds one
  synthesis call, and vague discovery plus answering adds at most two. Unrelated
  requests continue the normal reasoning flow after classification. All calls
  reuse the existing private gateway, timeout and model; no new provider/model is
  installed. Known task/profile workflows and save/skip consent bypass this call.
- Summaries: source-order samples spread from beginning to end, at most eight
  chunks from the existing maximum 1,024. Always labeled partial; not a promise
  of complete coverage or a substitute for reading the document.

<a id="document-chat-setup-and-local-verification"></a>
#### Setup and local verification

No new dependencies, models, SQL scripts or environment variables are required.
Use existing `DOCUMENT_INGESTION_ENABLED`, `LOCAL_EMBEDDINGS_ENABLED`, configured
tenant/room mappings and private AI gateway setup. The listener must send its
existing dedicated `SWITCH_ADAPTER_TOKEN` in addition to ingest authentication;
the trusted token must have at least 32 characters and differ from shared/admin
credentials. Use ignored backend configuration, never chat or a client build.

Upload consent uses `SWITCH_DOCUMENT_ADAPTER_TOKEN`; ordinary document questions
use the separate `SWITCH_ADAPTER_TOKEN`. An upload startup message reporting
`adapter_configured=True` therefore does not verify chat authentication. Listener
startup now reports `general_adapter_ready` separately. If it is false, configure
the general credential in the ignored local configuration and reload both API
and listener. Explicit document questions fail with a setup explanation instead
of falling back to unrelated memories when adapter identity is unverified.

After review, the operator must load the updated backend code and verify with
approved disposable published documents in Discord. Shared services were not
restarted by this implementation. The hosted metadata catalog path was
subsequently verified through read-only backend requests during troubleshooting,
without reading source text or modifying documents. Live gateway document
relevance/answer quality and the updated end-to-end chat flow remain rollout checks.

Local commands from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_document_chat.py -q
$env:RUN_LOCAL_MODEL_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use fake AI/HTTP responses, scoped in-memory document storage and temporary
SQLite ingestion journals. The opt-in test performs real installed local-model
embedding/retrieval/publication, with fake answer synthesis. The disposable
PostgreSQL/pgvector suite (`node document-schema.mjs` from `supabase/tests/`)
checks existing document/embedding/storage contracts and the metadata join.
Neither test path contacts hosted services or sends live messages.

Verification for this implementation: full backend run **595 passed** (including
real local model tests); after the final filename/Unicode regression addition,
the final document-chat run **25 passed**, including real local inference. The
disposable PostgreSQL document, storage, embedding and format suites also passed.
The only reported warning is the existing Starlette/httpx test-client deprecation.

Saved-inventory/authentication troubleshooting regression run: **601 passed,
5 skipped**. This run covered the original saved-document question, missing
adapter fail-closed behavior, authenticated delivery and separate startup
credential diagnostics. Opt-in local-model cases were skipped in that run.
The ignored local configuration was repaired without restarting shared services;
both API and listener must load matching general adapter configuration.

AI-intent routing revision: **637 backend tests passed**, with opt-in real local
embedding tests enabled. Classifier and answer responses are mocked for repeatable
behavior tests, including indirect wording without document keywords, ordinals,
follow-ups, malformed/unauthorized proposals, consent and unrelated-request routing.
This verifies backend routing and enforcement, not live model intent accuracy.
The revised prompts still need fresh live Discord verification after API reload.

Unmentioned-continuation regression run: **642 passed, 5 skipped**. Tests cover
bare choices, combined choice/questions, no-context/expired/restarted conversations,
sender/room/tenant isolation and unrelated unmentioned chatter. External AI and
delivery remain mocked; opt-in real-model tests were skipped in this run.
