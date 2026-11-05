# Feature logs

Feature tracing is enabled locally with `FEATURE_LOGS_ENABLED=true` and `LOG_LEVEL=INFO` in ignored `backend/.env`. The example file documents these non-secret switches. No business feature, delivery flag, policy, database migration or external integration was enabled by this logging change.

Restart the API and the separate Switch listener to load the console configuration:

```powershell
# Terminal 1, from backend/
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
# Terminal 2, from backend/
.\.venv\Scripts\python.exe -m app.switch_listener
```

Look for `[feature] component=... state=...`. Events are INFO-level on `team_memory.features`. Normal feature logging is separate from HTTP/WebSocket wire debugging, which remains disabled. There is no new file log or database logging table.

| Component | What the event proves |
| --- | --- |
| `startup` | Process starting/stopping; configuration summary only |
| `switch_listener` | Room connection, API acknowledgement, duplicate delivery or reconnect attempt |
| `ingestion` | Received, ignored agent/echo, durable journal persisted, duplicate, recovered, completed or failed processing |
| `ai` | Job queued/started, parsed decision and extraction counts, processing success or safe failure category |
| `confirmation` | Draft staged, saved after confirmation, cancelled or failed; saving a draft is not human approval of a task |
| `storage` | A storage acknowledgement returned with counts, or configuration/storage failure |
| `chat_commitment` | Candidate context prepared, proposed action, ambiguous evidence or denied authority |
| `commitment` | Authorized reads/history counts, requested action, conflict/denial, successful persisted change, equivalent no-op or receipt replay |
| `embedding` | Worker availability, claims, completed embeddings, stale claims or failures |
| `retrieval` | Semantic results or keyword fallback, with result counts |
| `viewer` | Authenticated socket connection, refusal and disconnection |
| `reminder_planner` | Worker availability, intentions persisted, deduplicated/rechecked work or no eligible work |
| `reminder_dispatch` | Claim, deferral, stale-state skip, durable send-start, retry/failure/ambiguity or saved acknowledgement |
| `reply` | Existing reply helper returned or failed; not proof of Discord receipt |

Examples of event shapes (illustrative, not evidence of hosted execution):

```text
[feature] component=commitment state=blocked reason=human_auth_unavailable status=503
[feature] component=chat_commitment state=disabled reason=flag_disabled
[feature] component=ai state=prepared action=store_only commitments=1 decisions=0 memories=0 proposed=0
[feature] component=commitment state=applied action=complete task=<hash> changed=true version=2
[feature] component=commitment state=replayed action=complete task=<hash> changed=true version=2
[feature] component=reminder_planner state=persisted ref=<hash> task=<hash> policy_version=1 version=2
[feature] component=reminder_dispatch state=deferred reason=quiet_hours_or_cadence ref=<hash> wait_seconds=3600
[feature] component=reminder_dispatch state=acknowledged ref=<hash> attempts=1
```

`configured` means settings are present, not that hosted functions, grants, mappings or external delivery were verified. `prepared` means parsing/context preparation, not a mutation. `commitment applied` is emitted only after the repository returns a validated lifecycle result. `replayed` describes the saved earlier result, so `changed=true` on a replay does not mean another write occurred.

`reminder send_started` means the durable marker was saved before calling Switch. It is not success. `acknowledged` is emitted only after a valid Switch receipt and successful database result save; it does not prove downstream Discord delivery or human receipt. A timeout or interrupted attempt can be `ambiguous` and is not blindly resent. `ingestion succeeded` means processing finished; a staged confirmation or `none` decision can finish without a Supabase write. Use `storage succeeded` and counts for storage evidence.

`ref` and `task` are stable short SHA-256 correlations. Raw user/actor identifiers, display names, task descriptions, message bodies, prompts, answers, reasons entered by users, credentials, URLs and remote exception bodies are not included in these new events. Only known event/reason/action values and bounded numeric/boolean metrics are emitted; unknown diagnostics become `other`. Legacy opt-in redacted message-preview logging remains separate and was not enabled. Do not turn on transport wire debugging.

Repeated empty-worker heartbeats are limited to once per minute per component. Disabled/blocked worker availability is logged on state changes; active work has individual transition events. Failed durable jobs are visible through later recovery/queue events and attempt counts, within the existing retry limits. Logging does not add database probes, model calls, sends or retries.

Automatic chat management, public human authentication and reminder deployment keep their existing gates. Disabled or blocked lines explain why those paths currently cannot run; enabling their logs does not activate them. See [REMINDERS.md](REMINDERS.md) and [AUTOMATIC_COMMITMENTS.md](AUTOMATIC_COMMITMENTS.md) for deployment dependencies.

Set `FEATURE_LOGS_ENABLED=false` to turn off these extra events. Existing application diagnostics remain governed by their existing configuration. Tests use mocked/fake operations to check that failure/replay/ambiguity is not logged as a fresh success and that secrets/content cannot be reflected into trace fields.
