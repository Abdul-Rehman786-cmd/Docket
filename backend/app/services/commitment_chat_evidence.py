"""Conservative evidence gate, independent of AI confidence or its claimed authority.

Unsupported phrasing is ambiguous. Expand this grammar only with negative tests.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.commitment_chat import ChatPlan, TaskChangeProposal
from app.core.chat_participation import without_agent_mention

STOP = {'i','my','the','a','an','task','please','to','by','send','sent','deliver','delivered',
        'finish','finished','complete','completed','prepare','write','submit','submitted'}
UNSAFE = re.compile(r'\b(if|might|maybe|perhaps|could|would|will|not|never|hypothetically|said|asked|quote|but)\b', re.I)
PATTERNS = {
    'start': re.compile(r'i (?:have )?started (?:my|the) (?P<object>[\w -]{2,160})\.?', re.I),
    'complete': re.compile(r"(?:i(?:'ve| have)? (?:sent|finished|completed|delivered|submitted|done) (?:the |my )?(?P<object>[\w -]{2,160})|(?P<bare>done|finished))\.?", re.I),
    'cancel': re.compile(r"cancel (?:my|the) (?P<object>[\w -]{2,160}?)(?: task)?(?:;? (?:it(?:'s| is) no longer needed))?\.?", re.I),
    'reassign': re.compile(r'(?P<assignee>[\w @<>!-]{1,256}?) is taking over (?:the|my) (?P<object>[\w -]{2,160})\.?', re.I),
    'reschedule': re.compile(r'(?:move (?:my|the) (?P<object>[\w -]{2,160}?) deadline to (?P<when>[^\n;!?]{1,128})|(?:clear|remove) (?:my|the) (?P<clear_object>[\w -]{2,160}?) deadline)\.?', re.I),
}


def update_intent(body: str) -> bool:
    """Suppress new task extraction for lifecycle-looking inputs, including uncertainty."""
    return bool(re.search(r"\b(done|finished|completed|sent|delivered|submitted|cancel|taking over|deadline|reschedule)\b"
        r"|\bstarted\b|\b(?:might|maybe|could|would)\s+(?:finish|complete|send|deliver|cancel|reschedule)\b", body, re.I))


def commitment_intent(body):
    return update_intent(body) or bool(re.search(r"\bI(?:['\u2019]ll| will| commit to)\b|\b(?:what are|list|show) my (?:tasks|commitments)\b",body,re.I))


def own_request_kind(body):
    """Routing only, never mention/identity proof or permission to execute."""
    text=re.sub(r'^\s*@[\w.-]+[:,]?\s+','',body.replace('\u2019',"'")).strip()
    if re.fullmatch(r'(?:what are|show|list) my (?:tasks|commitments)\??\.?',text,re.I): return 'list'
    if '?' not in text and re.match(r"(?:I(?:'ll| will| commit to| started| finished| completed)\b|I(?: have|'ve) (?:sent|finished|completed|delivered|submitted)\b|cancel my\b|(?:move|clear|remove) my .+deadline\b)",text,re.I):
        return 'action'
    return None


def personal_commitment(body, metadata):
    """Use Switch addressing, not member verification, for simple capture.

    `addressed` may include follow-ups: still require an explicit mention token.
    Names/native IDs here are formatting, not ownership or authorization evidence.
    """
    if not metadata or not metadata.get('addressed'): return None
    text=body.strip().replace('\u2019',"'")
    if any(c in text for c in ('?', '"', '`', '\n')) or text.startswith('>'): return None
    # Switch handles actual names/aliases and marks the message addressed. This
    # also accepts the normal native Discord mention syntax delivered by adapters.
    text,count=re.subn(r'^(?:@[\w.-]+|<@!?[A-Za-z0-9_-]+>)[,:]?\s+', '', text, count=1)
    if not count: return None
    match=re.fullmatch(r"I(?:'ll| will| commit to) (?P<task>[\w ,/-]{2,500}?)(?: by (?P<when>[^\n;!?]{1,128}))?\.?",text,re.I)
    if not match or re.search(r'\b(if|might|maybe|perhaps|not|never|hypothetically|said|quote)\b',text,re.I): return None
    return match['task'].strip().rstrip('.'), match['when'].rstrip('.') if match['when'] else None


def protect_extraction(decision,body,enabled):
    if not enabled or decision.task_changes or not update_intent(body): return decision
    # Keep legitimate retrieval questions working, but never extract another task
    # from an update-looking message, even when the model misses task_changes.
    if decision.action in ('store_only','store_and_query'):
        return strip_chat_extraction(decision)
    return decision


def words(text):
    return {word for word in re.findall(r'\w+', text.casefold()) if word not in STOP and len(word)>1}


def resolved_deadline(expression, plan, candidate):
    expression=expression.strip().rstrip('.')
    try:
        absolute=datetime.fromisoformat(expression.replace('Z','+00:00'))
        if absolute.tzinfo is not None:
            return absolute.astimezone(timezone.utc)
    except ValueError:
        pass
    if not plan.policy.timezone:
        raise ValueError('unresolved_deadline')
    try:
        zone=timezone.utc if plan.policy.timezone=='UTC' else ZoneInfo(plan.policy.timezone)
    except (ZoneInfoNotFoundError,ValueError):
        raise ValueError('unresolved_deadline') from None
    match=re.fullmatch(r'(?:next )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?: at ([0-2]\d:[0-5]\d))?',expression,re.I)
    if not match:
        raise ValueError('unresolved_deadline')
    local_source=plan.source.occurred_at.astimezone(zone)
    weekday=['monday','tuesday','wednesday','thursday','friday','saturday','sunday'].index(match[1].lower())
    days=(weekday-local_source.weekday())%7
    # Same-day Monday/"next Monday" is linguistically uncertain; do not choose.
    if days==0: raise ValueError('unresolved_deadline')
    if match[2]:
        hour,minute=map(int,match[2].split(':'))
        clock=(hour,minute,0,0)
    elif candidate.due_at:
        due=candidate.due_at.astimezone(zone)
        clock=(due.hour,due.minute,due.second,due.microsecond)
    else:
        raise ValueError('unresolved_deadline')
    day=local_source.date()+timedelta(days=days)
    local=datetime(day.year,day.month,day.day,*clock,tzinfo=zone)
    # Reject nonexistent or ambiguous daylight-saving clock times.
    if local.utcoffset()!=local.replace(fold=1).utcoffset() or local.astimezone(timezone.utc).astimezone(zone)!=local:
        raise ValueError('unresolved_deadline')
    return local.astimezone(timezone.utc)


def validate_proposal(plan: ChatPlan, proposal: TaskChangeProposal):
    """Return the uniquely matching backend candidate and action-specific fields."""
    source=plan.source
    if proposal.clarification_required or plan.truncated or len(plan.candidates)>20 or len(plan.members)>100:
        raise ValueError('clarification_required')
    if proposal.source_message_id!=source.switch_message_id or proposal.evidence!=source.body.strip():
        raise ValueError('unsupported_evidence')
    text=source.body.strip().replace('\u2019',"'")
    if plan.policy.mode=='open_mentions':
        if not source.registered_agent_name: raise ValueError('unsupported_evidence')
        text=without_agent_mention(text,source.registered_agent_name)
    safe_mentions=re.sub(r'<@!?[A-Za-z0-9_-]+>','member',text)
    if ('?' in text or any(c in safe_mentions for c in ('"','`','\n','>'))
        or text.startswith("'") or text.endswith("'") or UNSAFE.search(text)):
        raise ValueError('unsupported_evidence')
    # Discord user mentions contain >; permit only the exact <@id> syntax for handoffs.
    match=PATTERNS[proposal.action].fullmatch(text)
    if not match: raise ValueError('unsupported_evidence')
    subject=match.groupdict().get('object') or match.groupdict().get('clear_object')
    if subject:
        terms=words(subject)
        matches=[c for c in plan.candidates if terms and terms.issubset(words(c.description))]
    else:
        matches=plan.candidates
    if len(matches)!=1 or matches[0].id!=proposal.commitment_id:
        raise ValueError('ambiguous_commitment')
    candidate=matches[0]
    if candidate.tenant_id!=source.scope.tenant_id or candidate.room_id!=source.scope.room_id:
        raise ValueError('invalid_scope')
    if candidate.approval_status=='rejected' or candidate.task_status in ('completed','cancelled'):
        raise ValueError('ineligible_commitment')
    if (plan.policy.mode=='open_mentions' or plan.role!='admin') and candidate.owner_switch_user_id!=source.actor.subject_id:
        raise PermissionError('sender_not_owner')
    fields={}
    if proposal.description is not None: raise ValueError('unexpected_description')
    if proposal.action=='reassign':
        if plan.policy.mode=='open_mentions': raise PermissionError('handoff_requires_admin')
        if plan.role!='admin': raise PermissionError('handoff_requires_admin')
        reference=match['assignee'].casefold()
        members=[m for m in plan.members if (m.label and m.label.casefold()==reference)
                 or (m.external_author_id and reference in (f'<@{m.external_author_id}>',f'<@!{m.external_author_id}>'))]
        targets={m.subject_id for m in members}
        if len(targets)!=1 or proposal.assignee_id not in targets:
            raise ValueError('ambiguous_assignee')
        fields['assignee_id']=proposal.assignee_id
    elif proposal.assignee_id is not None:
        raise ValueError('unexpected_assignee')
    if proposal.action=='reschedule':
        due=resolved_deadline(match['when'],plan,candidate) if match['when'] else None
        if proposal.due_at!=due:
            raise ValueError('unresolved_deadline')
        fields['due_at']=due
    elif proposal.due_at is not None:
        raise ValueError('unexpected_deadline')
    return candidate,fields


def validate_creation(plan, proposal):
    """Only a single explicit first-person promise can create a sender-owned task."""
    source=plan.source
    if (plan.policy.mode!='open_mentions' or proposal.clarification_required or
        proposal.evidence!=source.body.strip() or proposal.source_message_id!=source.switch_message_id
        or proposal.commitment_id is not None or proposal.assignee_id is not None):
        raise ValueError('unsupported_evidence')
    text=without_agent_mention(source.body.replace('\u2019',"'"),source.registered_agent_name or '')
    match=re.fullmatch(r"I(?:'ll| will| commit to) (?P<task>[\w -]{2,160}?)(?: by (?P<when>[^\n;!?]{1,128}))?\.?",text,re.I)
    if not match or re.search(r'\b(if|might|maybe|perhaps|not|never|said|quote|someone)\b',text,re.I):
        raise ValueError('unsupported_evidence')
    description=match['task'].strip().rstrip('.')
    if proposal.description!=description: raise ValueError('unsupported_evidence')
    from types import SimpleNamespace
    due=resolved_deadline(match['when'],plan,SimpleNamespace(due_at=None)) if match['when'] else None
    if proposal.due_at!=due: raise ValueError('unresolved_deadline')
    return description,due


def own_task_query(plan, proposal):
    text=without_agent_mention(plan.source.body,plan.source.registered_agent_name or '')
    return (plan.policy.mode=='open_mentions' and not proposal.clarification_required and
        proposal.source_message_id==plan.source.switch_message_id and proposal.evidence==plan.source.body.strip() and
        proposal.commitment_id is None and proposal.assignee_id is None and proposal.due_at is None and
        proposal.description is None and bool(re.fullmatch(r'(?:what are|show|list) my (?:tasks|commitments)\??\.?',text,re.I)))


def strip_chat_extraction(decision):
    """Chat commitments go through lifecycle transactions, never general extraction."""
    if not decision.commitments: return decision
    data=decision.model_dump()
    data['commitments']=[]
    data['answer']=None  # An AI extraction is never confirmation of a saved task.
    if not (data['decisions'] or data['memories']):
        data['action']='query_only' if decision.action=='store_and_query' else 'none'
    return type(decision).model_validate(data)
