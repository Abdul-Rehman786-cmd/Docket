# Team Memory Agent: Architecture and Implementation Plan

This document describes the system architecture, component boundaries, Switch agent integration, and database schema for the Team Memory backend.

## Current implementation: FastAPI and Switch message delivery

The diagram and implementation plan below describe the target system. The current backend is a local FastAPI scaffold that starts without database, LLM, or messaging credentials:

- `backend/app/main.py` creates the app and registers routes; `/health` returns `{"status":"ok"}`. FastAPI provides `/docs` and `/openapi.json`. `controllers/viewer.py` serves the public browser-viewer shell at `/` and its local CSS/JavaScript from `views/`; no private data or credentials are embedded in these files.
- `controllers/records.py` exposes the admin-only `/api/v1/supabase/status`, `/api/v1/messages`, `/api/v1/commitments`, `/api/v1/decisions`, and `/api/v1/memories` endpoints. `models/`, `services/`, and `repositories/` separate validation, business rules, and database access. These endpoints use `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from `backend/.env` and scope record reads to `SWITCH_TENANT_ID` when configured. Database writes and queries are driven by the LLM's 4-way decision, which runs automatically for each new Discord message on `POST /messages` and for explicit viewer requests on `POST /ai/requests`.
- `core/` owns configuration and authentication. Development credentials belong only in the ignored `backend/.env` (or root `.env`); `get_settings()` automatically reloads when `.env` files are saved so credential updates take effect without restarting `uvicorn`. The health endpoint needs no credentials and confirms API liveness only.
- `controllers/messages.py` exposes `POST /messages`, `GET /messages/status`, and `GET /messages/recent`, all protected by a dedicated `X-Ingest-Token`. Latest-message status returns `sender_name` and `message` instead of counters; both are `null` before a message arrives. `models/messages.py` validates input, `services/message_service.py` enforces the configured tenant and room mapping for ingestion, status, and preview reads, and `repositories/message_receipts.py` holds bounded receipt metadata and redacted previews. Switch's `payload.sender_name` is forwarded separately from its opaque participant ID and redacted before retention. Missing or blank names use `Unknown sender`. Validation errors never reflect message contents. Logs contain receipt metadata by default; opt-in local debugging with `LOG_DISCORD_MESSAGES=true` adds redacted sender names and message previews in the API terminal. `core/message_logging.py` masks configured credentials, common credential formats, and URLs; escapes terminal controls; and limits previews to 2,000 characters. Raw text is suppressed if redaction fails.
- The browser viewer asks for the local ingest token in a masked field, keeps it only in tab memory, and opens an authenticated viewer WebSocket for snapshots and incremental updates. It displays sender names with message text rather than a count or receipt reference. Both fields render using `textContent`, never HTML; **Lock** closes the socket, clears the token, cancels pending requests, and removes displayed data. The page uses a restrictive Content Security Policy and message responses use `Cache-Control: no-store`. This is a local development viewer, not production user authentication.
- `python -m app.switch_listener` runs the separate WebSocket listener in `services/switch_listener.py`. It uses the existing agent credentials, declares configured rooms at connection open, receives all message events for those rooms, and forwards their sender IDs, sender display names, and contents to FastAPI. Run only one listener for that agent; declaring rooms takes over those claims from another client of the same agent. `--check` verifies API authentication, the WebSocket, and room membership without claiming rooms; `--status` shows the latest sender name and redacted message. It never registers agents or posts chat messages.
- `SWITCH_TENANT_ID` and `SWITCH_ROOM_IDS` bind the initial demo to one tenant and an explicit room allow-list. Unknown rooms are ignored. This is server-to-server ingestion authorization, not mobile user authentication.
- Heartbeat handling runs independently of API forwarding. The listener checkpoints its acknowledged cursor, connection ID, and generation under ignored `backend/.local/`, and resumes after transient disconnects. Permanent refusals, takeovers, and retention gaps stop with a safe diagnostic. Likewise, `repositories/reply_state.py` persists the highest replied Discord message watermark in ignored `backend/.local/last_replied_message.txt` using atomic writes (`.tmp` to `.txt` rename), preventing the AI from re-answering backlogged questions when the backend restarts.
- Receipt metadata and duplicate detection are process-local, bounded to the latest 1,000 message identities, and reset on API restart. The latest 100 redacted previews are also held in memory and reset on restart. Run one worker. Receipt acknowledgement does not mean durable storage or extraction. The raw message body is redacted before enqueueing; queued jobs retain only the redacted question and necessary routing metadata.
- Backend tests cover health, record access, message authentication/authorization, preview isolation and retention, safe validation/logging, duplicate receipts, resume state, rejected deliveries, and WebSocket heartbeats while the API is slow. A local headless-browser check also verified unlock, automatic message updates, safe rendering of untrusted markup, lock cleanup, and invalid-token rejection with fictional messages.

The diagram's conflict checks, scheduler chases, and `/docs/sync` and `/docs/apply-update` endpoints remain future work. Silent extraction, the LLM 4-way database decision, Supabase persistence/retrieval, and closed-loop responses back to Discord via Switch are implemented. Additional external integrations and remote database migrations require a separate request. See `README.md` for Windows setup and run commands.

### Implemented: server-only answers from the existing Switch Codex AI

The viewer can ask the existing `codex-discord` AI about a selected incoming message or a typed question, and display its answer only on our server. Its existing model authentication remains in Switch; no model key is added to FastAPI. The user explicitly authorized a separate non-AI server client and supplied a registration key locally. `python -m app.switch_setup` registers `team-memory-client` once and creates an internal, private response room. Existing Codex credentials remain intact. The client belongs to the same owner, is invited to the input rooms, and has no model. Permissions are never widened automatically, and registration conflicts never overwrite an identity.

```text
Discord -> Switch input rooms -> non-AI listener -> authenticated message feed
                                                         |
                                       Select Ask Codex, or type a question
                                                         |
                                                         v
POST /ai/requests -> bounded queue -> team-memory-client
                                             |
                                  Internal Switch room (no bridge)
                                             |
                                  Existing codex-discord AI/runtime
                                             |
                                  Correlated reply in request thread
                                             |
GET /ai/requests <- redacted result <- FastAPI
```

`controllers/ai.py` provides authenticated `POST /ai/requests` and `GET /ai/requests`. `models/ai.py` validates a caller-generated request UUID and either a question or a retained source reference, and defines the LLM's 4-way database decision (`db_action`: `store_only`, `query_only`, `none`, `store_and_query`). `services/ai_service.py` authorizes source previews, redacts before sending, serializes requests, correlates room, exact opaque AI sender, and request thread, and executes the LLM's chosen database action via `repositories/record_repository.py`. `repositories/ai_requests.py` retains bounded, redacted results scoped to tenant, input-room allow-list, responder, client, and internal room. Every request verifies that the room has no bridge/external channel and exactly the expected two participants before sending. No caller-supplied destination is accepted. `POST /ai/requests` (viewer) results never post to Discord; only messages that arrived from Discord through the listener can produce a Discord reply, and only to their own origin room (see **Discord to FastAPI to Discord closed-loop pipeline**). HTTP and WebSocket transport lives in `services/switch_api.py`; heartbeat processing runs independently of HTTP calls.

#### LLM 4-Way Database Decision (`POST /messages` & `POST /ai/requests`)

Rather than blindly writing every incoming message to Supabase on `POST /messages`, each incoming user message on `POST /messages` (via `AIService.evaluate_incoming_message`) as well as any explicit request on `POST /ai/requests` is evaluated by the LLM to decide what FastAPI should do with the Supabase database and the response across four options (`db_action`). FastAPI logs each step (`AI evaluating message`, `AI decision`, `AI database store/query complete`, and `AI response complete`) in the API terminal:

| # | Decision (`db_action`) | What FastAPI does with Supabase & the response |
|---|------------------------|------------------------------------------------|
| 1 | `store_only` | **Store in database only:** Ensures `tenants` and `rooms` exist, stores the source message in `messages`, and stores extracted `commitments`, `decisions`, and `memories` (`fact`, `decision`, `commitment`). Does not query existing database records for the response. |
| 2 | `query_only` | **Use database for the response:** Queries `memories`, `commitments`, `decisions`, and `messages` from Supabase (scoped to `SWITCH_TENANT_ID` and optional search keywords) and passes the retrieved database context to the LLM so the response is grounded in stored team memory. Does not store the prompt itself. |
| 3 | `none` | **Do nothing with the database:** Neither stores to nor reads from Supabase. Returns the LLM's direct response. |
| 4 | `store_and_query` | **Both store and access database for the response:** Stores the message and extracted `commitments`, `decisions`, and `memories` into Supabase first, then queries Supabase so the final response sent back incorporates both the newly stored items and historical database context. |

The existing feed and `/messages/status` retain their behavior. `SWITCH_REQUESTER_*` takes precedence for the input listener, so it no longer claims rooms using Codex's identity. The response connection claims only the client's internal room. Setup stores returned credentials only in ignored `backend/.env` and learns the AI's sender ID from a harmless correlated readiness reply, without parsing participant IDs. The viewer renders all questions and answers as text, receives socket updates, and clears them and pending browser requests on lock. Its token stays in tab memory. These are local development interfaces, not production user authentication.

#### Agent Isolation Invariant
To ensure AI answers are routed exclusively through FastAPI and prevent duplicate or ungrounded responses in Discord:
1. `codex-discord` must remain strictly inside the internal private room (`team-memory-server-responses`). Setup removes Codex from configured input rooms, and FastAPI checks report active input-room sessions or re-invitations as fatal errors before sending.
2. In Switch Console, any session binding `codex-discord` directly to a Discord channel must remain stopped. Only the session for `team-memory-server-responses` should run.
3. Only `team-memory-client` (the non-AI client) is permitted in Discord input rooms to ingest messages and post FastAPI-approved replies.





One process permits four outstanding jobs in total (running plus queued), runs one worker, and retains at most 100 results plus 100 aliases. Source reuse preserves the canonical request ID, including while a job is running. Aliases are bounded independently and removed with evicted results. Full capacity returns `429` for a new eligible incoming message before recording its receipt; the listener retries without advancing its acknowledged cursor. Duplicates and configured agent/echo messages do not consume another slot. Same request UUID and question is idempotent within retained history, and explicit viewer requests for an already-evaluated `source_message_ref` reuse that evaluation. A timeout defaults to 180 seconds; a disconnect or restart never blindly resends a potentially delivered request. Only the first matching nonblank response is retained; the prompt asks Codex to reply once. Each question/answer uses the existing redaction and 2,000-character preview cap. Results and pending jobs are process-local, while records chosen by `store_only` or `store_and_query` are persisted in Supabase (`tenants`, `rooms`, `messages`, `commitments`, `decisions`, `memories`). Run one API worker. Vector embedding generation (`Supabase/gte-small`) on `memories.embedding` remains future work.

Tests cover authentication, scope isolation, redaction before prompting, room routing checks, exact sender/thread correlation, heartbeats during HTTP waits, idempotency, and safe registration recovery. Switch's [agent protocol](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol), [connections](https://docs.switchagents.ai/switch-rooms/internals/agent-connection), and [room routing](https://docs.switchagents.ai/switch-rooms/internals/rooms-and-resources) were checked before implementation. See README for restart and setup commands.

### Discord to FastAPI to Discord closed-loop pipeline

```text
1. Discord channel
       | user sends message
       v
2. Switch Discord Bridge
       | relays event into Switch room
       v
3. Python listener process: python -m app.switch_listener
       | outbound WebSocket receives event
       | POST /messages with X-Ingest-Token
       v
4. FastAPI backend (app/services/ai_service.py):
       | 1. Deduplicate & Ingest
       | 2. Loop Guard: ignore posts from bot/agents (switch_ai_senders)
       | 3. Direct Message Prompt: pass clean current message body to Codex
       | 4. LLM 4-Way Database Decision: store_only, query_only, none, store_and_query
       | 5. If store: write commitments/decisions/memories to Supabase
       | 6. If query/addressed: query Supabase records & synthesize context with Codex
       | 7. Outbound Dispatch: SwitchAPI.post_room_message() to origin room_id
       v
5. Switch input room (origin room_id)
       | team-memory-client posts message via /ops/post_message (or /message fallback)
       v
6. Switch Discord Bridge
       | relays agent response into Discord channel or active thread
       v
7. Discord channel
       | teammates see the grounded team memory answer directly in chat
```

#### Dual-Agent Separation in Switch
To deliver answers to Discord while completely avoiding double replies and uncontrolled hallucinations, the architecture separates the chat adapter from the model runtime:

1. **Front-Facing Room Agent (`team-memory-client`)**:
   - Registered as a custom agent without an autonomous LLM model attached in Switch Console.
   - Assigned to the Discord-bridged input rooms (`SWITCH_ROOM_IDS`).
   - Sits on the WebSocket listener to ingest chat events, and serves as the outbound sender for FastAPI replies.
   - Because it has no attached model in Switch Console, it never blurts out unprompted or ungrounded chatter into Discord.
2. **Private Reasoning Engine (`codex-discord`)**:
   - Isolated inside the private, internal, unbridged room (`team-memory-server-responses`).
   - Removed from Discord input rooms so it never processes Discord events directly.
   - Queried privately by FastAPI over the Switch API to evaluate current messages, decide database actions, and synthesize team memory responses.

#### Direct Message Evaluation and Safe Redaction
Incoming user messages supply their current body for redaction during admission. The redacted text becomes the queued question; `source_meta["body"]` is removed before enqueueing, and author names are redacted as well. `build_decision_prompt()` receives the redacted current question. The message text is JSON-quoted so chat text remains untrusted data rather than instructions. Defensive redaction in `message_logging.py` safeguards against `None` values or invalid types, logging clean redacted previews while never suppressing legitimate user input.

#### Outbound Dispatch and Dual Loop Guard
- **Origin Isolation**: Only messages that originated from Discord through the listener (`origin == "discord"`) can trigger outbound Discord replies. Explicit viewer questions on `POST /ai/requests` (both typed questions and **Ask Codex about this message**) always stay inside the viewer and never post to Discord.
- **Outbound Dispatch**: Responses are posted using `SwitchAPI.post_room_message()`, calling `/agents/{agent_id}/ops/post_message` with `{room_id, body, thread_id}` (falling back to `/message` if ops are unmapped). If an outbound post fails (e.g. transient network or permission error), the failure is logged as a warning and does not fail the saved evaluation.
- **Thread Context**: Replies to threaded Discord messages carry `thread_id` so they appear nested in the proper Discord conversation thread.
- **Dual Loop Guard**:
  1. *Sender-based*: `switch_ai_senders` automatically includes `SWITCH_REQUESTER_AGENT_ID`, `SWITCH_AGENT_ID`, and configured `SWITCH_AI_SENDER_IDS`.
  2. *Content-fingerprint based*: Switch sender IDs are opaque participant IDs that can differ from agent IDs. FastAPI maintains a bounded rolling cache (`posted_replies`) of SHA-256 fingerprints of recently posted replies keyed by `(room_id, body)`. When an echo of our own reply arrives via the WebSocket listener, FastAPI identifies the fingerprint and skips evaluation, completely preventing infinite response loops.
- **Reply Policy Control**: Governed by `SWITCH_REPLY_TO_DISCORD=true` (default). The bot strictly replies only when tagged/addressed in Discord (or answering a two-turn confirmation prompt). Ambient channel chatter is ingested silently. When tagged, the bot sends an immediate `"Processing your request..."` acknowledgment to give the user closure, followed by the final synthesized answer or staged confirmation preview.

### Current runtime and message flow

```text
Discord channel
    |
    v
Switch bridge / configured rooms
    | outbound WebSocket opened by the listener
    v
Python listener process: python -m app.switch_listener
    | POST /messages with X-Ingest-Token
    v
FastAPI process: python -m uvicorn app.main:app --reload
    | authenticate -> validate tenant/room -> redact -> retain in memory
    v
MessageReceiptRepository (one API worker)
    ^
    | /viewer/ws initial snapshot and incremental events
Browser: Incoming messages screen at /
```

FastAPI and the listener are separate processes. Starting or reloading FastAPI does not start the listener. Both must remain running for new Discord messages to appear. The browser's **Live** indicator confirms the authenticated viewer socket; it does not confirm that the listener is connected to Switch. Likewise, `/health` can return `200` while Discord delivery is stopped.

The listener reads settings from the ignored `backend/.env`, opens the outbound Switch connection, and forwards authorized room events to FastAPI. The browser contacts FastAPI only. The listener's persisted cursor survives restarts, while the API's receipt and preview memory does not. Restarting the API therefore clears displayed history; resuming the listener does not restore messages it already acknowledged. Unacknowledged events can be replayed while they remain in Switch's retention window.

### Authenticated viewer socket

`controllers/viewer.py` exposes `/viewer/ws` and delegates its lifecycle to `services/viewer_service.py`. `models/viewer.py` validates the first authentication frame without reflecting input. The service requires a same-origin browser handshake, rejects URL parameters, and authenticates the existing ingest token within five seconds before returning private data. Each socket gets a UUID; multiple tabs with the same token have distinct connections but the same authorized feed. This remains local shared-token authentication, not multi-user or multi-server authorization.

FastAPI lifespan owns a bounded `ViewerHub`. Receipt and AI repositories expose optional change callbacks; the hub filters redacted events by tenant/room or the existing AI response scope before enqueueing them for each viewer. Removal events keep clients synchronized with retention eviction. A newly authenticated connection receives a scoped snapshot before incremental messages and AI-state changes. Normal browser operation performs no history polling; HTTP read endpoints remain compatible and question submission stays on HTTP.

Connections have bounded outgoing queues (32 events), a global connection cap (64 including handshakes), send deadlines, and independent scheduled application pings. Writer tasks revalidate current credential digests and authorized scope before every event and heartbeat; credential rotation or a scope change closes existing sockets. Disconnect cleanup removes the subscription synchronously and cancels both socket tasks. Shutdown stops broadcasts. The browser reconnects with capped backoff and a fresh scoped snapshot, ignores stale callbacks from older sessions, and clears data and its token on lock/page exit. Server-side and browser timers detect stalled connections. Browser CSP explicitly permits only the current host's ws/wss endpoint alongside same-origin HTTP access.

Socket notifications and connection IDs are process-local. They introduce no database calls, migrations, external messaging integration, persistent event log, or change to Switch AI concurrency. One API worker and the separate Switch listener remain required.

### Managed asynchronous HTTP resources

FastAPI lifespan owns two separate `httpx.AsyncClient` pools, one for database access and one for Switch HTTP operations. `core/http_clients.py` binds the pools to the request context; the single AI worker inherits that application context. Repository decorators pass one database client through each complete operation, including nested tenant/room checks. Standalone calls use explicitly closed temporary clients. All database HTTP calls are awaited; controllers and record services are asynchronous. Existing query selection and write ordering are preserved.

Each pool has eight maximum connections and four keep-alive connections. Credentials are supplied per operation using its settings snapshot, so newly admitted work uses reloaded credentials without mutating shared client headers. Switch HTTP calls use absolute URLs and explicit authentication. WebSockets retain per-request room claims and exact reply correlation; no persistent shared room connection or parallel AI execution was introduced. Shutdown cancels/drains AI work before closing pools. Resource reuse is not durable queue storage and does not change the two-process, single-worker deployment.

### Delivery diagnostics and operational troubleshooting

When the message feed or preview endpoint is empty, verify listener connectivity and room membership:

For an empty Incoming messages screen:

1. Confirm FastAPI is running, then run `python -m app.switch_listener --check` from `backend/` using its virtual-environment interpreter. This command checks connectivity and membership, then exits; it does not keep listening.
2. Confirm exactly one listener for the agent is running. If none is running, start `python -m app.switch_listener`. Do not start a competing listener, because it can take over room claims.
3. Check listener output for room coverage and `API acknowledged message ref=...`, and API output for the matching `Switch message received ref=...`. Receipt references allow delivery correlation without printing message text or credentials.
4. Unlock or refresh the browser feed and send a new harmless Discord marker. An authenticated empty preview response means no authorized previews are currently retained; `401` indicates an authentication failure, and `503` can indicate missing ingestion configuration.

The listener checkpoint is persisted at `backend/.local/switch-listener.json`; do not delete or reset it as a routine step, as it maintains the connection ID and acknowledged cursor.

Default delivery logs contain receipt metadata. `LOG_DISCORD_MESSAGES=true` separately enables redacted sender and message previews in the API terminal after authentication and authorization. HTTP and WebSocket wire-level debugging can expose authentication headers and must remain disabled. The current FastAPI entry point does not configure a general `LOG_LEVEL` setting.

## 1. What we are building

A team memory agent that lives in chat (Discord first) and:

- Silently reads team conversation and extracts decisions, commitments, facts and person profiles.
- Chases people about commitments they made in chat.
- Answers questions from that memory and from project docs, with citations.
- Notices when a chat decision contradicts a doc and proposes a doc update for approval.
- Replies only when it should. It stays silent otherwise.

Switch provides the chat bridge and agent protocol identity, while the Python FastAPI backend manages ingestion, extraction, database access, and LLM reasoning.

## 2. Decisions made

| # | Decision | Reason |
|---|----------|--------|
| 1 | Switch is the chat layer (Discord via Switch's built-in bridge). No custom Discord bot. | Standardized agent protocol and reduced bot maintenance. |
| 2 | Backend is Python: FastAPI, Dual-Storage (Supabase Postgres + pgvector or local embedded SQLite with NumPy vector search), APScheduler, one LLM gateway. | Flexibility for cloud sync or offline local-first storage. Strictly follows ATOM principles with atomic multi-table transactions. SQLite storage path must be explicitly chosen by the user from UI (no default directory). |
| 3 | Reuse one existing Codex AI plus one shared non-AI server client; no agent per server. Register the client once and preserve its credentials. | The user authorized the client so server requests have a different sender from the existing AI and do not take over its runtime. |
| 4 | A small custom agent client ("router client") with no LLM sits on the Switch WebSocket. | Lets our code decide when to reply, instead of an LLM answering everything. |
| 5 | The backend owns the reply decision (`router.py`). | Switch has no "reply only if needed" setting. |
| 6 | Every reply goes out through the `post_message` API. | Single path for speaking, easy to control. |
| 7 | Scheduling (chases) runs in our backend with APScheduler. | Switch has no scheduler of its own. |
| 8 | RAG runs asynchronously: acknowledge fast, search in the backend, post the answer when ready. | Embedding and search take seconds. |
| 9 | Data is keyed by `tenant_id` and `room_id`. Never by room name. | Names change and can collide. |
| 10 | Follow-ups are turned off and no main agent is assigned. | Otherwise Switch routes unaddressed messages to the agent. |
| 11 | Scope: Discord-first end-to-end flow. | Validates complete closed loop before multi-platform adapters. |


## 3. Architecture

This section describes the target architecture. The implemented delivery and viewer path is shown in **Current runtime and message flow** above; extraction, replies, and scheduling below remain planned.

```
Discord server
   | (messages, replies)
   v
Switch messaging bridge (built in)      channel = room
   |
   v
Switch rooms  --> reply control (follow-ups off, no main agent)
   |
   v
Router client (custom agent protocol, NO LLM)
   | WebSocket, reads every message event
   | POST /messages (all)      GET /ask (addressed)
   v
FastAPI backend
   main.py        entry points
   prefs.py       preferences (reply_mode, tone, cadence, quiet hours)
   router.py      reply policy (ingest silently; reply only when needed)
   extract.py     decisions, commitments, facts, person profiles
   answer.py      embeddings + vector search + cited answers
   conflicts.py   doc vs decision check, propose_update
   reminders.py   APScheduler chases (+ notify)
   llm.py         single LLM gateway (Ollama or hosted)
   db.py          Dual-Storage: Supabase Postgres (pgvector) OR local SQLite (NumPy cosine vectors)
   |
   +--> POST /agents/{agent_id}/message --> Switch --> Discord
```



### Components

- **Switch bridge.** Connects Discord to Switch. A channel becomes a room, and Switch creates the room when the first message arrives.
- **Switch rooms.** One room per Discord channel. Our own "project folder" idea maps onto rooms through the backend.
- **Router client.** A small Python process. It registers once with `POST /agents`, opens `/agents/{agent_id}/connection/ws`, answers pings, and forwards events to the backend.
- **Backend.** Does extraction, retrieval, conflict checks, scheduling and the reply decision.
- **LLM.** Used only inside the pipeline (extraction, answer writing, conflict checks). It never talks to users directly.
- **Preferences UI.** A small settings page. Reply mode, tone, chase cadence, quiet hours, project folders, LLM provider.

## 4. Switch: what the docs say

Base URL: https://docs.switchagents.ai

- **Custom agent client is supported.** Register with `POST /agents`, open the WebSocket, claim rooms, answer pings, act only on events where `payload.addressed` is true. ([agent-protocol](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol))
- **A connection receives every message in its rooms by default.** Delivery and acting are different things. Use `filter=all` to see everything. A `filter=addressed` connection never receives command events. ([agent-events](https://docs.switchagents.ai/switch-rooms/internals/agent-events), [agent-connection](https://docs.switchagents.ai/switch-rooms/internals/agent-connection))
- **Posting needs no WebSocket.** `POST /agents/{agent_id}/message` with `{room_id, content}` and the agent API key as a bearer token. The backend can post whenever it is ready.
- **Liveness.** The server pings every 2 seconds. Reply with a pong carrying your cursor. A connection that misses 6 seconds lapses. Resume with the same `connection_id` and `expected_generation`.
- **Limits.** An agent can hold 32 connections. A new agent answers only its owner until addressing is changed (`owner_only`, `owner_and_owner_agents`, `anyone`) or custom rules are set.
- **Do not re-register.** Re-registering an existing name gives a 409 or issues a new API key and breaks the old one.
- **Follow-ups.** When on, a message sent within 5 minutes of the agent's last answer reaches it without an @. A main agent receives every message from people. Turn off with `!follow-ups off` (no @ needed) and `!unassign-agent`. ([work-in-a-room](https://docs.switchagents.ai/switch-rooms/using/work-in-a-room))
- **No scheduler.** Recurring work must come from outside Switch. Ours comes from APScheduler.
- **Discord setup** needs the Message Content Intent and Server Members Intent, plus bot permissions including View Channels, Send Messages, Manage Webhooks, Manage Channels, Manage Roles, Read Message History, Attach Files, Add Reactions. The Discord connect form asks for a bot token and a Guild Id. ([discord](https://docs.switchagents.ai/switch-rooms/deploy/messaging-apps/discord))
- **Rooms.** A room is a chat channel. On Discord, Switch creates the room when the first message arrives in a channel the bot can see. The docs show no way to set a room name from code.

## 5. Reply policy (`ai_service.py` & `message_service.py`)

Every message is ingested into the authenticated preview feed (`POST /messages`). To prevent ambient chat clutter, the bot strictly replies only when tagged/addressed in Discord (or answering a two-turn confirmation prompt). When tagged, an immediate acknowledgment (`"Processing your request..."`) is dispatched before running LLM evaluation or database search. Each incoming message is evaluated by the LLM (`AIService.evaluate_incoming_message`), deciding one of five actions (`store_only`, `query_only`, `none`, `store_and_query`, or `clarify`). FastAPI executes database actions against Supabase and posts back to Discord according to the table below:

| Event / LLM Decision | FastAPI Database Action | Discord Reply Behavior |
|-----------------------|-------------------------|------------------------|
| `store_only` (new commitment, decision, or team fact) | Stages draft in SQLite (`PendingConfirmationRepository`). On user "yes", commits to Supabase. On "no", discards draft. | **Replies in Discord (when tagged).** Sends extraction preview asking confirmation ("Would you like to save this into the database?"). On "yes", commits and replies "Data has been saved into the database." On "no", replies "Cancelled. Data was not saved." |
| `clarify` (unclear intent to store/record) | Neither stores nor reads from Supabase. | **Replies in Discord (when tagged).** Posts clarification question asking user to specify missing details. |
| `query_only` (question requiring team memory) | Queries Supabase (`memories`, `commitments`, `decisions`, `messages`) via local ONNX vector embeddings (`gte-small`, 384d). | **Replies in Discord (when tagged).** Codex synthesizes a plain-text answer from Supabase records; FastAPI posts to origin room & thread via `SwitchAPI.post_room_message()`. |
| `store_and_query` (new information + question) | Stages draft for confirmation if new items extracted, and queries Supabase records. | **Replies in Discord (when tagged).** Codex synthesizes an answer incorporating newly stored and historical records; FastAPI posts to origin room & thread. |
| `none` (casual chatter, greetings, or off-topic) | Neither stores nor reads from Supabase. | **Silent in Discord.** Even when tagged, no answer is sent unless the model generated an explicit textual response. |
| Ambient message (not tagged/addressed) | Silently ingested into message feed. | **Completely silent in Discord.** Prevents agent from answering unaddressed chatter in the channel. |
| Message from the agent itself (`switch_ai_senders`) | Ignored by both listener and API. | **Loop Guard.** Ignored immediately to prevent infinite reply loops. |

Outbound Discord posting is controlled by `SWITCH_REPLY_TO_DISCORD=true` in `backend/.env`.

### Immediate Closure Acknowledgment
When an incoming message is addressed to the bot, `AIService._run` immediately posts:
```text
Processing your request...
```
to the originating room and thread before executing the model evaluation or database queries.
- **Watermark isolation**: The intermediate closure message passes `timestamp_ms=None`, preventing premature watermark progression so the subsequent final response is not skipped.
- **Echo prevention**: The progress notification fingerprint is recorded in `AIService.posted_replies` and `IngestionJobs` before transmission, ensuring incoming Switch echoes are dropped cleanly.

### Two-Turn Staged Confirmation (`PendingConfirmationRepository`)
To ensure accuracy and prevent unwanted database writes from misinterpretations:
- When the model extracts records under `store_only` or `store_and_query`, the extraction is staged in a local SQLite table (`.local/pending_confirmations.db`) keyed by `{room_id}:{thread_id}`.
- The bot replies in Discord with a preview of extracted commitments/decisions and asks:
  `Would you like to save this into the database? (Reply 'yes' to confirm or 'no' to cancel)`
- When a teammate replies `yes` (or affirmative variations), the staged draft is committed to Supabase and the bot confirms: `"Data has been saved into the database."`
- When a teammate replies `no` (or negative variations), the staged draft is purged and the bot confirms: `"Cancelled. Data was not saved."`

### Reply Watermark (`backend/.local/last_replied_message.txt`)
To prevent replaying or re-answering historical questions when the backend is restarted after being offline, `AIService` tracks the highest message timestamp (`timestamp_ms`) that received an AI reply.
- The watermark is persisted atomically in ignored `backend/.local/last_replied_message.txt`.
- When an incoming Discord message has `timestamp_ms <= last_replied_watermark`, it is identified as an older or replayed message.
- For such older messages, expensive database query synthesis and Discord replies are skipped (`logger.info("AI skipped reply to older message...")`), preventing duplicate responses to past queries upon listener reconnection. Memory storage (`store_only` / `store_and_query`) continues to safely upsert into Supabase with conflict resolution.

## 6. RAG and response flow (closed-loop)

1. **User tags bot in Discord**: Teammate mentions `@team-memory-agent` with a question or fact in a Discord channel (or active thread).
2. **Switch relays event**: The Switch Discord bridge delivers the message event into the Switch room with `addressed=true` or mention syntax (`<@...>` / `@team-memory-agent`).
3. **Listener forwards to API**: `switch_listener.py` receives the WebSocket event and posts to `POST /messages` with `X-Ingest-Token`.
4. **Loop Guard check**: FastAPI checks `message.sender in settings.switch_ai_senders`. If from the bot or an AI agent, it is dropped immediately.
5. **Watermark & Mention check**: FastAPI verifies the message is addressed and not older than the watermark in `backend/.local/last_replied_message.txt`.
6. **Immediate closure notice**: FastAPI immediately dispatches `"Processing your request..."` to the originating Discord channel/thread so the user knows their request was acknowledged.
7. **Direct message prompt**: FastAPI passes the clean, unescaped message body directly to Codex in the private internal room (`team-memory-server-responses`).
8. **Action execution**:
   - If `query_only`: FastAPI generates query embeddings using local ONNX `Supabase/gte-small` (384d), runs vector search via `search_memories` RPC on Supabase, and feeds retrieved context to Codex to synthesize a plain-text answer.
   - If `store_only`: FastAPI stages the extracted draft in `PendingConfirmationRepository` and asks the user to confirm with `yes` or `no`.
   - If `clarify`: FastAPI posts a clarification question back to Discord.
9. **Outbound posting to Switch & Watermark update**: FastAPI calls `SwitchAPI.post_room_message(room_id, body, thread_id)` using `team-memory-client` via `POST /agents/{agent_id}/ops/post_message`. Once successfully posted, `AIService` updates `backend/.local/last_replied_message.txt` with the message timestamp watermark.
10. **Relay to Discord**: The Switch Discord bridge delivers the final response into the originating Discord channel or active thread.

## 7. Data model

### Current foundation schema

`supabase/migrations/20261104000100_initial_schema.sql` is the consolidated,
destructive development reset. It creates `tenants`, `rooms`, `room_members`,
`messages`, `commitments`, `decisions`, `reminder_deliveries`, and `memories`.
Tenant and room primary keys are named `id`; room-scoped records carry
`tenant_id` and `room_id`, with composite foreign keys preventing cross-room
source references. Tenants and rooms are the scope roots, not room-scoped rows.

Messages carry Switch identifiers and extraction retry/lease state. Memories
contain searchable facts, decisions, or commitment summaries linked to source
messages. They use locally generated 384-dimensional `Supabase/gte-small`
embeddings. `search_memories` ranks only active, embedded memories within the
specified tenant and room, returns source message IDs, and is executable only by
the backend service role. It returns no rows for a missing query vector. It uses
exact cosine search initially. Editing memory content clears its old embedding;
the backend must regenerate it before the memory becomes searchable. The backend must
authorize room access and keep searchable summaries synchronized with structured
commitment/decision state. RLS is enabled and client table privileges are revoked;
service-role access still bypasses RLS.

The following list records the broader planned data model. Tables not listed in
the current foundation above remain future scope.

Room-scoped tables carry `tenant_id` and `room_id`. Every query and every vector search must enforce the caller's authorized scope. Postgres row-level security can provide additional protection for non-privileged access; privileged backend credentials require explicit authorization in FastAPI.

- `tenants`: `tenant_id`, Discord server id (guild id), settings.
- `rooms`: Switch `room_id`, `tenant_id`, project folder or channel it covers. Unknown room ids are ignored until mapped.
- `memories`: extracted decisions, facts and summaries, with embedding vectors.
- `commitments`: owner, text, due date, status, source `message_id`, `last_chased_at`.
- `documents` and `doc_chunks`: project docs with embeddings.
- `pending_updates`: proposed doc changes awaiting approval.
- `person_profiles`: per-person facts built over time.
- `preferences`: `reply_mode`, tone, cadence, quiet hours, project folders, LLM provider.
- `processed_events`: `message_id` for deduplication.

Open question: Switch events may not include the Discord server id. If not, record it ourselves when the server is onboarded and map the rooms to that tenant. Check the room response fields in the Switch API reference.

## 8. Dual Database Engine: Supabase (PostgreSQL) & Local Embedded SQLite

The system supports seamless switching between remote Supabase (PostgreSQL with `pgvector`) and local file-based SQLite:

### Configuration & Dispatch
- Controlled by `DATABASE_ENGINE` (`"supabase"` or `"sqlite"`) in `backend/.env`.
- `effective_database_engine` property in `core/config.py` evaluates the active engine.
- Repository layer routes dynamically:
  - `repositories/record_repository.py` delegates to `repositories/sqlite_records.py` when engine is SQLite, or Supabase REST when Supabase.
  - `repositories/embedding_repository.py` delegates to `repositories/sqlite_embeddings.py` when engine is SQLite, or Supabase RPCs when Supabase.

### Strict User Directory Policy
- **No silent defaults**: `SQLITE_DB_PATH` is required for SQLite and defaults to empty.
- If SQLite is requested without `SQLITE_DB_PATH`, the backend logs an error and raises `HTTPException(503, "Database is not configured")`.
- Path must be explicitly chosen from the UI Settings panel or configuration modal.

### ATOM / ACID Transaction Principles
- Every multi-table extraction write in SQLite (`messages`, `commitments`, `decisions`, `memories`) executes within an atomic transaction context (`with conn:`).
- Any failure during insertion rolls back all tables completely, preventing partial or corrupt states.
- Foreign keys (`PRAGMA foreign_keys = ON;`) and WAL mode (`PRAGMA journal_mode = WAL;`) are strictly enforced.

### Vector Embeddings & Semantic Search in SQLite
- 384-dimensional embeddings generated by local ONNX `Supabase/gte-small` are stored in SQLite `memories.embedding` (and `document_chunks.embedding`) as raw IEEE-754 float32 byte arrays (`float32.tobytes()`).
- Semantic vector similarity is calculated locally using NumPy normalized dot-product (`cand_vec · query_vec / (|cand| * |query|)`).
- Sub-millisecond search execution with zero external compiled C-extension dependencies (no binary compatibility issues).

### Supabase-to-SQLite Migration Pipeline
- `services/migration_service.py` pulls records in dependency order from Supabase REST:
  1. `tenants` → `rooms`
  2. `messages`
  3. `commitments` & `decisions`
  4. `memories` (transforms JSON vector arrays to float32 BLOBs)
  5. `documents` → `document_versions` → `document_chunks` → `document_imports`
- Inserts records into SQLite using atomic transaction batch upserts (`INSERT OR REPLACE`).
- Exposed via authenticated endpoint `POST /viewer/migrate-db`.

### In-App Engine Switching Modal
- In `views/messages.html` / `views/messages.js`, selecting SQLite radio intercepts the engine change.
- A modal dialog appears immediately, presenting:
  1. Directory selection: input field with `📁 Browse` and `⚡ Use local folder` quick-fill buttons.
  2. Decision prompt:
     - **Migrate data & Switch**: saves path, runs live migration via `/viewer/migrate-db`, updates `DATABASE_ENGINE=sqlite`, reloads.
     - **Switch without migrating**: saves path and switches engine immediately (starts clean SQLite database).
     - **Cancel**: closes modal and reverts radio button back to Supabase.

### Storage Retention & SQLite Space Quota Management
- `services/storage_service.py` and `controllers/viewer.py` expose:
  - `GET /viewer/storage/status`: returns active engine, timeline retention settings, SQLite maximum space quota, and live SQLite storage usage metrics (`used_bytes`, `file_bytes`, `payload_bytes`, and row counts).
  - `POST /viewer/storage/prune-timeline`: deletes all records older than the user-selected timeline (`hours`, `days`, `weeks`, `months`, `years`, or explicit ISO cutoff) in either **Supabase** (`delete_records_older_than_supabase`) or **SQLite** (`delete_records_older_than_sqlite`) following ATOM principles (child records deleted before parent messages in a single transaction, followed by `VACUUM` in SQLite).
  - `POST /viewer/storage/enforce-space`: when using **SQLite**, allows the user to enable and set a maximum storage space (`KB`, `MB`, or `GB`).
    - **Targeted-Table Eviction**: When new records are added (in `persist_extraction` or `finish_embedding`), `enforce_sqlite_max_space` receives `target_tables` (e.g. `['messages']` or `['memories']`) and prioritizes deleting the oldest records specifically from the table that received new data before touching other tables, preserving newly added message IDs (`preserve_message_ids`).
    - **Effective Size & Schema Overhead Protection**: SQLite requires ~172 KB base disk allocation for 10 tables and 30 indexes even when empty. When maximum space is configured below 256 KB, the enforcer and UI evaluate user payload data (`payload_bytes`) rather than empty schema page overhead to prevent accidental data deletion.
    - **Dual UI Metric Display**: Storage usage bar dynamically displays `Payload: X KB / Y KB (%) • Disk: Z KB` when small quotas are active, or total disk usage for standard megabyte/gigabyte quotas.

## 9. Multiple servers (later)

- One agent, one WebSocket, many rooms.
- Map `room_id` to `tenant_id` in the backend. Never use `room_name` as the identifier.
- A server has many rooms (one per channel). Keep the rooms-to-server mapping in `rooms`.
- If Switch does not give the server id in events, store the guild id at onboarding.
- Connecting a server to Switch is a manual step in Switch Console (token and Guild Id, admin only). No API found to automate it. Ask the Switch team.
- Single Discord server scope for initial deployment.

## 10. End-to-end flow

1. Someone writes "I'll send the report by Friday" in Discord.
2. The router client sees it. The backend silently stores the commitment.
3. Friday arrives. The agent chases them in the room.
4. A teammate asks `@memory-agent what did we decide about X?`. The agent says "Looking that up..." and posts a cited answer.
5. A decision contradicts a doc. The agent proposes an update for approval.

Make Switch visible: the room, the router client connected through the agent protocol, and replies posted back through `post_message`.

### In scope

- Discord through Switch
- Silent ingestion, commitments, chases, RAG answers, one doc write-back flow
- Small settings page (optional)

### Out of scope for the demo

- Slack and Teams
- Multi-server onboarding
- Drive mirroring
- A large preferences UI

## 11. Risks and open questions



**Unverified items (test before relying on them):**

- A message posted through `post_message` appears in the Discord channel as expected (test live).
- Switch operation `ops/post_message` vs direct `/message` endpoint behavior across different Switch bridge versions (code implements `ops/post_message` with `/message` fallback).
- The agent must be a room member to post (possible 403 otherwise).
- Messages from bots or webhooks may not count as coming from a person.
- `payload.addressed` behavior when follow-ups are on.
- Whether a thread parameter exists on `post_message`.
- Whether Switch events include the Discord server id.
- Whether one Switch connection can serve many Discord servers.
- A bot-posted `!follow-ups off` being accepted when provisioning a room.

**Loop guard.** Dual loop guard implemented: sender ID checking (`switch_ai_senders` including both agent credentials) plus rolling content-fingerprint matching (`posted_replies`) to catch echo events regardless of opaque participant IDs.

**Visible Switch layer.** Users may still see Switch-voiced messages (for example "agent not available"), Switch's slash commands in the Discord menu, and the bot's permission requests. Keep the router client connected.














## 12. Build order

1. Register the agent and open the router client WebSocket. Confirm events arrive and pings are answered.
2. Backend `POST /messages` with silent extraction into Postgres. Add deduplication.
3. `post_message` from the backend. Confirm replies show up in Discord.
4. Addressed question flow: acknowledgement, pgvector search, cited answer.
5. Commitments table and the APScheduler chase loop (cadence and quiet hours).
6. Doc sync, `conflicts.py`, and the approval flow for proposed updates.
7. Reply policy and `reply_mode`. Turn follow-ups off.
8. Small settings page. Polish the demo script.

## 13. Reference docs

- Agent protocol: https://docs.switchagents.ai/switch-rooms/internals/agent-protocol
- Agent connection: https://docs.switchagents.ai/switch-rooms/internals/agent-connection
- Agent events: https://docs.switchagents.ai/switch-rooms/internals/agent-events
- Work in a room (follow-ups, scheduling): https://docs.switchagents.ai/switch-rooms/using/work-in-a-room
- Create a room: https://docs.switchagents.ai/switch-rooms/getting-started/create-a-room
- Discord setup: https://docs.switchagents.ai/switch-rooms/deploy/messaging-apps/discord
- Self-hosting Switch: https://docs.switchagents.ai/switch-rooms/deploy/self-host
