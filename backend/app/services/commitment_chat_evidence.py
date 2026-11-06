"""Conservative evidence gate, independent of AI confidence or its claimed authority.

Unsupported phrasing is ambiguous. Expand this grammar only with negative tests.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger('team_memory')

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


TZ_ABBREVIATIONS = {
    'pkt': timezone(timedelta(hours=5)),
    'utc': timezone.utc,
    'gmt': timezone.utc,
    'est': timezone(timedelta(hours=-5)),
    'edt': timezone(timedelta(hours=-4)),
    'cst': timezone(timedelta(hours=-6)),
    'cdt': timezone(timedelta(hours=-5)),
    'pst': timezone(timedelta(hours=-8)),
    'pdt': timezone(timedelta(hours=-7)),
    'ist': timezone(timedelta(hours=5, minutes=30)),
    'bst': timezone(timedelta(hours=1)),
    'cet': timezone(timedelta(hours=1)),
    'cest': timezone(timedelta(hours=2)),
    'jst': timezone(timedelta(hours=9)),
    'kst': timezone(timedelta(hours=9)),
}

MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'september': 9, 'oct': 10, 'october': 10,
    'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}


def parse_natural_datetime(expr: str, now: datetime | None = None, default_tz = timezone.utc) -> datetime | None:
    if not expr or not expr.strip():
        return None
    now = now or datetime.now(default_tz)
    s = expr.strip().lower()

    try:
        dt = datetime.fromisoformat(expr.strip().replace('Z', '+00:00'))
        if dt.tzinfo:
            return dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=default_tz).astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass

    tz = default_tz
    for tz_token, tz_obj in TZ_ABBREVIATIONS.items():
        pattern = r'\b' + re.escape(tz_token) + r'\b'
        if re.search(pattern, s):
            tz = tz_obj
            s = re.sub(pattern, '', s).strip()
            break

    year = now.year
    yr_match = re.search(r'\b(202\d|203\d)\b', s)
    if yr_match:
        year = int(yr_match.group(1))
        s = re.sub(r'\b' + yr_match.group(1) + r'\b', '', s).strip()

    hour = 9
    minute = 0
    t_match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', s)
    if t_match:
        h = int(t_match.group(1))
        minute = int(t_match.group(2) or 0)
        meridiem = t_match.group(3)
        if meridiem == 'pm' and h < 12:
            h += 12
        elif meridiem == 'am' and h == 12:
            h = 0
        hour = h
        s = s[:t_match.start()] + ' ' + s[t_match.end():]
    else:
        t24_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', s)
        if t24_match:
            hour = int(t24_match.group(1))
            minute = int(t24_match.group(2))
            s = s[:t24_match.start()] + ' ' + s[t24_match.end():]

    month = now.month
    day = now.day

    m_found = None
    for m_name, m_num in sorted(MONTHS.items(), key=lambda x: -len(x[0])):
        pattern = r'\b' + m_name + r'\b'
        if re.search(pattern, s):
            m_found = m_num
            s = re.sub(pattern, '', s).strip()
            break

    d_match = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', s)
    if d_match:
        day = int(d_match.group(1))
    elif 'today' in s:
        day = now.day
        month = now.month
    elif 'tomorrow' in s:
        tmrw = now + timedelta(days=1)
        day = tmrw.day
        month = tmrw.month
        year = tmrw.year

    if m_found:
        month = m_found

    try:
        parsed = datetime(year, month, day, hour, minute, 0, tzinfo=tz)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def personal_commitment(body, metadata):
    """Use Switch addressing, not member verification, for simple capture."""
    if not metadata or not metadata.get('addressed'): return None
    text=' '.join(body.strip().replace('\u2019',"'").split())
    if any(c in text for c in ('?', '"', '`')) or text.startswith('>'): return None
    text,count=re.subn(r'^(?:@[\w.-]+|<@!?[A-Za-z0-9_-]+>)[,:]?\s+', '', text, count=1)
    if not count: return None

    # Pattern 1: title first: (set/schedule a meeting) (for/with [the] title/topic [of] <topic>) (<when>)
    m1 = re.search(
        r'(?:(?:set|schedule|arrange|create)\s+(?:a\s+)?meeting|meeting)\s+'
        r'(?:with|for)\s+(?:the\s+)?(?:title|topic)(?:\s+of)?\s+(?P<topic>.+?)'
        r'(?:\s+(?:at|on|for)\s+|\s+(?=(?:202\d|203\d|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|today|tomorrow)\b|\d{1,2}(?::\d{2})?\s*(?:am|pm))))'
        r'(?P<when>.+)',
        text, re.I
    )
    if m1 and not re.search(r'\b(if|might|maybe|perhaps|not|never|hypothetically|said|quote)\b', text, re.I):
        topic = m1.group('topic').strip().rstrip('.')
        when = m1.group('when').strip()
        logger.info("Personal commitment matched (pattern 1): topic='%s', when='%s'", topic, when)
        return f"Meeting: {topic}", when

    # Pattern 2: when first, title last: (set meeting for/at/on <when>) (with/for [the] title/topic [of] / about / regarding / titled <topic>)
    m2 = re.search(
        r'(?:(?:set|schedule|arrange|create)\s+(?:a\s+)?meeting|meeting)\s+'
        r'(?:for|at|on)\s+(?P<when>.+?)\s+'
        r'(?:(?:with|for)\s+(?:the\s+)?(?:title|topic)(?:\s+of)?|about|regarding|titled)\s+(?P<topic>.+)',
        text, re.I
    )
    if m2 and not re.search(r'\b(if|might|maybe|perhaps|not|never|hypothetically|said|quote)\b', text, re.I):
        topic = m2.group('topic').strip().rstrip('.')
        when = m2.group('when').strip()
        logger.info("Personal commitment matched (pattern 2): topic='%s', when='%s'", topic, when)
        return f"Meeting: {topic}", when

    # Pattern 3: title first with about/regarding/titled <topic> at/on/for <when>
    m3 = re.search(
        r'(?:(?:set|schedule|arrange|create)\s+(?:a\s+)?meeting|meeting)\s+'
        r'(?:about|regarding|titled)\s+(?P<topic>.+?)\s+'
        r'(?:at|on|for)\s+(?P<when>.+)',
        text, re.I
    )
    if m3 and not re.search(r'\b(if|might|maybe|perhaps|not|never|hypothetically|said|quote)\b', text, re.I):
        topic = m3.group('topic').strip().rstrip('.')
        when = m3.group('when').strip()
        logger.info("Personal commitment matched (pattern 3): topic='%s', when='%s'", topic, when)
        return f"Meeting: {topic}", when

    # Pattern 4: general meeting with <topic> at/on/for <when> or meeting at/on/for <when>
    m4 = re.search(
        r'(?:(?:we have\s+(?:a\s+)?|set\s+(?:a\s+)?|schedule\s+(?:a\s+)?)?meeting)\s+'
        r'(?:(?:with|for|about|to)\s+(?P<topic>[\w ,/-]{2,400}?)\s+)?'
        r'(?:at|on|for)\s+(?P<when>[^\n;!?]{1,128})',
        text, re.I
    )
    if m4 and not re.search(r'\b(if|might|maybe|perhaps|not|never|hypothetically|said|quote)\b', text, re.I):
        topic = m4.group('topic').strip().rstrip('.') if m4.group('topic') else 'Team sync'
        when = m4.group('when').strip()
        logger.info("Personal commitment matched (pattern 4): topic='%s', when='%s'", topic, when)
        return f"Meeting: {topic}", when

    # Pattern 5: bare meeting <when> without preposition (e.g. "set a meeting 3:40 pm PKT")
    m5 = re.search(
        r'(?:(?:we have\s+(?:a\s+)?|(?:set|schedule|arrange|create)\s+(?:a\s+)?)?meeting)\s+'
        r'(?P<when>(?:202\d|203\d|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|today|tomorrow)\b|\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:[0-5]\d)[^\n;!?]{0,120})',
        text, re.I
    )
    if m5 and not re.search(r'\b(if|might|maybe|perhaps|not|never|hypothetically|said|quote)\b', text, re.I):
        when = m5.group('when').strip()
        logger.info("Personal commitment matched (pattern 5): topic='Team sync', when='%s'", when)
        return "Meeting: Team sync", when

    match=re.fullmatch(r"I(?:'ll| will| commit to) (?P<task>[\w ,/-]{2,500}?)(?: by (?P<when>[^\n;!?]{1,128}))?\.?",text,re.I)
    if match and not re.search(r'\b(if|might|maybe|perhaps|not|never|hypothetically|said|quote)\b',text,re.I):
        task = match['task'].strip().rstrip('.')
        when = match['when'].rstrip('.') if match['when'] else None
        logger.info("Personal commitment matched (task): task='%s', when='%s'", task, when)
        return task, when

    return None


def protect_extraction(decision,body,enabled):
    if not enabled or decision.task_changes or not update_intent(body): return decision
    if any(re.search(r'\bmeeting\b', c.description, re.I) for c in decision.commitments):
        return decision
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
    if not plan or not getattr(plan, 'policy', None) or not getattr(plan.policy, 'timezone', None):
        raise ValueError('unresolved_deadline')
    if not getattr(plan, 'source', None) or not getattr(plan.source, 'occurred_at', None) or plan.source.occurred_at is None:
        raise ValueError('unresolved_deadline')
    try:
        zone=timezone.utc if plan.policy.timezone=='UTC' else ZoneInfo(plan.policy.timezone)
    except (ZoneInfoNotFoundError,ValueError):
        raise ValueError('unresolved_deadline') from None
    match=re.fullmatch(r'(?:next )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?: at ([0-2]\d:[0-5]\d))?',expression,re.I)
    if match:
        local_source=plan.source.occurred_at.astimezone(zone)
        weekday=['monday','tuesday','wednesday','thursday','friday','saturday','sunday'].index(match[1].lower())
        days=(weekday-local_source.weekday())%7
        # Same-day Monday/"next Monday" is linguistically uncertain; do not choose.
        if days==0: raise ValueError('unresolved_deadline')
        if match[2]:
            hour,minute=map(int,match[2].split(':'))
            clock=(hour,minute,0,0)
        elif candidate and getattr(candidate, 'due_at', None):
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
    # Check natural date/time (e.g. "Nov 6 3:10 PM pkt 2026")
    local_source=plan.source.occurred_at.astimezone(zone)
    natural = parse_natural_datetime(expression, now=local_source, default_tz=zone)
    if natural is not None:
        return natural
    raise ValueError('unresolved_deadline')


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
    meetings = [c for c in decision.commitments if re.search(r'\bmeeting\b', c.description, re.I)]
    data=decision.model_dump()
    data['commitments']=[c.model_dump() for c in meetings]
    if not meetings:
        data['answer']=None  # An AI extraction is never confirmation of a saved task.
        if not (data['decisions'] or data['memories'] or data.get('profile_facts')):
            data['action']='query_only' if decision.action=='store_and_query' else 'none'
    return type(decision).model_validate(data)
