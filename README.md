# Team Memory â€” Clean Foundation

A project scaffold for **FastAPI + Supabase + React Native (Expo)** with a Switch listener for verifying Discord-to-API message delivery.

The implemented flow receives Discord messages through Switch, journals human input durably, displays bounded redacted previews, and evaluates useful information through the existing private Codex runtime. Atomic storage and versioned embedding jobs are defined in the single consolidated development baseline. The hosted non-destructive upgrade is prepared for review and approval; never replay the baseline reset on the hosted database. FastAPI and the Switch listener run as separate processes. See [architecture.md](architecture.md) for runtime boundaries, recovery, storage, retrieval and reply behavior.

## Structure
- `architecture.md`: design and boundaries.
- `AGENTS.md`: mandatory AI development and security rules.
- `backend/`: FastAPI MVC-inspired layers and tests.
- `supabase/migrations/`: ALL versioned SQL schema migrations.
- `mobile/`: Expo TypeScript starter screen (backend health-check only).

## 1. Backend setup (Windows PowerShell)

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

### Receive Discord messages through Switch

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

### Ask the existing Switch AI and receive answers only in the viewer

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

Every request verifies that the internal room has no bridge or external channel and contains only Codex and the client. It also checks Codex's reported sessions for live input-room bindings, and checks that every configured input room contains the listener without Codex. Missing runtime evidence, an active input-room session, or re-invitation causes a diagnostic before another question is posted. Stop the Discord-attached Codex session in Switch Console first, then rerun `python -m app.switch_setup`. Setup refuses to report success while an active input-room session is reported. Requests never fall back to an input room. Codex remains available in the private response room. Ask questions using the viewer's question box or **Ask Codex about this message**. Keep the internal room unbridged. The listener uses `SWITCH_REQUESTER_*`, preserving `SWITCH_AGENT_*` for the actual AI. Restarting the listener after an identity change starts a separate cursor; its old identity's messages are not replayed automatically.

Automated tests cover sender/thread correlation, responsive heartbeats, authentication, scope isolation, redaction, request deduplication, and setup recovery.

### Observe other messages and agent replies

`GET /messages/recent` now adds `sender_kind` (`ai` or `message`) and nullable `thread_ref` to each preview. `thread_ref` is a hash of the Switch thread identifier scoped by tenant and room, using the same reference format as messages. When it matches a retained message, the viewer displays that message as thread context. Otherwise it says that earlier context is unavailable. It never guesses which question an unthreaded answer belongs to, and a thread reference identifies conversation context rather than an exact question/answer pair. Raw participant and thread IDs are not returned by the feed. `/messages/status` retains its existing response shape and can return the latest message from either a person or an agent.

Messages in the existing feed have the same authorization, redaction, 2,000-character preview limit, 100-message retention, and restart behavior as before. `SWITCH_AI_SENDER_IDS` can additionally label other exact agent participant IDs; the learned `SWITCH_AI_SENDER_ID` is included automatically. The badge identifies a configured sender rather than proving that every post was generated by a model. These observed room messages are separate from the server-only request results above.

The transport and identity behavior follow the official Switch [event catalog and participant IDs](https://docs.switchagents.ai/switch-rooms/internals/agent-events), [agent operations](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol), and [room routing](https://docs.switchagents.ai/switch-rooms/internals/rooms-and-resources). For an unavailable responder, see [agent troubleshooting](https://docs.switchagents.ai/switch-rooms/using/agent-not-answering).

**Receipt means durable local acceptance, not successful Supabase storage.** Human source text is credential-redacted before journaling or prompting. The viewer keeps at most 100 previews, capped at 2,000 characters; ingestion retains the admitted redacted source up to 64,000 characters. Preview history resets on API restart, while jobs and replay identities survive. The listener saves progress only after acknowledgement and keeps heartbeats independent of processing. Do not delete its checkpoint or the ingestion journal as a routine recovery step.

Inspect safe processing state from `backend/`:

```powershell
python -m app.ingestion_jobs
```

For an exhausted job, stop the API, run `python -m app.ingestion_jobs --retry MESSAGE_REF`, and restart it. This resets attempts for the same job and preserves saved extraction/counts. Completed jobs cannot be reset. Removed/unauthorized rooms are not recovered. A disk failure returns a retryable ingestion error before acknowledgement.

If Switch refuses the connection, verify the bridge URL and agent key, and use a custom agent rather than a controller-managed agent. For Discord setup, confirm the Message Content Intent and room membership. See the official [Switch agent connection](https://docs.switchagents.ai/switch-rooms/internals/agent-connection) and [Discord setup](https://docs.switchagents.ai/switch-rooms/deploy/messaging-apps/discord) guides.

### Troubleshoot an empty Incoming messages screen

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

### Live viewer WebSocket

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

### Resource lifecycle and overload handling

FastAPI startup creates separate reusable asynchronous HTTP pools for Supabase and Switch. Shutdown stops worker/recovery tasks before closing pools. Automatic queued work remains recoverable in the journal. Explicit viewer questions remain ephemeral. Exactly one API worker and one listener are supported.

When configured AI processing has four running/queued jobs, new ingestion returns `429` with `Retry-After: 2` before durable admission. Already accepted duplicate deliveries are acknowledged. Viewer submissions share the same admission lock/capacity. Journal errors return `503`; the listener does not advance its checkpoint. Processing logs contain safe references, state, validated actions, stored counts and failure categories, without chat or model responses.

Database storage is a single backend-only transaction: source evidence and all extracted records succeed together, and only then is extraction marked completed. A source replay returns existing counts without altering approvals. A delivery failure after processing does not repeat storage; replies remain best effort, without a durable delivery outbox. Sender-ID and persisted reply-fingerprint guards prevent own-message loops. Room mappings must already exist and be enabled in Supabase; ingestion never creates/reassigns them.

Offline regressions mock Codex and database transports and use real disposable SQLite journals. See [storage verification](supabase/USEFUL_STORAGE_VERIFICATION.md) for live evidence, remaining gates and the exact integration test.

## 2. Supabase

Cloud connectivity has been checked through FastAPI's protected status route:
unauthenticated access and the ingest token are rejected; the separate admin
token returns `connected`. Connection success does not prove writes work. The
hosted atomic storage function is still missing, and one of two configured rooms
has no database mapping. See [current cloud storage findings](supabase/CLOUD_STORAGE_STATUS.md).

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

The only core migration is [20261104000100_initial_schema.sql](supabase/migrations/20261104000100_initial_schema.sql). It includes atomic storage, durable versioned embedding state, lease/hash guarded completion, invalidation and authorized vector search. The earlier untracked additive migration was confirmed absent from the connected project's migration history and live functions, copied exactly into the baseline, and removed locally. No hosted migration history was edited.

**The consolidated baseline is destructive. Do not run it or `db push` against the hosted project.** See [the non-destructive deployment proposal](supabase/DEPLOYMENT_PROPOSAL.md). `python -m app.schema_upgrade` from `backend/` prints the exact reviewable delta, derived from the baseline; it never connects or applies it. Hosted schema changes still require approval. Until the new functions are installed, writes fail explicitly and remain recoverable.

### Local embeddings and semantic retrieval

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

The disposable PostgreSQL/pgvector schema runner is prepared under `supabase/tests/` (`npm install`, then `npm test`). Its dependency installation was declined in this session, so fresh-schema and SQL behavior execution remain pending. It never loads project credentials or connects remotely. See [verification evidence](supabase/USEFUL_STORAGE_VERIFICATION.md) for exact distinctions between mocked tests, real local inference and unverified hosted/Discord work.

## 3. Mobile (Expo)
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

## 4. Codex / MCP
Codex uses `AGENTS.md` automatically when operating inside this project. Documentation MCPs can help confirm current FastAPI, Supabase and Expo APIs, but connecting a documentation MCP does not grant access to running services. You can start Codex from this folder using `codex`.

Suggested first Codex prompt:
> Read AGENTS.md and architecture.md. Review the current project without introducing any external messaging integrations. Verify the MVC separation, security rules, environment files, migration placement, and tests. Report specific issues before changing any files.

## 5. Before every push
```powershell
git status --short
git check-ignore backend/.env mobile/.env
git diff --cached --name-only
```
The two `.env` files should be reported as ignored when they exist. Inspect staged contents and never force-push. If any credential was previously committed, revoke/rotate it; `.gitignore` alone cannot remove history.

## Current limits

Local model inference is verified. The hosted non-destructive schema upgrade, versioned vector storage, and full Discord -> Switch -> Codex -> Supabase -> semantic answer flow remain pending deployment approval and live verification. Human input is durably journaled before acknowledgement; embedding failures preserve useful text. Oversized memories remain explicit failed embedding jobs. Fresh-schema execution is pending the declined disposable-database test dependency installation. Conflict checks, reminders, document workflows and mobile private-data authentication remain outside this task. No hosted data reset or migration-history rewrite was performed.
