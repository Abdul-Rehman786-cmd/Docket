# Team Memory — Clean Foundation

A project scaffold for **FastAPI + Supabase + React Native (Expo)** with a Switch listener for verifying Discord-to-API message delivery.

The implemented flow receives Discord messages through Switch and displays redacted sender names and text in a local browser viewer. FastAPI and the Switch listener run as separate processes. See [architecture.md](architecture.md) for the current runtime, security boundaries, and planned extraction, RAG, reminders, and reply flows.

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
                                                   FastAPI in-memory previews
                                                             ^
                                                             |
Browser at / -> authenticated GET /messages/recent every 2 seconds
```

**Keep both FastAPI and exactly one listener running.** Starting or reloading FastAPI does not start the listener. The browser's **Live** indicator confirms API polling only; `/health` confirms API liveness only. Neither verifies that Discord messages are being delivered.

Use an existing Switch agent that permits a custom WebSocket client and is assigned to the Discord room. The listener does not register an agent or post replies. Configure these settings in the ignored `backend/.env`; the placeholder-only `.env.example` lists them. Existing credentials should be preserved.

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

To see messages **in the browser**, open `http://127.0.0.1:8000/`, enter your local `SWITCH_INGEST_TOKEN` in the masked access-token field, and select **Open feed**. The page refreshes every two seconds and displays each sender's display name and message text, newest first. Names and text are redacted before retention; the latest 100 messages are kept in memory. Missing or blank names appear as `Unknown sender`; participant IDs are never parsed to invent a username. Keep FastAPI and the listener running, then send a new Discord message. Restart the listener after an update so it forwards Switch's `sender_name` field. Text received before this viewer was added is not available. **Lock** clears the token and displayed messages from the tab. The token is sent only in an authentication header and is not embedded in the page, stored in browser storage, or included in URLs. The page shell is public; `GET /messages/recent` requires authentication and filters previews by the configured tenant and room allow-list. Responses are marked `no-store`.

To also print the incoming sender and message in the **FastAPI terminal**, set `LOG_DISCORD_MESSAGES=true` in the ignored `backend/.env` and restart the API. New deliveries print a line such as `Discord message ref=... sender="Ali" text="Hello from Discord!"`. This local debug option defaults to `false`; configured credential values, common credential formats, and URLs are masked, control characters are escaped, and previews are limited to 2,000 characters. Set it back to `false` to stop printing text. Browser previews work independently of this terminal logging option.

Default delivery logs use receipt metadata to correlate API and listener activity. The current FastAPI entry point does not configure a general `LOG_LEVEL` setting. Keep HTTP and WebSocket wire-level debugging disabled because those traces can expose authentication headers.

Check the latest sender and message in a third terminal, also from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m app.switch_listener --status
```

The protected `GET /messages/status` and the `--status` command return the latest authorized message as `{"sender_name":"Ali","message":"Hello team"}`, replacing the previous counter response. Both values are `null` until a message arrives. Preview reads and latest-message status filter by tenant and allowed rooms. The CLI reads the token internally, so it never has to appear in terminal commands. All `/messages` endpoints require `X-Ingest-Token`. `POST /messages` also checks the tenant and room mapping; an invalid mapping returns `403`. Payload validation returns a generic `422` without reflecting chat text. Text logging runs only after authentication and authorization succeed.

### Ask the existing Switch AI and receive answers only in the viewer

The viewer now has **Ask Codex** beside each received message and a question box above the feed. Select a message or enter a question. The answer appears in the **Ask Codex** panel automatically. This reuses the existing `codex-discord` AI and its model authentication; FastAPI needs no model API key. Questions go through a separate non-AI Switch client to an internal room with no Discord bridge. No automatic questions are sent for ordinary incoming chat.

For a new local installation:

In **Switch Console**, ensure any session binding `codex-discord` to an input room is stopped, leaving only the session for `team-memory-server-responses` running. `team-memory-client` must remain registered and running as the input listener.



1. Preserve `SWITCH_AGENT_ID` and `SWITCH_AGENT_API_KEY` for the existing Codex agent. Set `SWITCH_AI_AGENT_NAME` to its registered name (default `codex-discord`). Keep its Switch Console or managed runtime available.
2. Add `SWITCH_REGISTRATION_TOKEN` to ignored `backend/.env`, using a Switch registration key created under the **same owner** as Codex. Do not paste credentials into commands, chat, or screenshots.
3. From `backend/`, run `.\.venv\Scripts\python.exe -m app.switch_setup`. It registers `team-memory-client` once, saves its returned credentials directly into ignored `.env`, creates the private internal response room, and keeps only the non-AI client in the configured input rooms. It removes `codex-discord` from those input rooms so incoming Discord messages cannot trigger independent AI replies there. Other agents and the Discord bridges are preserved. A harmless private readiness request learns Codex's exact opaque sender ID. No existing agent is overwritten and no addressing permissions are widened. The setup is already completed for the current workspace.
4. Restart FastAPI after setup. Stop the previous listener and start exactly one listener again so it uses the new non-AI identity. Refresh and unlock the viewer at `http://127.0.0.1:8000/`.

Setup writes `SWITCH_REQUESTER_AGENT_ID`, `SWITCH_REQUESTER_API_KEY`, `SWITCH_AI_ROOM_ID`, and `SWITCH_AI_SENDER_ID` in `.env`. `SWITCH_AI_TIMEOUT_SECONDS` defaults to 180 seconds. Once the returned credentials are saved, subsequent setup runs reuse them and the room. A registration conflict stops rather than rotating credentials; restore the saved client credentials if needed. If readiness times out, start Codex's existing runtime and rerun setup. The registration key is used only by the setup command, never by HTTP requests from the viewer.

`POST /ai/requests` accepts `{request_id, question}` or `{request_id, source_message_ref}` and returns `202` with a queued result. Use a UUID for `request_id`; repeating the same ID/question reuses that result, while reusing it for different content returns `409`. `GET /ai/requests` polls the latest redacted results. Both require `X-Ingest-Token`, enforce the configured scope, and return `Cache-Control: no-store`. Client callers cannot choose a destination room or an AI identity. Invalid or expired source-message references return `404`. These are local development interfaces, not mobile user authentication.

The API processes one question at a time, permits up to four pending questions, and retains at most 100 results in memory. Redaction runs before a question reaches Codex and before an answer is retained. Each answer is matched to the configured sender, internal room, and exact request thread. The first matching nonblank response completes the request; the prompt asks Codex to reply once. Timeout, disconnect, or restart does not resubmit a potentially delivered question. Pending work and results are not durable. Run **one API worker**.

Every request verifies that the internal room has no bridge or external channel and contains only Codex and the client. It also checks Codex's reported sessions for live input-room bindings, and checks that every configured input room contains the listener without Codex. Missing runtime evidence, an active input-room session, or re-invitation causes a diagnostic before another question is posted. Stop the Discord-attached Codex session in Switch Console first, then rerun `python -m app.switch_setup`. Setup refuses to report success while an active input-room session is reported. Requests never fall back to an input room. Codex remains available in the private response room. Ask questions using the viewer's question box or **Ask Codex about this message**. Keep the internal room unbridged. The listener uses `SWITCH_REQUESTER_*`, preserving `SWITCH_AGENT_*` for the actual AI. Restarting the listener after an identity change starts a separate cursor; its old identity's messages are not replayed automatically.

Automated tests cover sender/thread correlation, responsive heartbeats, authentication, scope isolation, redaction, request deduplication, and setup recovery.

### Observe other messages and agent replies

`GET /messages/recent` now adds `sender_kind` (`ai` or `message`) and nullable `thread_ref` to each preview. `thread_ref` is a hash of the Switch thread identifier scoped by tenant and room, using the same reference format as messages. When it matches a retained message, the viewer displays that message as thread context. Otherwise it says that earlier context is unavailable. It never guesses which question an unthreaded answer belongs to, and a thread reference identifies conversation context rather than an exact question/answer pair. Raw participant and thread IDs are not returned by the feed. `/messages/status` retains its existing response shape and can return the latest message from either a person or an agent.

Messages in the existing feed have the same authorization, redaction, 2,000-character preview limit, 100-message retention, and restart behavior as before. `SWITCH_AI_SENDER_IDS` can additionally label other exact agent participant IDs; the learned `SWITCH_AI_SENDER_ID` is included automatically. The badge identifies a configured sender rather than proving that every post was generated by a model. These observed room messages are separate from the server-only request results above.

The transport and identity behavior follow the official Switch [event catalog and participant IDs](https://docs.switchagents.ai/switch-rooms/internals/agent-events), [agent operations](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol), and [room routing](https://docs.switchagents.ai/switch-rooms/internals/rooms-and-resources). For an unavailable responder, see [agent troubleshooting](https://docs.switchagents.ai/switch-rooms/using/agent-not-answering).

**Receipt means delivery to the API, not database storage.** The backend discards raw message contents after acknowledgement. It retains receipt metadata for the latest 1,000 message identities and up to 100 redacted previews in memory. Duplicate detection is limited to the receipt window. Counters, duplicate detection, and browser history reset on API restart; run one API worker for this demo. The listener stores only its connection ID, generation, processed cursor, and configuration fingerprint in ignored `backend/.local/switch-listener.json`. It saves progress after the API acknowledges each message, answers heartbeats while HTTP requests are pending, and resumes after transient failures. A takeover or retention gap stops the listener with a diagnostic instead of silently skipping the problem.

Restarting the listener does not restore messages already acknowledged before an API restart. Its saved cursor survives while the API's preview memory is cleared. Unacknowledged events can be replayed within Switch's retention window; do not delete or reset the checkpoint as a routine recovery step.

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

## 2. Supabase
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

Review `supabase/migrations/20261104000100_initial_schema.sql`. From the **repository root**:
```powershell
npx supabase login
npx supabase link --project-ref YOUR_PROJECT_REF
npx supabase db push
```
`supabase db push` changes the linked remote database: get approval and confirm the target project first. Put `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` only in `backend/.env`; never in mobile.

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
Switch messages are acknowledged, evaluated by the LLM 4-way decision, and either stored to Supabase (commitments, decisions, memories) or answered back into Discord and the browser viewer. Conflict checks, APScheduler reminders, and mobile private data authentication remain future work. Development credentials belong only in ignored local environment files.
