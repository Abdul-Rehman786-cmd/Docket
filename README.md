# Team Memory

Team Memory remembers useful team information, commitments and documents from
chat, then provides source-backed answers. Messaging passes through Switch to a
Python listener and FastAPI; the backend uses the existing private AI runtime,
local embeddings and its configured database.

## Main documentation

- [architecture.md](architecture.md): **main architecture reference**, implemented
  boundaries, data model, policies, limitations and planned work.
- [Operations](docs/OPERATIONS.md): full setup, commands, module contracts,
  document uploads/questions, commitments, reminders, profiles and troubleshooting.
- [Database setup](docs/DATABASE.md): fresh installation, existing deployments
  and migration-history rules.
- [Verification history](docs/VERIFICATION.md): preserved audit, test and deployment
  reports. Historical results do not establish current live readiness.
- [AGENTS.md](AGENTS.md): repository development and security rules.

## Start the backend

From the repository root in PowerShell:

```powershell
cd backend
python -m pip install -r requirements-dev.txt
python -m pip install -r requirements-embeddings.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Use your selected Python environment consistently. Configure ignored
`backend/.env` locally; do not publish credentials. If you use port 8001, set
`SWITCH_BACKEND_URL=http://127.0.0.1:8001`.

In another terminal, from `backend`, run one listener:

```powershell
python -m app.switch_listener
```

Stop another listener using the same agent first. Each new listener process
starts at Switch's current stream head and skips offline messages. Accepted
backend jobs and confirmed document imports recover from durable storage;
keep `backend/.local/`. Transient reconnects within the process resume its cursor.

Open the local API's `/health`, `/docs` or browser viewer `/`. Health confirms
API liveness only. Full installation, model setup, Switch identity configuration,
mobile setup and troubleshooting are preserved in the
[application reference](docs/OPERATIONS.md#application-reference).

## Documents and commitments

Supported document inputs are Markdown/text, text-based PDF, DOCX and PPTX.
Uploader-only `yes`/`save` consent starts ingestion; replies stay in the channel.
Ask the bot to list/find documents or ask questions about a selected document.
Conversation state currently expires after 15 minutes or an API restart; use one
API process for consistent selection. See the
[document chat contract](docs/OPERATIONS.md#document-chat) for exact limits.

Tagged first-person commitments use the existing atomic storage and embedding
path. Advanced lifecycle and reminder features are separately configured; do not
infer their activation from successful chat or document ingestion. See
[commitment capture](docs/OPERATIONS.md#commitment-capture) and
[reminders](docs/OPERATIONS.md#reminders).

## Credentials and administration

Unlock the viewer's Settings panel with `ADMIN_API_TOKEN`. Saved credentials show
configured status only. Enter replacements or leave blank to keep existing keys.
Tokens/entered keys are not cached or exported; saves atomically update the ignored
local `.env`. The ingest/viewer token grants no admin access. Restart affected
services after changing connection settings or startup-only feature flags.

## Database safety

For a **new, disposable database**, the standalone
[baseline SQL](supabase/migrations/20261104000100_initial_schema.sql) contains the
implemented modules. **It resets project tables: never run it on existing hosted
team data.** Existing deployments require separately reviewed additive upgrades.
All PostgreSQL module SQL is integrated into that baseline; earlier module files
remain in Git history only. The SQLite profile upgrade stays separate because its
SQL dialect is different. See [database setup](docs/DATABASE.md).

## Local checks

From `backend`:

```powershell
python -m pytest -q
```

From the repository root:

```powershell
node --test backend/tests/viewer_socket.test.cjs
```

Disposable SQL tests run from `supabase/tests` using its package scripts. They do
not apply hosted migrations. See [database verification](docs/DATABASE.md#database-setup-verification)
and the [verification archive](docs/VERIFICATION.md).
