# Single-script Supabase setup

For a new, empty database, run the complete contents of:

[`migrations/20261104000100_initial_schema.sql`](migrations/20261104000100_initial_schema.sql)

Run it once. It contains all currently implemented database modules in one
transaction: core scope/storage, memory embeddings, commitment lifecycle/audit,
chat actions, reminders, versioned documents (Markdown/text/PDF/DOCX/PPTX), simple
capture and person profiles. It uses no file includes, external scripts or secrets.
Runtime feature activation and backend credentials are configured separately.

The SQL needs PostgreSQL/pgvector/pgcrypto and the usual Supabase `anon`,
`authenticated`, `service_role` roles. The disposable test harness creates those
roles and extensions locally; a Supabase project supplies its standard roles.

## Existing installations

**Do not run this script on an existing hosted database.** It intentionally resets
Team Memory tables and data, including document/profile/audit data. Consolidation
was performed locally; the connected hosted project's data, schema and migration
history were not changed. Existing deployments require reviewed additive upgrades,
not this reset. Never run the archived module scripts after a fresh installation.

`migrations/history/` preserves the earlier module SQL for audit/reconciliation,
including proposals that were not deployed everywhere. It is not an active chain.
SQLite scripts remain separate because they target a different database engine.

## Verification

The standalone script is exercised in disposable PostgreSQL/PGlite by the existing
fresh/reset, lifecycle, chat, reminders, documents, storage and embedding suites.
Backend SQL-backed profile/capture tests load that same one script. No production
credentials or hosted database connections are used by these test harnesses.


Verified 2026-11-06: the standalone script creates all 21 project tables with RLS
and no direct anonymous/authenticated table access; fresh creation and a second
explicit disposable reset both pass. SQL audit, useful-storage, embedding, lifecycle,
chat, reminder and document/format suites pass. The complete backend suite passes
571 tests with real local inference enabled. Earlier audit fixtures were updated
to check versioned reminder occurrence uniqueness and delete only an unreferenced
memory, preserving the current source-evidence restriction.
