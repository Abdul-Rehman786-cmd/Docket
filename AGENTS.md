# AI coding rules — Team Memory Project

Read `architecture.md` and `README.md` before creating or changing files. Work in small, testable steps. Do not implement external messaging integrations until explicitly requested.

## Absolute security and Git rules
1. NEVER expose, print, log, paste, hardcode, commit, or include real API keys, passwords, access tokens, private connection strings or other secrets in code, tests, docs, prompts, terminal transcripts, issue comments, screenshots, or client builds.
2. Store development secrets ONLY in ignored local `.env` files. In deployed environments, an approved secret manager / host environment variables are permitted. Never commit real `.env` files; only commit `.env.example` with non-secret placeholders.
3. BEFORE `git add`, commit, or push: verify `.gitignore` covers every `.env` in the repository; run `git status --short`, `git check-ignore backend/.env mobile/.env`, inspect `git diff --cached --name-only` after staging, and scan staged content for secrets. Do not proceed if credentials may be included.
4. NEVER run `git push --force`, `git push --force-with-lease`, `git reset --hard` against shared history, or rewrite shared Git history. Do not push to GitHub unless explicitly instructed.
5. If a secret is exposed or committed, stop, notify the user, rotate/revoke it, and assess repository history. Adding `.gitignore` later is insufficient.
6. Expo `EXPO_PUBLIC_*` variables are readable by every app user. ONLY put non-sensitive values there (for example, the public API URL). Never place a Supabase service key, private database key, or LLM key in React Native.
7. Privileged Supabase keys must stay on the backend. Do not ship an unauthenticated endpoint that accesses private user data. Do not put credentials in HTTP query strings.

## Architecture and development rules
- Use MVC-style separation: `controllers/` for HTTP endpoints, `models/` for validation and domain types, `services/` for business rules, `repositories/` for database access, `core/` for configuration/security.
- Keep database queries out of controllers. Keep business logic out of repositories. Keep secrets out of every file except ignored local `.env` files or approved deployment secrets.
- FastAPI is the only backend API for React Native. The mobile app must not use the Supabase privileged API directly.
- Keep ALL schema migrations under `supabase/migrations/` using timestamped SQL names. Never change an already-applied migration; write a new one. Ask before applying destructive SQL.
- Add tests for endpoints and security. No new endpoint for private data without authentication and authorization.
- Avoid unnecessary dependencies; update `README.md` and `architecture.md` when interfaces change.

## MCP guidance
- Use documentation MCPs to check vendor APIs before coding. MCP installation does NOT mean a live database or service is connected.
- Do not add or use any external messaging or agent integration unless asked.
- Never provide a coding agent with unrestricted production database write access. Request approval before schema changes or migrations against remote environments.
