"""Conservative source evidence validation; identity never comes from the model."""

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.message_logging import message_text_for_log, redact_source_identifier
from app.core.feature_logging import feature_event
from app.models.profiles import ExtractedProfileFact

# Whole-message grammar deliberately excludes quotations, third-party claims,
# questions, hypothetical statements and instructions embedded around a quote.
PATTERNS = {
    "responsibility": r"I maintain (?P<value>[\w /+-]{2,256})",
    "skill": r"I (?:specialize in|am skilled in) (?P<value>[\w /+#.-]{2,256})",
    "preference": r"I prefer (?P<value>[\w /+-]{2,256})",
    "timezone": r"My timezone is (?P<value>[A-Za-z_+/0-9:-]{1,128})",
}
CORRECTIONS = {
    "responsibility": r"My responsibility is now (?P<value>[\w /+-]{2,256})",
    "skill": r"My specialty is now (?P<value>[\w /+#.-]{2,256})",
    "preference": r"My preference is now (?P<value>[\w /+-]{2,256})",
    "timezone": r"My timezone is now (?P<value>[A-Za-z_+/0-9:-]{1,128})",
}
UNSAFE = re.compile(r"\b(if|might|maybe|perhaps|would|could|not|never|quote|said|hypothetically)\b", re.I)
SINGLE_VALUED = {'timezone'}


def personal_profile_question(body):
    """Routing from the original message, independent of model-selected keywords."""
    text = re.sub(r"^(?:@[\w.-]+|<@!?[A-Za-z0-9_-]+>)[,:]?\s+", '', (body or '').strip()).casefold()
    if not re.search(r'\b(my|mine|me|i)\b', text):
        return None
    asking = (bool(re.search(r'\?', text)) or bool(re.match(
        r'^(what|who|which|how|do|did|can|could|would|tell|show|list|remind)\b', text))
        or bool(re.fullmatch(r'my (?:profile|preferences?|preferred name|prefered name|nickname|timezone|time zone|skills?|responsibilities)\.?', text)))
    if not asking:
        return None
    if re.search(r'\b(preferred name|prefered name|nickname|alias)\b|\bcall me\b|\baddress me\b|\bgo by\b|\bbe called\b', text):
        return 'preferred_name'
    if re.search(r'\bwho am i\b|\b(?:know|remember) me\b', text):
        return 'profile'
    if re.search(r'\bname\b', text) and not re.search(r'\b(task|commitment|project)\b', text):
        return 'preferred_name'
    for attribute, pattern in (
        ('profile', r'\bprofile\b'), ('preference', r'\b(preferences?|prefer)\b'),
        ('timezone', r'\b(timezone|time zone)\b'), ('skill', r'\bskills?\b'),
        ('responsibility', r'\bresponsibilit(?:y|ies)\b'),
    ):
        if re.search(pattern, text):
            return attribute
    return None


def confirmation_key(settings, room_id, thread_id, sender):
    if not isinstance(sender, str) or redact_source_identifier(sender, settings) != sender:
        return None
    identity = json.dumps([str(settings.switch_tenant_id), room_id, thread_id, sender])
    return 'sender-v1-' + hashlib.sha256(identity.encode()).hexdigest()


async def answer_personal_profile(settings, metadata, kind):
    """Answer from this sender's evidence only; shared AI conversation is not identity."""
    if not settings.person_profiles_enabled:
        return 'Person profiles are disabled.', set()
    meta = metadata or {}
    sender = meta.get('switch_author_id')
    if (meta.get('origin') != 'discord' or not meta.get('durable') or not isinstance(sender, str)
            or redact_source_identifier(sender, settings) != sender or sender in settings.switch_ai_senders):
        return 'I cannot identify your profile from this request. Ask from your own message in an enabled room.', set()
    from app.repositories.profile_repository import sender_profile_reports
    reports = await sender_profile_reports(settings, switch_room_id=meta.get('room_id'), switch_author_id=sender)
    feature_event('retrieval', 'succeeded' if reports else 'no_change', reference=sender, reason='read_detail', count=len(reports))
    if len(reports) > 200:
        raise HTTPException(503, 'Profile evidence exceeds retrieval limit')
    selected = {}
    for report in reports:
        if report['status'] != 'active' or (kind != 'profile' and report['attribute'] != ('preference' if kind == 'preferred_name' else kind)):
            continue
        value = report['value']
        if kind == 'preferred_name':
            name = re.fullmatch(r'(?:to be called|being called|to be addressed as) (.+)', value, re.I)
            if not name or re.search(r'\b(my|your|preferred|prefered|everytime|whenever)\b|every time', name[1], re.I):
                continue
            value = name[1]
        key = (report['attribute'], value.casefold())
        if key not in selected:
            selected[key] = (value, str(UUID(report['message_id'])))
    if not selected:
        if kind == 'preferred_name':
            return 'I do not have a preferred name saved for you in this room. Say: I prefer to be called <name>.', set()
        return 'I do not have a saved ' + kind + ' report for you in this room.', set()
    if len(selected) > 10:
        return 'You have several saved profile reports. Ask about one specific preference, skill, responsibility or timezone.', set()
    values = list(selected.values())
    sources = {source for _, source in values}
    if kind == 'preferred_name' and len(values) == 1:
        name, source = values[0]
        return f'Your stored preferred name is {name}. [source:{source}]', sources
    if (kind == 'preferred_name' or kind in SINGLE_VALUED) and len(values) > 1:
        title = 'I have conflicting saved reports for you; I cannot choose one:'
    else:
        title = 'Your saved profile reports:'
    lines = [f'- {attribute}: {value} [source:{source}]' for (attribute, _), (value, source) in selected.items()]
    return title + '\n' + '\n'.join(lines), sources


def observed_time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(422, "Invalid profile source timestamp") from None
    if parsed.tzinfo is None:
        raise HTTPException(422, "Profile source requires a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def profile_items(facts, settings, metadata, body):
    if not facts:
        return []
    if not settings.person_profiles_enabled:
        raise HTTPException(503, "Person profiles are disabled")
    meta = metadata or {}
    sender = meta.get("switch_author_id")
    if (meta.get("origin") != "discord" or not meta.get("durable") or not isinstance(sender, str)
            or not 1 <= len(sender) <= 512 or re.search(r"\s", sender)
            or redact_source_identifier(sender, settings) != sender):
        raise HTTPException(422, "Profile source identity is unavailable")
    if not isinstance(body, str) or not body.strip():
        raise HTTPException(422, "Profile source text is unavailable")
    safe_body = json.loads(message_text_for_log(body, settings, max_length=64000))
    text = re.sub(r"^(?:@[\w.-]+|<@!?[A-Za-z0-9_-]+>)[,:]?\s+", "", safe_body.strip())
    if UNSAFE.search(text):
        raise HTTPException(422, "Unsupported profile evidence")
    result = {}
    for raw in facts:
        fact = raw if isinstance(raw, ExtractedProfileFact) else ExtractedProfileFact.model_validate(
            {k: v for k, v in raw.items() if k != 'extraction_key'})
        pattern = (CORRECTIONS if fact.correction else PATTERNS)[fact.attribute]
        match = re.fullmatch(pattern + r"\.?", text, re.I)
        if (not match or fact.evidence != safe_body.strip()
                or fact.value != match["value"].strip().rstrip(".")):
            raise HTTPException(422, "Unsupported profile evidence")
        value = fact.value
        if json.loads(message_text_for_log(value, settings)) != value or "[REDACTED" in safe_body:
            raise HTTPException(422, "Unsupported profile evidence")
        key = "profile-" + hashlib.sha256(json.dumps([fact.attribute, value.casefold()]).encode()).hexdigest()[:24]
        result[key] = dict(extraction_key=key, **fact.model_dump())
    return list(result.values())


def filter_profile_decision(decision, settings, metadata, body):
    """Disabled/untrusted proposals cannot alter existing extraction behavior."""
    if not decision.profile_facts:
        return decision
    accepted = []
    if settings.person_profiles_enabled:
        try:
            profile_items(decision.profile_facts, settings, metadata, body)
            accepted = decision.profile_facts
        except HTTPException:
            pass
    data = decision.model_dump()
    data["profile_facts"] = [item.model_dump() for item in accepted]
    if not (data["commitments"] or data["decisions"] or data["memories"] or accepted):
        data["action"] = "query_only" if decision.action == "store_and_query" else "none"
    return type(decision).model_validate(data)


PROFILE_PROMPT = (
    '\nOptional profile_facts: [{"attribute":"responsibility|skill|preference|timezone",'
    '"value":"exact stated value","evidence":"EXACT full current message","correction":false}]. '
    'Only explicit first-person work statements: I maintain X; I specialize in X; I am skilled in X; '
    'I prefer X; My timezone is X. Explicit corrections: My responsibility/specialty/preference/timezone '
    'is now X (correction=true). Otherwise leave profile_facts empty. Never infer identity, traits or '
    'third-party facts. No quotes, hypotheticals or questions. Profile facts count as storage items; '
    'use store_only or store_and_query and do not duplicate them in memories. '
    'Claims are reported statements, not verified personal attributes.\n'
)


async def read_profile(settings, scope, actor, **options):
    from app.repositories.profile_repository import read_profiles
    from app.models.profiles import ProfileDetail, ProfilePage
    result = await read_profiles(settings, scope, actor, **options)
    try:
        parsed = (ProfileDetail if 'profile' in result else ProfilePage).model_validate(result)
        records = [parsed.profile, *parsed.claims, *parsed.evidence] if isinstance(parsed, ProfileDetail) else parsed.items
        if any(r.tenant_id != scope.tenant_id or r.room_id != scope.room_id for r in records):
            raise ValueError('Invalid profile scope')
        if isinstance(parsed, ProfileDetail):
            if parsed.profile.id != options.get('profile_id') or any(c.profile_id != parsed.profile.id for c in parsed.claims):
                raise ValueError('Invalid profile result')
            claim_ids = {c.id for c in parsed.claims}
            if any(e.claim_id not in claim_ids for e in parsed.evidence):
                raise ValueError('Invalid profile evidence')
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(503, 'Invalid profile storage result') from None
    if 'claims' in result:
        active = {}
        for claim in result['claims']:
            if claim['status'] == 'active':
                active.setdefault(claim['attribute'], set()).add(claim['value'].casefold())
        result['conflicting_attributes'] = sorted(k for k, values in active.items() if k in SINGLE_VALUED and len(values) > 1)
        result['verification'] = 'reported_statements'
    return result


def redact_profile_answer(text, settings, source_ids):
    """Preserve only citations validated against retrieved profile sources."""
    citations = []
    def replace(match):
        source = match[1]
        if source not in source_ids or redact_source_identifier(source, settings) != source:
            return ''
        label = f'[profile source {len(citations) + 1}]'
        citations.append((label, f'[source:{source}]'))
        return label
    masked = re.sub(r'\[source:([^\]]+)\]', replace, text or '')
    safe = json.loads(message_text_for_log(masked, settings))
    for label, citation in citations:
        safe = safe.replace(label, citation)
    return safe


async def enrich_retrieval(settings, records, switch_room_id=None):
    if not settings.person_profiles_enabled:
        return records
    from app.repositories.profile_repository import retrieval_context
    rows = await retrieval_context(settings, records, switch_room_id=switch_room_id)
    if not rows:
        return records
    # Oversized context must not quietly hide a contradictory or superseding report.
    if len(rows) > 200:
        raise HTTPException(503, 'Profile evidence exceeds retrieval limit')
    active = {}
    for row in rows:
        if row['status'] == 'active':
            active.setdefault((row['profile_id'], row['attribute']), set()).add(row['value'].casefold())
    for row in rows:
        row['conflicting'] = row['attribute'] in SINGLE_VALUED and len(active.get((row['profile_id'], row['attribute']), set())) > 1
        row['verification'] = 'reported_statement'
    inactive_memories = {r['memory_id'] for r in rows if r['status'] != 'active'}
    records['memories'] = [m for m in records.get('memories', []) if m['id'] not in inactive_memories]
    states = {}
    for row in rows:
        states.setdefault(row['message_id'], []).append(row['status'])
    for source in records.get('messages', []):
        if source['id'] in states:
            source['profile_claim_statuses'] = states[source['id']]
    records['profile_reports'] = rows
    return records
