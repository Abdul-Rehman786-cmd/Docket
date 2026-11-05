"""Content-free feature events. Unknown fields/reasons are never reflected into logs."""
import hashlib
import logging
import time
from app.core.config import get_settings

COMPONENTS={'application','startup','ingestion','ai','reply','confirmation','commitment','chat_commitment',
            'reminder_planner','reminder_dispatch','reminder_worker','embedding','retrieval','viewer','storage','switch_listener'}
STATES={'starting','stopping','configured','enabled','disabled','blocked','stopped','received','duplicate','skipped','queued',
        'recovered','retry_scheduled','exhausted','prepared','proposed','requested','applied','no_change','replayed',
        'conflict','failed','persisted','deferred','superseded','claimed','send_started','acknowledged','ambiguous',
        'staged','confirmed','cancelled','succeeded','idle','connected','disconnected','unknown'}
REASONS={'flag_disabled','database_not_configured','scope_not_configured','ai_not_configured','adapter_not_verified',
         'sender_timestamp_unavailable','mapping_or_policy_unavailable','human_auth_unavailable','own_echo_or_ai_sender',
         'awaiting_configuration','journal_unavailable','storage_unavailable','switch_unavailable','timeout','interrupted',
         'unexpected_failure','invalid_storage_result','commitment_storage_unavailable','not_found','forbidden_action',
         'stale_version','invalid_transition','idempotency_conflict','invalid_assignee','invalid_action_payload',
         'clarification_required','unsupported_evidence','ambiguous_commitment','ambiguous_assignee','unresolved_deadline',
         'invalid_scope','ineligible_commitment','unexpected_assignee','unexpected_deadline','sender_not_owner','handoff_requires_admin',
         'no_unique_proposal','no_eligible_intention','tick_already_running','current_state_recheck','quiet_hours_or_cadence',
         'switch_connect_failed','switch_rate_limited','switch_rejected','send_outcome_unknown','acknowledgement_unavailable',
         'delivery_not_configured','database_or_model_unavailable','worker_unavailable','keyword_fallback','semantic',
         'no_vector_matches','disabled','vector_search_unavailable','viewer_auth_failed','rpc_not_available','database_request_rejected',
         'read_detail','read_list','read_history','explicit_mention_required','other'}
ACTIONS={'create','list','approve','reject','start','complete','cancel','reassign','reschedule','store_only','query_only','store_and_query','none','clarify'}
METRICS={'count','attempts','version','policy_version','candidates','scanned','persisted','proposed','acknowledged','messages','commitments','decisions','memories',
         'queued','pending','rooms','enabled','delivery_enabled','database_configured','ai_configured','adapter_configured',
         'upcoming','overdue','changed','wait_seconds','status','limit','semantic_matches','embedding_jobs'}
_LAST_IDLE={}


def feature_event(component,state,*,reference=None,task=None,action=None,reason=None,**metrics):
    """Only literals/enumerations, bounded counters and hashed correlations are emitted.

    Do not pass bodies, prompts, names, tokens, URLs or exception messages. Logging
    failures must never change a domain result or notification delivery outcome.
    """
    try:
        if not get_settings().feature_logs_enabled:return
        if state=='idle':
            now=time.monotonic()
            if component in _LAST_IDLE and now-_LAST_IDLE[component]<60:return
            if component in COMPONENTS:_LAST_IDLE[component]=now
        fields=[('component',component if component in COMPONENTS else 'application'),
                ('state',state if state in STATES else 'unknown')]
        if action is not None:fields.append(('action',action if action in ACTIONS else 'unknown'))
        if reason is not None:fields.append(('reason',reason if reason in REASONS else 'other'))
        for name,value in (('ref',reference),('task',task)):
            if value is not None:fields.append((name,hashlib.sha256(str(value).encode()).hexdigest()[:12]))
        for name,value in sorted(metrics.items()):
            if name in METRICS and isinstance(value,(bool,int)) and abs(value)<=1000000000:
                fields.append((name,str(value).lower() if isinstance(value,bool) else value))
        logging.getLogger('team_memory.features').info('[feature] %s',' '.join(f'{key}={value}' for key,value in fields))
    except Exception:
        return


def startup_events(settings,ai_configured):
    feature_event('startup','starting',rooms=len(settings.allowed_switch_rooms))
    feature_event('storage','configured' if settings.supabase_configured else 'blocked',
                  reason=None if settings.supabase_configured else 'database_not_configured')
    feature_event('ai','configured' if ai_configured else 'blocked',reason=None if ai_configured else 'ai_not_configured')
    feature_event('chat_commitment','configured' if settings.automatic_commitments_enabled else 'disabled',
                  reason=None if settings.automatic_commitments_enabled else 'flag_disabled',
                  adapter_configured=bool(settings.switch_adapter_token.get_secret_value()))
    feature_event('commitment','blocked',reason='human_auth_unavailable')
    feature_event('reminder_planner','configured' if settings.reminders_enabled else 'disabled',
                  reason=None if settings.reminders_enabled else 'flag_disabled')
    feature_event('reminder_dispatch','configured' if settings.reminders_enabled and settings.reminder_delivery_enabled else 'disabled',
                  reason=None if settings.reminders_enabled and settings.reminder_delivery_enabled else 'flag_disabled')
    feature_event('embedding','configured' if settings.local_embeddings_enabled else 'disabled',
                  reason=None if settings.local_embeddings_enabled else 'flag_disabled')
