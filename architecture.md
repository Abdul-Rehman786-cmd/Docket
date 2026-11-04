# Team Memory Agent: Architecture and Implementation Plan

Prepared November 4, 2026 for the Flip the Switch hackathon. This file records what we decided, why, and what is still unverified.

## Current implementation: FastAPI and Switch message delivery

The diagram and implementation plan below describe the target system. The current backend is a local FastAPI scaffold that starts without database, LLM, or messaging credentials:

- `backend/app/main.py` creates the app and registers routes; `/health` returns `{"status":"ok"}`. FastAPI provides `/docs` and `/openapi.json`. `controllers/viewer.py` serves the public browser-viewer shell at `/` and its local CSS/JavaScript from `views/`; no private data or credentials are embedded in these files.
- `controllers/records.py` exposes the admin-only `/api/v1/supabase/status`, `/api/v1/messages`, `/api/v1/commitments`, `/api/v1/decisions`, and `/api/v1/memories` endpoints. `models/`, `services/`, and `repositories/` separate validation, business rules, and database access. These endpoints use `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from `backend/.env` and scope record reads to `SWITCH_TENANT_ID` when configured. Database writes and queries are driven by the LLM's 4-way decision in the AI flow (`POST /ai/requests`) rather than unconditionally storing every incoming message on `POST /messages`.
- `core/` owns configuration and authentication. Development credentials belong only in the ignored `backend/.env` (or root `.env`); `get_settings()` automatically reloads when `.env` files are saved so credential updates take effect without restarting `uvicorn`. The health endpoint needs no credentials and confirms API liveness only.
- `controllers/messages.py` exposes `POST /messages`, `GET /messages/status`, and `GET /messages/recent`, all protected by a dedicated `X-Ingest-Token`. Latest-message status returns `sender_name` and `message` instead of counters; both are `null` before a message arrives. `models/messages.py` validates input, `services/message_service.py` enforces the configured tenant and room mapping for ingestion, status, and preview reads, and `repositories/message_receipts.py` holds bounded receipt metadata and redacted previews. Switch's `payload.sender_name` is forwarded separately from its opaque participant ID and redacted before retention. Missing or blank names use `Unknown sender`. Validation errors never reflect message contents. Logs contain receipt metadata by default; opt-in local debugging with `LOG_DISCORD_MESSAGES=true` adds redacted sender names and message previews in the API terminal. `core/message_logging.py` masks configured credentials, common credential formats, and URLs; escapes terminal controls; and limits previews to 2,000 characters. Raw text is suppressed if redaction fails.
- The browser viewer asks for the local ingest token in a masked field, keeps it only in tab memory, and polls the authenticated preview endpoint every two seconds. It displays sender names with message text rather than a count or receipt reference. Both fields render using `textContent`, never HTML; **Lock** clears the token, cancels pending requests, and removes displayed data. The page uses a restrictive Content Security Policy and message responses use `Cache-Control: no-store`. This is a local development viewer, not production user authentication.
- `python -m app.switch_listener` runs the separate WebSocket listener in `services/switch_listener.py`. It uses the existing agent credentials, declares configured rooms at connection open, receives all message events for those rooms, and forwards their sender IDs, sender display names, and contents to FastAPI. Run only one listener for that agent; declaring rooms takes over those claims from another client of the same agent. `--check` verifies API authentication, the WebSocket, and room membership without claiming rooms; `--status` shows the latest sender name and redacted message. It never registers agents or posts chat messages.
- `SWITCH_TENANT_ID` and `SWITCH_ROOM_IDS` bind the initial demo to one tenant and an explicit room allow-list. Unknown rooms are ignored. This is server-to-server ingestion authorization, not mobile user authentication.
- Heartbeat handling runs independently of API forwarding. The listener checkpoints its acknowledged cursor, connection ID, and generation under ignored `backend/.local/`, and resumes after transient disconnects. Permanent refusals, takeovers, and retention gaps stop with a safe diagnostic.
- Receipt metadata and duplicate detection are process-local, bounded to the latest 1,000 message identities, and reset on API restart. The latest 100 redacted previews are also held in memory and reset on restart. Run one worker. Receipt acknowledgement does not mean durable storage or extraction; raw message contents are discarded after acknowledgement.
- Backend tests cover health, record access, message authentication/authorization, preview isolation and retention, safe validation/logging, duplicate receipts, resume state, rejected deliveries, and WebSocket heartbeats while the API is slow. A local headless-browser check also verified unlock, automatic message updates, safe rendering of untrusted markup, lock cleanup, and invalid-token rejection with fictional messages.

The diagram's extraction, RAG, conflict checks, scheduler, LLM gateway, and `/ask`, `/docs/sync`, `/docs/apply-update`, and `/memories` endpoints remain future work. Additional external integrations and remote database migrations require a separate request. See `README.md` for Windows setup and run commands.

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

`controllers/ai.py` provides authenticated `POST /ai/requests` and `GET /ai/requests`. `models/ai.py` validates a caller-generated request UUID and either a question or a retained source reference, and defines the LLM's 4-way database decision (`db_action`: `store_only`, `query_only`, `none`, `store_and_query`). `services/ai_service.py` authorizes source previews, redacts before sending, serializes requests, correlates room, exact opaque AI sender, and request thread, and executes the LLM's chosen database action via `repositories/record_repository.py`. `repositories/ai_requests.py` retains bounded, redacted results scoped to tenant, input-room allow-list, responder, client, and internal room. Every request verifies that the room has no bridge/external channel and exactly the expected two participants before sending. No caller-supplied destination is accepted and no fallback to Discord exists. HTTP and WebSocket transport lives in `services/switch_api.py`; heartbeat processing runs independently of HTTP calls.

#### LLM 4-Way Database Decision (`POST /messages` & `POST /ai/requests`)

Rather than blindly writing every incoming message to Supabase on `POST /messages`, each incoming user message on `POST /messages` (via `AIService.evaluate_incoming_message`) as well as any explicit request on `POST /ai/requests` is evaluated by the LLM to decide what FastAPI should do with the Supabase database and the response across four options (`db_action`). FastAPI logs each step (`AI evaluating message`, `AI decision`, `AI database store/query complete`, and `AI response complete`) in the API terminal:

| # | Decision (`db_action`) | What FastAPI does with Supabase & the response |
|---|------------------------|------------------------------------------------|
| 1 | `store_only` | **Store in database only:** Ensures `tenants` and `rooms` exist, stores the source message in `messages`, and stores extracted `commitments`, `decisions`, and `memories` (`fact`, `decision`, `commitment`). Does not query existing database records for the response. |
| 2 | `query_only` | **Use database for the response:** Queries `memories`, `commitments`, `decisions`, and `messages` from Supabase (scoped to `SWITCH_TENANT_ID` and optional search keywords) and passes the retrieved database context to the LLM so the response is grounded in stored team memory. Does not store the prompt itself. |
| 3 | `none` | **Do nothing with the database:** Neither stores to nor reads from Supabase. Returns the LLM's direct response. |
| 4 | `store_and_query` | **Both store and access database for the response:** Stores the message and extracted `commitments`, `decisions`, and `memories` into Supabase first, then queries Supabase so the final response sent back incorporates both the newly stored items and historical database context. |

The existing feed and `/messages/status` retain their behavior. `SWITCH_REQUESTER_*` takes precedence for the input listener, so it no longer claims rooms using Codex's identity. The response connection claims only the client's internal room. Setup stores returned credentials only in ignored `backend/.env` and learns the AI's sender ID from a harmless correlated readiness reply, without parsing participant IDs. The viewer renders all questions and answers as text, polls automatically, and clears them and pending browser requests on lock. Its token stays in tab memory. These are local development interfaces, not production user authentication.

Server-only routing also requires removing the AI from Discord input rooms: an AI that remains a member can answer Discord messages independently of the API's private-room flow. After the user reported replies on both sides, setup was updated to retain `team-memory-client` and remove only `codex-discord` from configured input rooms. This was applied to both configured input rooms. Discord bridges, channel mappings, other agents, and Codex's internal response-room membership are preserved. Setup validates all source rooms before changing memberships and verifies each result; repeat runs do not repeat completed membership changes. Each API question now checks that Codex has not been re-invited to an input room, failing with a setup diagnostic before sending if that invariant is broken. The viewer remains the explicit trigger for AI questions; ordinary Discord chat is ingested silently.

A subsequent live audit after another duplicate-reply report found Codex back in one input room with a live input-room session, while its internal session was also live. The earlier membership-only verification did not establish durable isolation. Switch Console keeps session-to-room bindings separately from server membership; its session must be stopped with the Console **Stop** control for that Discord session, leaving the internal response session running. The available agent API has no stop-session operation and local process inspection could not safely identify the bound session's process. Both setup and API requests now also inspect reported runtime sessions, reject live input-room bindings or missing runtime evidence, and return an actionable stop-session diagnostic. Final server-only delivery must be reverified after that external session is stopped; it must not be inferred solely from a successful private answer or one membership snapshot.

After the user stopped the Codex session for `Idk`, setup and live checks reported no active Codex input-room sessions. During recovery the saved non-AI client's key returned `401`, and its configured identity/name were confirmed absent from Switch. The supplied registration key restored only that missing client without overwriting any existing agent; its new credentials were saved in ignored `.env`. Setup now verifies the preserved private room through Codex's existing access and re-invites a restored client before it attempts member-only room reads. The input listener was restarted with the restored credentials and covered both configured input rooms. A uniquely marked question was then sent through the running authenticated API; the expected private answer arrived, and four subsequent checks of both configured input-room histories found no copy of the marker. Runtime-session and membership checks passed throughout the observation window. Keep the Discord-attached `Idk` session stopped; reopening it would restore a separate reply path.

One process permits four pending questions, runs one at a time, and retains at most 100 results. Same request UUID and question is idempotent within retained history, and explicit viewer requests for an already-evaluated `source_message_ref` reuse that evaluation. A timeout defaults to 180 seconds; a disconnect or restart never blindly resends a potentially delivered request. Only the first matching nonblank response is retained; the prompt asks Codex to reply once. Each question/answer uses the existing redaction and 2,000-character preview cap. Results and pending jobs are process-local, while records chosen by `store_only` or `store_and_query` are persisted in Supabase (`tenants`, `rooms`, `messages`, `commitments`, `decisions`, `memories`). Run one API worker. Vector embedding generation (`Supabase/gte-small`) on `memories.embedding` remains future work.

On November 4, 2026, live setup verified a readiness reply from the existing Codex runtime, and the running FastAPI endpoint received the expected answer to a harmless arithmetic question through the unbridged room. Tests cover authentication, scope isolation, redaction before prompting, room routing checks, exact sender/thread correlation, heartbeats during HTTP waits, idempotency, and safe registration recovery. Switch's [agent protocol](https://docs.switchagents.ai/switch-rooms/internals/agent-protocol), [connections](https://docs.switchagents.ai/switch-rooms/internals/agent-connection), and [room routing](https://docs.switchagents.ai/switch-rooms/internals/rooms-and-resources) were checked before implementation. See README for restart and setup commands.

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
    | GET /messages/recent with X-Ingest-Token, every 2 seconds
Browser: Incoming messages screen at /
```

FastAPI and the listener are separate processes. Starting or reloading FastAPI does not start the listener. Both must remain running for new Discord messages to appear. The browser's **Live** indicator confirms successful API polling; it does not confirm that the listener is connected to Switch. Likewise, `/health` can return `200` while Discord delivery is stopped.

The listener reads settings from the ignored `backend/.env`, opens the outbound Switch connection, and forwards authorized room events to FastAPI. The browser contacts FastAPI only. The listener's persisted cursor survives restarts, while the API's receipt and preview memory does not. Restarting the API therefore clears displayed history; resuming the listener does not restore messages it already acknowledged. Unacknowledged events can be replayed while they remain in Switch's retention window.

### Delivery diagnostics and verified recovery

During the November 4, 2026 investigation, the API and authenticated preview endpoint were healthy, but the feed was empty because no listener process was running. The existing listener's `--check` command verified API authentication, the WebSocket connection, and membership in both configured rooms. Restarting the listener restored message acknowledgements, and the authenticated preview endpoint returned messages with sender and text fields. No application code change was needed for this recovery.

For an empty Incoming messages screen:

1. Confirm FastAPI is running, then run `python -m app.switch_listener --check` from `backend/` using its virtual-environment interpreter. This command checks connectivity and membership, then exits; it does not keep listening.
2. Confirm exactly one listener for the agent is running. If none is running, start `python -m app.switch_listener`. Do not start a competing listener, because it can take over room claims.
3. Check listener output for room coverage and `API acknowledged message ref=...`, and API output for the matching `Switch message received ref=...`. Receipt references allow delivery correlation without printing message text or credentials.
4. Unlock or refresh the browser feed and send a new harmless Discord marker. An authenticated empty preview response means no authorized previews are currently retained; `401` indicates an authentication failure, and `503` can indicate missing ingestion configuration.

The recovered local listener was started in the background, with output redirected to ignored `backend/.local/listener.stdout.log` and `backend/.local/listener.stderr.log`. This is a local process, not an installed service or automatic startup mechanism. Its checkpoint remains at `backend/.local/switch-listener.json`; do not delete or reset it as a routine recovery step.

Default delivery logs contain receipt metadata. `LOG_DISCORD_MESSAGES=true` separately enables redacted sender and message previews in the API terminal after authentication and authorization. HTTP and WebSocket wire-level debugging can expose authentication headers and must remain disabled. The current FastAPI entry point does not configure a general `LOG_LEVEL` setting.

## 1. What we are building

A team memory agent that lives in chat (Discord first) and:

- Silently reads team conversation and extracts decisions, commitments, facts and person profiles.
- Chases people about commitments they made in chat.
- Answers questions from that memory and from project docs, with citations.
- Notices when a chat decision contradicts a doc and proposes a doc update for approval.
- Replies only when it should. It stays silent otherwise.

Switch is required for the hackathon. Switch is the chat layer and the agent identity. Our Python backend does all the real work.

## 2. Decisions made

| # | Decision | Reason |
|---|----------|--------|
| 1 | Switch is the only chat layer for the hackathon (Discord through Switch's built-in bridge). No custom Discord bot. | Hackathon requirement. Less code. |
| 2 | Backend is Python: FastAPI, Postgres (Supabase) with pgvector, APScheduler, one LLM gateway. | Best library support for RAG and extraction. Standard stack. |
| 3 | Reuse one existing Codex AI plus one shared non-AI server client; no agent per server. Register the client once and preserve its credentials. | The user authorized the client so server requests have a different sender from the existing AI and do not take over its runtime. |
| 4 | A small custom agent client ("router client") with no LLM sits on the Switch WebSocket. | Lets our code decide when to reply, instead of an LLM answering everything. |
| 5 | The backend owns the reply decision (`router.py`). | Switch has no "reply only if needed" setting. |
| 6 | Every reply goes out through the `post_message` API. | Single path for speaking, easy to control. |
| 7 | Scheduling (chases) runs in our backend with APScheduler. | Switch has no scheduler of its own. |
| 8 | RAG runs asynchronously: acknowledge fast, search in the backend, post the answer when ready. | Embedding and search take seconds. |
| 9 | Data is keyed by `tenant_id` and `room_id`. Never by room name. | Names change and can collide. |
| 10 | Follow-ups are turned off and no main agent is assigned. | Otherwise Switch routes unaddressed messages to the agent. |
| 11 | A SaaS version would not use Switch. Plain Discord bot, one bot, many servers. | Switch is built for per-team self-hosting, and its license says internal use only (see section 10). |
| 12 | Scope for the demo: Discord only, one end-to-end flow. | One polished flow beats many rough features. |

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
   db.py          SQL (Supabase Postgres + pgvector)
   |
   +--> POST /agents/{agent_id}/message --> Switch --> Discord
```

The earlier draw.io file (`team-memory-agent-architecture.drawio`) shows this layout. It predates two later decisions: embeddings plus vector search in `answer.py` (the file still says keyword search), and the acknowledgement posted by the router client. Update it if you present it.

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

## 5. Reply policy (`router.py`)

Every message is ingested into the authenticated preview feed (`POST /messages`). When routed to the LLM (`POST /ai/requests`), the LLM decides one of four database actions (`store_only`, `query_only`, `none`, or `store_and_query`) before FastAPI finalizes the response. Replies happen only when the policy says so.

| Event | Action |
|-------|--------|
| Any message in a room | Store and extract silently (`POST /messages`). No reply. |
| Addressed question (`@memory-agent ...`) | Post a short acknowledgement, run RAG in the background, post the cited answer. |
| New commitment found | Store it. Reply only if `reply_mode` is `proactive`. |
| Commitment due or overdue | Scheduler posts a chase in the room, within quiet hours and cadence settings. |
| Decision contradicts a doc | Post a proposed doc update for approval. Never auto-apply. |
| Message from the agent itself | Ignore (loop guard). |

`reply_mode` values: `silent`, `mentions_only`, `proactive`. Set from the Preferences UI and read by `router.py`.

Because follow-ups may mark an unaddressed message as addressed, the backend should also check the message body for an explicit mention before treating it as a question. This is not yet verified.

## 6. RAG flow (acknowledge, then answer)

1. User asks `@memory-agent what did we decide about X?`.
2. Router client sees `addressed: true` and immediately posts "Looking that up..." with `post_message`. No LLM involved.
3. It sends the question to the backend with `room_id` and `message_id` as a background job.
4. Backend embeds the question, searches pgvector (filtered by `tenant_id` and `room_id`), and `llm.py` writes a cited answer.
5. Backend calls `POST /agents/{agent_id}/message` with the answer.
6. If retrieval fails or times out, post a short error so the user is not left waiting.

Use `message_id` as the job id so a reconnect or replayed event does not run the same search twice.

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

## 8. Multiple servers (later)

- One agent, one WebSocket, many rooms.
- Map `room_id` to `tenant_id` in the backend. Never use `room_name` as the identifier.
- A server has many rooms (one per channel). Keep the rooms-to-server mapping in `rooms`.
- If Switch does not give the server id in events, store the guild id at onboarding.
- Connecting a server to Switch is a manual step in Switch Console (token and Guild Id, admin only). No API found to automate it. Ask the Switch team.
- For the hackathon this is out of scope. One Discord server is enough.

## 9. Demo flow (hackathon)

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

## 10. Risks and open questions

**License.** Switch's legal terms (`/legal/terms`) grant a license "solely for Licensee's internal purposes" and bar making the software or its access available to third parties. That makes a multi-customer SaaS on Switch a poor fit and possibly a violation. Only that page was read. It may not match the GitHub repo's license. Read the repo's LICENSE, check the hackathon rules, and ask SandboxAQ in writing. The earlier comparison doc also flagged this. This is not legal advice.

**Unverified items (test before relying on them):**

- A message posted through `post_message` appears in the Discord channel as expected.
- The agent must be a room member to post (possible 403 otherwise).
- Messages from bots or webhooks may not count as coming from a person.
- `payload.addressed` behavior when follow-ups are on.
- Whether a thread parameter exists on `post_message`.
- Whether Switch events include the Discord server id.
- Whether one Switch connection can serve many Discord servers.
- A bot-posted `!follow-ups off` being accepted when provisioning a room.

**Loop guard.** Ignore the agent's own posts, or it can reply to itself.

**Visible Switch layer.** Users may still see Switch-voiced messages (for example "agent not available"), Switch's slash commands in the Discord menu, and the bot's permission requests. Keep the router client connected.

## 11. Competitive reality

The earlier comparison doc marked several features F for all competitors. Searches on November 4, 2026 found products that already do pieces of this:

- Chasing commitments in chat: Commitment Crawler, Mem Agent, and Claude Tag follow-ups (June 2026 roundup).
- Doc vs chat contradiction with proposed fix and approval: Slite Agent.
- Discord memory that is mostly silent, with cited answers: Cassandra for Discord and MemoryBot.
- Open-source or self-hosted memory layers: Mem0, Mengram (MIT, team memory, Curator for contradictions), Letta (Slack and Discord).

Not found, though not searched exhaustively: all five capabilities in one product across Discord, Slack and Teams; memory kept in the user's own Drive; person profiles built over time as we define them. These checks came from product pages and marketing, not hands-on tests.

**How to pitch it.** Do not claim "nobody does X". Pitch the combination and the closed loop: chat promise, then chase, then doc updated, then memory the user owns. Update the comparison table before presenting.

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
