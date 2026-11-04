# Team Memory Agent: Architecture and Implementation Plan

Prepared November 4, 2026 for the Flip the Switch hackathon. This file records what we decided, why, and what is still unverified.

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
| 3 | One shared Switch agent for all servers and rooms. Do not create or delete an agent per server. | Switch docs: one agent can be in many rooms. Docs say to register once. |
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

Every message is ingested silently. Replies happen only when the policy says so.

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

Every table carries `tenant_id` and `room_id`. Every query and every vector search filters on `tenant_id`. Postgres row-level security in Supabase can enforce this at the database level.

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
