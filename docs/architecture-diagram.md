# Team Memory architecture

Based on the current [README](../README.md), [architecture](../architecture.md)
and inspected frontend/controller/service code. Solid arrows show implemented
relationships, not proof of hosted deployment. Dashed arrows show unfinished
integration. Amber means partial or gated; red marks a security gap.

[Open the standalone SVG diagram](team-memory-architecture.svg).

## Application and data pipeline

```mermaid
flowchart TB
    subgraph clients["Chat and frontend"]
        discord["Discord: messages and uploads"]
        slack["Slack: connected chat; documents incomplete"]
        browser["Browser: feed, Ask Codex, Settings"]
        mobile["Expo mobile: health-check starter"]
        product["Future: authenticated tasks, documents and search UI"]
    end

    subgraph integration["Switch boundary"]
        bridge["Existing messaging bridges and scoped rooms"]
        listener["One non-AI listener: sender/source metadata, addressing, loop guards"]
        privateAI["Private AI room: existing Codex runtime"]
        outbound["Non-AI Switch transport: application replies and notifications"]
    end

    subgraph backend["FastAPI - one API worker"]
        ingress["Controllers and validated models: ingest authentication, adapter checks, room scope"]
        viewer["Viewer feed and question API: authenticated HTTP / WebSocket"]
        settings["Settings controllers: current authorization gap"]
        journal["Durable ingestion and recovery journal"]
        orchestrator["AI orchestration: structured proposals and answer synthesis"]
        capture["Tagged commitment capture / fact and decision extraction"]
        lifecycle["Commitment service: transitions, owner authority, versions and audit"]
        documents["Documents: uploader consent, bounded parsing, versions and chunks"]
        profiles["Optional source-backed person profiles"]
        repositories["Repositories: scoped reads and atomic writes / idempotency"]
        embed["Embedding workers: local gte-small, 384 dimensions; retry and stale-vector checks"]
        retrieval["Scoped memory / published-document retrieval and citations"]
        planner["Reminder eligibility and pure planner -> scheduler"]
        outbox["Durable delivery records -> claimed dispatcher -> eligibility recheck"]
    end

    subgraph persistence["Backend-only persistence"]
        cloud[("Supabase: relational records, audit, memories, document chunks and vectors")]
        local[("SQLite: supported record/vector/profile/reminder adapters; not full parity")]
        state[("Local SQLite journals: receipts, consent, recovery and document reply outbox")]
        config["Ignored backend .env / deployment secrets + database room policies"]
    end

    discord <--> bridge
    slack <--> bridge
    bridge --> listener --> ingress --> journal --> orchestrator
    listener -. "Slack document normalization missing" .-> documents
    browser <--> viewer
    browser --> settings
    mobile -->|"GET /health only"| ingress
    mobile -.-> product
    viewer <--> orchestrator
    orchestrator <-->|"reasoning through existing private runtime"| privateAI
    orchestrator --> capture & lifecycle & profiles & documents
    capture --> repositories
    lifecycle --> repositories
    profiles --> repositories
    documents --> repositories
    documents -->|"published versions only"| retrieval
    repositories <--> cloud
    repositories <--> local
    repositories -->|"durable jobs"| embed
    embed -->|"persist vectors; publish complete document versions"| repositories
    retrieval <-->|"authorized scope / current vectors"| repositories
    retrieval --> orchestrator
    repositories -->|"eligible snapshots and history"| planner
    planner --> outbox
    outbox <-->|"claims, leases and confirmed-send state"| repositories
    outbox --> outbound
    orchestrator -->|"configured chat confirmation / answer"| outbound
    outbound --> bridge
    journal <--> state
    documents <-->|"consent and notification recovery"| state
    settings -->|"engine, retention, reminders, migration"| config
    config --> repositories & planner

    classDef partial fill:#fff7e6,stroke:#c98a13,color:#573b09;
    classDef blocked fill:#fff1f2,stroke:#d04c66,color:#861c35;
    classDef planned fill:#f3f4f6,stroke:#94a3b8,stroke-dasharray:5 4,color:#475569;
    class slack,lifecycle,documents,mobile,local partial;
    class settings blocked;
    class product planned;
```

The repository box represents multiple adapters, not a universal engine switch:
document import/chat/embedding and lifecycle persistence still use Supabase.
General-memory and document-chunk embedding jobs are separate. Database source
messages are distinct from local processing journals. UI questions display their
answers in the viewer; only policy-approved chat work uses the outbound path.

## Frontend settings and the actions they control

```mermaid
flowchart LR
    subgraph frontend["Browser Settings - current local development UI"]
        engine["Database Engine: Supabase / SQLite + local path"]
        migrate["Migrate to SQLite / migrate-and-switch modal"]
        reminders["Meeting and Task Reminders: enabled + advance notice"]
        retention["Storage and Retention: cutoff / delete older data"]
        quota["SQLite max space: enable, value and unit"]
        env["Environment Configuration: existing values and save controls"]
    end
    subgraph api["FastAPI settings routes - authorization still required"]
        envapi["/viewer/env"]
        migrateapi["/viewer/migrate-db"]
        reminderapi["/viewer/reminders/configure"]
        retentionapi["/viewer/storage/prune-timeline"]
        quotaapi["/viewer/storage/enforce-space"]
        status["/viewer/storage/status and /viewer/reminders/status"]
    end
    subgraph effects["Backend effects"]
        config["Ignored .env -> validated Settings -> supported repository selection"]
        copy["Migration service: Supabase records -> SQLite; not continuous sync"]
        policy["Reminder settings service: global flags + room policies"]
        prune["Storage service / retention worker: selected-engine cleanup"]
        evict["SQLite quota enforcement and usage metrics"]
        workers["Planner / dispatcher: eligibility, quiet hours, cadence and leases"]
    end
    engine --> envapi --> config
    env --> envapi
    migrate --> migrateapi --> copy
    reminders --> reminderapi --> policy --> workers
    policy --> config
    retention --> retentionapi --> prune
    quota --> quotaapi --> evict
    status --> engine & reminders & retention & quota
    config --> prune & evict
```

Settings are controls for backend behavior, not a separate database/AI pipeline.
The current environment/settings/migration/storage handlers lack route-level
authorization; the diagram records that gap and does not endorse public exposure
or use of those handlers for secrets. Edit credentials directly in ignored local
configuration. Mobile currently has no equivalent settings interface. Quiet hours
and authority remain policy/service concerns, not fully implemented UI controls.

## Gates and completion boundaries

| Path | Required checks / current limit |
| --- | --- |
| Chat ingestion | Ingest credential, configured tenant/rooms, stable source identity, duplicate/self guards. |
| Tagged task creation | Supported addressing plus a clear personal promise; owner from sender metadata; no registration prerequisite. |
| Advanced actions | Trusted adapter, owner/role policy, eligible state, evidence, expected version, idempotency and atomic history. Manual human-auth routes remain closed. |
| Discord upload | Document adapter, exact allowed bridge/self participant, uploader-only consent, enabled room, bounded parser and document schema. PDF save reported; full cited retrieval unverified. |
| Document questions | Separate valid general adapter token; authorized published document scope. Current reported failure is adapter verification. |
| Slack documents | Platform-aware normalization, allow-list and chat scope integration unfinished in this checkout. |
| Reminders | Global planning/delivery gates, current room policy and eligible accepted task/deadline; supersede stale intentions and handle uncertain sends. |
| Storage | SQLite adapters cover selected modules. Supabase lifecycle/document functions and reviewed hosted deployment remain necessary. |

Future components: full authenticated mobile/web product screens, protected
preferences, document conflict detection and reviewed write-back, recording
controls and multi-workspace onboarding. These are not hidden behind completed
boxes in the application diagram.

The active fresh-install SQL is
[`20261104000100_initial_schema.sql`](../supabase/migrations/20261104000100_initial_schema.sql).
It resets tables and must never be replayed on existing hosted data. Remote schema
reconciliation needs separate review/approval. No services, schema or live messages
were changed to produce this diagram.
