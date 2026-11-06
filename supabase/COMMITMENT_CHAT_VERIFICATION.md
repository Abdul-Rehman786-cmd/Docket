# Automatic commitment chat verification — 2026-11-05

Status: locally implemented and verified. **No hosted migration, external message, real-team task mutation or live model execution was performed.** Feature activation remains disabled by default and requires reviewed deployment/onboarding.

## Tests actually executed

| Check | Result | What it proves |
| --- | --- | --- |
| Backend `.venv/Scripts/python.exe -m pytest -q` | **258 passed, 1 skipped**, existing Starlette/httpx deprecation warning | Regression coverage including ingestion, extraction, viewer, replies, manual lifecycle and automatic chat tests |
| Targeted `tests/test_commitment_chat.py` | **35 passed** | Mocked AI/HTTP, fake lifecycle persistence, real disposable SQLite journal, controllable clock |
| `npm run test:chat` | **Passed** | Actual disposable PostgreSQL/PGlite + pgvector applies core, lifecycle and chat migrations, then executes manual and chat SQL assertions |
| Python `compileall` and `pip check` | **Passed**, no broken requirements | New modules load; timezone dependency resolves locally |
| `git diff --check` | **Passed** | Patch whitespace checks |

The full regression run preceded the final provenance-field additions; the subsequent targeted run and SQL run cover those additions. No production persistence is inferred from the fake tests. The optional real embedding-model test remains skipped; no model weights were downloaded for this task.

Initial failures were resolved: relative UTC handling on Windows before timezone data was installed; and the old viewer test that expected a public credential dump. UTC has a standard-library path, `tzdata` is now declared/installed for IANA zones, and the viewer test now asserts credentials cannot be read or rewritten. This security change is deliberate, not a relaxation to make tests pass.

## Behavior verified

- Clear completion, cancellation, admin-authorized stable reassignment, relative/absolute rescheduling and explicit deadline clearing change the intended record.
- Tests inspect **resulting task state, version, audit actor/backend actor/evidence, linked memory content and embedding invalidation**, rather than accepting an AI proposal as success.
- Pending owner completion records automated acceptance only when explicitly enabled, keeps human approval pending and creates no human approver/time. Public/manual completion of pending work remains rejected. Admins cannot use that option to complete another member's pending task.
- Unauthorized senders, unknown/missing mappings, display-name impersonation, ambiguous targets/assignees, questions, quotes, hypotheticals and tentative statements leave commitments unchanged.
- An owner handoff proposal is refused; a mapped room admin's settled handoff succeeds only for a valid target. Duplicate aliases are ambiguous. Stable platform mention resolution is covered.
- Weekday resolution uses the original timestamp and confirmed timezone, including Asia/Karachi. Missing timezone/time, same-day weekday ambiguity and DST gaps/folds fail closed.
- Model-supplied task/source IDs and supporting evidence must match the backend plan. Multiple proposals cannot partially execute. Update-looking extraction cannot create a duplicate task; retrieval questions retain their query path.
- Two fake preflights racing before the transaction produce one applied action and one conflict. Current membership, mapping and policy are rechecked; stale record versions conflict.
- A simulated lost database acknowledgement after a successful write recovers from the real local journal without another AI call or duplicate audit/version change. Disabling the feature blocks cached proposals during recovery.
- Ambiguity finishes once and remains silent for unaddressed input; the journal retains a safe outcome/source reference. Existing sender/echo guards remain covered by the regression suite.
- Dedicated adapter authentication is separate from shared ingest/viewer credentials. Adapter authentication alone cannot unlock public lifecycle routes. Viewer environment read/write requests return 503 and leave configuration intact.

## Actual disposable SQL assertions

The runner uses a fresh local database only, with explicit `anon`, `authenticated` and backend roles. Fixtures roll back. It verifies:

- Sender-tuple mapping, scoped candidate preparation, immutable source replay checks and preserved platform citation identifiers.
- No candidate/mutation for a task whose original source conversation postdates the update.
- Pending policy denial, policy-change conflict, deliberate owner acceptance without fabricated human approval, accurate audit and inactive updated memory.
- Exact receipt replay without duplicate events; one-source/one-action enforcement even with another key/target.
- Initiator/source spoofing denial, revoked mapping denial and refusal of owner-only handoffs.
- Admin handoff, aware deadline update, stale-version rejection and cancellation/reminder ineligibility.
- Forced receipt-insert failure after task/memory/audit writes rolls them all back.
- Runtime backend execution, client function denials and inability of runtime credentials to provision identities/policies or update protected identity columns.
- Prior manual lifecycle fixture still passes after the shared transaction refactor.

This is real SQL execution, **not hosted Supabase**. PGlite's single connection does not establish live multi-connection lock contention, PostgREST discovery, Switch puppet-to-member mappings or private-runtime proposal quality. Python concurrency tests use a locked fake. Live verification requires separately approved disposable fixtures and exact-scope cleanup.

## Files / schema

- New proposal/source/policy contracts: `backend/app/models/commitment_chat.py`.
- Evidence gate and chat coordinator: `backend/app/services/commitment_chat_evidence.py`, `commitment_chat_service.py`.
- Existing lifecycle service/repository/models gain the internal chat execution contract while public authorization stays closed.
- Existing AI schema/prompt/worker and durable ingestion journal gain proposals, frozen plans, outcomes and recovery integration.
- Ingestion controller, security, listener/configuration add separate adapter attestation. Unsafe viewer environment routes are disabled.
- `requirements.txt` / local lock add timezone data; `.env.example` adds non-secret placeholders and a false feature flag.
- Additive `migrations/history/20261105000200_commitment_chat.sql` introduces policy/identity/source tables, separate automated-acceptance fields, audit provenance/outcomes, source uniqueness and shared transactional functions. Earlier migrations are preserved.
- README, architecture, lifecycle and deployment documents link the [complete automatic policy/API contract](../AUTOMATIC_COMMITMENTS.md).

## Remaining deployment requirements and limits

1. Review actual hosted schema and approve/apply non-destructive storage/embedding prerequisites, lifecycle migration and chat migration in order. Never run the destructive core reset remotely.
2. Onboard verified human sender tuples, stable room members and enabled policies. Confirm the room timezone and decide explicitly whether to allow pending owner completion.
3. Configure a distinct adapter-only secret locally, install dependencies, enable the global feature and restart the backend/listener. Do not expose secrets through the viewer or mobile app.
4. Verify approved disposable hosted actions, concurrent writes, RPC discovery and actual Switch identity behavior. No real external chat or team-task mutation is authorized by these local tests.

This initial policy supports conservative English statements and admin-only handoffs, not arbitrary multilingual dialogue or target-acceptance negotiation. Context is the current source plus bounded structured candidates in the same thread. Unassigned/name-only extracted commitments remain ineligible until they have a trusted stable owner. Public/manual routes still need individual-user authentication; the chat path uses the verified adapter/sender mapping instead. Scheduling, notifications, documents, recording and frontend redesign remain outside scope.
