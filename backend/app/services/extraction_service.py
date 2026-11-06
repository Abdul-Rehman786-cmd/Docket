"""Build permitted database records from validated extraction, with stable item keys."""

import hashlib
import json

from app.core.message_logging import message_text_for_log, redact_source_identifier
from app.services.commitment_service import summary_text
from fastapi import HTTPException


async def store_ai_memory(settings, *, decision, **kwargs):
    from app.repositories.record_repository import persist_extraction

    if decision.action not in ("store_only", "store_and_query"):
        raise HTTPException(422, "A storage action is required")
    return await persist_extraction(settings, items=storage_items(decision, settings,
        source_metadata=kwargs.get('source_metadata'), source_body=kwargs.get('body')), **kwargs)


def redact_decision(decision, settings):
    data = decision.model_dump()
    for field in ("answer", "search_query"):
        if data[field] is not None:
            data[field] = json.loads(message_text_for_log(data[field], settings))
    for group, fields in (("commitments", ("description", "owner_name")),
                          ("decisions", ("summary", "topic")), ("memories", ("content",)),
                          ("profile_facts", ("value", "evidence"))):
        for item in data[group]:
            for field in fields:
                if item[field] is not None:
                    item[field] = json.loads(message_text_for_log(item[field], settings))
    for proposal in data.get('task_changes',[]):
        proposal['evidence']=json.loads(message_text_for_log(proposal['evidence'],settings,max_length=4000))
        if proposal.get('description') is not None:
            proposal['description']=json.loads(message_text_for_log(proposal['description'],settings,max_length=2000))
        for field in ('source_message_id','assignee_id'):
            if proposal.get(field) is not None:
                proposal[field]=redact_source_identifier(proposal[field],settings) or 'unresolved'
    return type(decision).model_validate(data)


def storage_items(decision, settings, *, source_metadata=None, source_body=None):
    result = {"commitments": [], "decisions": [], "memories": []}
    seen = set()

    def safe(value):
        return json.loads(message_text_for_log(value, settings)) if value is not None else None

    def key(kind, text):
        return kind + "-" + hashlib.sha256(text.casefold().encode()).hexdigest()[:24]

    for item in decision.commitments:
        description, owner = safe(item.description), safe(item.owner_name)
        capture=bool(source_metadata and source_metadata.get('commitment_capture'))
        owner_id=source_metadata.get('switch_author_id') if capture else None
        if capture: owner=safe(source_metadata.get('author_name'))
        effective_due = item.due_at
        if not effective_due and not capture and description and "meeting" in description.lower():
            from app.services.commitment_chat_evidence import parse_natural_datetime
            parsed_dt = parse_natural_datetime(description) or (parse_natural_datetime(source_body) if source_body else None)
            if parsed_dt is not None:
                effective_due = parsed_dt.isoformat()
        identity = key("capture" if capture else "commitment", json.dumps([description, owner_id if capture else owner, effective_due]))
        if identity in seen:
            continue
        seen.add(identity)
        result["commitments"].append(dict(extraction_key=identity, description=description,
                                          owner_name=owner, due_at=effective_due))
        summary = summary_text(description, owner_name=owner, due_at=effective_due)
        if capture:
            summary=summary_text(description,owner_name=owner,owner_id=owner_id,due_at=effective_due)
            summary+='\nAcceptance: explicit sender statement'
            if source_metadata.get('commitment_deadline_text'):
                summary+='\nDeadline wording: '+safe(source_metadata['commitment_deadline_text'])
        result["memories"].append(dict(extraction_key=identity, memory_type="commitment", content=summary))
    for item in decision.decisions:
        summary, topic = safe(item.summary), safe(item.topic)
        identity = key("decision", summary)
        if identity in seen:
            continue
        seen.add(identity)
        result["decisions"].append(dict(extraction_key=identity, summary=summary, topic=topic))
        result["memories"].append(dict(extraction_key=identity, memory_type="decision", content=summary))
    contents = {m["content"].casefold() for m in result["memories"]}
    for item in decision.memories:
        content = safe(item.content)
        if content.casefold() in contents:
            continue
        contents.add(content.casefold())
        result["memories"].append(dict(extraction_key=key("fact", content), memory_type="fact", content=content))
    if decision.profile_facts:
        from app.services.profile_service import profile_items
        result['profile_facts'] = profile_items(decision.profile_facts, settings, source_metadata, source_body)
        for fact in result['profile_facts']:
            result['memories'].append(dict(extraction_key=fact['extraction_key'], memory_type='fact',
                content=f"Reported profile ({safe(source_metadata.get('author_name')) or 'Unknown sender'}): "
                        f"{fact['attribute']}: {fact['value']}"))
    return result


def capture_decision(decision, body, metadata, settings):
    """Normalize an AI extraction using the original first-person statement.

    This creates no records. Existing atomic storage remains the only writer.
    """
    import logging
    logger = logging.getLogger('team_memory')
    from types import SimpleNamespace
    from datetime import datetime
    from app.models.ai import LLMDecision, ExtractedCommitment
    from app.services.commitment_chat_evidence import personal_commitment, resolved_deadline, strip_chat_extraction
    promise=personal_commitment(body,metadata)
    proposals=decision.task_changes
    accepted_proposal=(len(proposals)==1 and proposals[0].action=='create'
        and proposals[0].source_message_id==metadata.get('switch_message_id')
        and proposals[0].evidence==body.strip() and proposals[0].assignee_id is None
        and proposals[0].commitment_id is None)
    is_meeting = bool(promise and promise[0].startswith('Meeting:'))
    logger.info("capture_decision input: promise=%s, is_meeting=%s, commitments=%d, proposals=%d", promise, is_meeting, len(decision.commitments), len(proposals))
    if not promise or not (len(decision.commitments)==1 or accepted_proposal or is_meeting):
        return strip_chat_extraction(decision) if not proposals else LLMDecision(action='none')
    description,wording=promise
    due=None
    if wording:
        occurred_dt = None
        try:
            # Absolute aware instants don't need a message clock; relative dates do.
            when=metadata.get('occurred_at') if metadata.get('timestamp_ms') is not None else None
            if isinstance(when, datetime):
                occurred_dt = when
            elif isinstance(when, str) and when:
                occurred_dt = datetime.fromisoformat(when.replace('Z', '+00:00'))
            plan=SimpleNamespace(source=SimpleNamespace(occurred_at=occurred_dt),
                policy=SimpleNamespace(timezone=metadata.get('capture_timezone')))
            due=resolved_deadline(wording,plan,SimpleNamespace(due_at=None))
        except (ValueError,AttributeError,TypeError,OverflowError) as ex:
            if is_meeting:
                from datetime import timezone
                from app.services.commitment_chat_evidence import parse_natural_datetime
                due = parse_natural_datetime(wording, now=occurred_dt, default_tz=timezone.utc)
            if due is None:
                logger.warning("capture_decision deadline resolution skipped: wording='%s', reason=%s", wording, ex)
    logger.info("capture_decision resolved: description='%s', due_at='%s'", description, due.isoformat() if due else None)
    return LLMDecision(action='store_only',commitments=[ExtractedCommitment(description=description,
        owner_name=metadata.get('author_name'),due_at=due.isoformat() if due else None)],
        decisions=decision.decisions,memories=decision.memories,profile_facts=decision.profile_facts)
