# Optional person profiles

Profiles collect explicit statements from existing authorized message ingestion.
They are disabled by default. This module adds no messaging integration, model
account, identity lookup, mobile UI or historical backfill.

## Identity and evidence

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

## Supported first version

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

## Storage and retrieval

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

## HTTP reads

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

## Deployment and local verification

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

## Verification recorded for this implementation

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

## Approved hosted deployment

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

## Sender-isolation correction

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
