# Team Memory — System Architecture (v1, foundation only)

## Goal
Build a maintainable foundation for a team knowledge and commitments application, consisting of a FastAPI server, Supabase PostgreSQL database, and React Native/Expo mobile frontend. **No third-party messaging listener, agent protocol, or bot integration is included at this stage.**

## High-level design
```text
+----------------------------------+
| React Native mobile app (Expo)    |
| UI and non-secret public config  |
+----------------+-----------------+
                 | HTTPS / JSON
                 v
+----------------------------------+
| FastAPI server                   |
| Controllers (API and validation) |
|   -> Services (business rules)   |
|   -> Repositories (data access)  |
| Models (Pydantic schemas)        |
| Core (env, auth, error handling) |
+----------------+-----------------+
                 | server-side only
                 v
+----------------------------------+
| Supabase PostgreSQL              |
| commitments, decisions, messages |
| SQL: supabase/migrations/        |
+----------------------------------+
```

## MVC-style mapping
FastAPI does not impose traditional MVC. This project uses an MVC-inspired layered structure:
- **Model**: Pydantic schemas in `app/models/`, PostgreSQL schema under `supabase/migrations/`.
- **View**: React Native UI in `mobile/` (API responses are JSON presentation to the client).
- **Controller**: HTTP route handlers in `app/controllers/`.
- **Service**: logic/validation that must be independent of HTTP or database details.
- **Repository**: database queries with no HTTP handler logic.

## Data model
- `messages`: optional future source material; body, author, timestamps. No platform-specific fields yet.
- `commitments`: description, owner, optional deadline, approval status, timestamps.
- `decisions`: summary, approval status, timestamps.
- `commitments.message_id` and `decisions.message_id` may reference messages when linked later.
- New records default to `pending` review. Approval statuses are `pending`, `approved`, `rejected`.

## API plan
- `GET /health`: public server health check, implemented.
- `GET /api/v1/commitments`: example protected data endpoint, implemented only for local/server-side administrator testing.
- `GET /api/v1/decisions`: example protected data endpoint, implemented only for local/server-side administrator testing.
- Before using these endpoints from the mobile client, implement user sign-in and verify per-user identity/authorization server-side. **Never embed the admin API token in the mobile app.**

## Security boundary
- `backend/.env`: local backend admin token and Supabase server credentials (ignored by Git).
- `mobile/.env`: non-secret public API URL only (ignored by Git).
- RLS enabled in Supabase; no anonymous policies shipped. A privileged backend Supabase credential bypasses RLS and must be used only after backend authentication/authorization.
- Local development is not production security. Production requires HTTPS, authentication, per-user authorization, rate limiting, auditing and scoped database access.

## Migration strategy
- Put **all** migrations in `supabase/migrations/`.
- Use `YYYYMMDDHHMMSS_description.sql` file names.
- Review and test SQL before applying remotely. Never edit previously applied migrations.

## Current status and roadmap
1. **Included**: FastAPI skeleton, controller/service/repository separation, PostgreSQL schema migration, Expo starter screen, security rules, and tests.
2. **Next**: configure Supabase project, apply migration, verify protected data read.
3. **After that**: implement mobile authentication and per-user permissions.
4. **Later**: business features (create/approve commitments and decisions, LLM processing, integrations) only after requirements are agreed.
