"""Conservative evidence gate, independent of AI confidence or its claimed authority.

Unsupported phrasing is ambiguous. Expand this grammar only with negative tests.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.commitment_chat import ChatPlan, TaskChangeProposal

STOP = {'i','my','the','a','an','task','please','to','by','send','sent','deliver','delivered',
        'finish','finished','complete','completed','prepare','write','submit','submitted'}
UNSAFE = re.compile(r'\b(if|might|maybe|perhaps|could|would|will|not|never|hypothetically|said|asked|quote|but)\b', re.I)
PATTERNS = {
    'complete': re.compile(r"(?:i(?:'ve| have)? (?:sent|finished|completed|delivered|submitted|done) (?:the |my )?(?P<object>[\w -]{2,160})|(?P<bare>done|finished))\.?", re.I),
    'cancel': re.compile(r"cancel (?:my|the) (?P<object>[\w -]{2,160}?)(?: task)?(?:;? (?:it(?:'s| is) no longer needed))?\.?", re.I),
    'reassign': re.compile(r'(?P<assignee>[\w @<>!-]{1,256}?) is taking over (?:the|my) (?P<object>[\w -]{2,160})\.?', re.I),
    'reschedule': re.compile(r'(?:move (?:my|the) (?P<object>[\w -]{2,160}?) deadline to (?P<when>[^\n;!?]{1,128})|(?:clear|remove) (?:my|the) (?P<clear_object>[\w -]{2,160}?) deadline)\.?', re.I),
}


def update_intent(body: str) -> bool:
    """Suppress new task extraction for lifecycle-looking inputs, including uncertainty."""
    return bool(re.search(r"\b(done|finished|completed|sent|delivered|submitted|cancel|taking over|deadline|reschedule)\b"
        r"|\b(?:might|maybe|could|would)\s+(?:finish|complete|send|deliver|cancel|reschedule)\b", body, re.I))


def protect_extraction(decision,body,enabled):
    if not enabled or decision.task_changes or not update_intent(body): return decision
    # Keep legitimate retrieval questions working, but never extract another task
    # from an update-looking message, even when the model misses task_changes.
    if decision.action in ('store_only','store_and_query'):
        querying=decision.action=='store_and_query'
        return type(decision)(action='query_only' if querying else 'none',
                              search_query=decision.search_query if querying else None)
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
    if plan.role!='admin' and candidate.owner_switch_user_id!=source.actor.subject_id:
        raise PermissionError('sender_not_owner')
    fields={}
    if proposal.action=='reassign':
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
