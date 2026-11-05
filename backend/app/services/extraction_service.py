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
    return await persist_extraction(settings, items=storage_items(decision, settings, source_metadata=kwargs.get('source_metadata')), **kwargs)


def redact_decision(decision, settings):
    data = decision.model_dump()
    for field in ("answer", "search_query"):
        if data[field] is not None:
            data[field] = json.loads(message_text_for_log(data[field], settings))
    for group, fields in (("commitments", ("description", "owner_name")),
                          ("decisions", ("summary", "topic")), ("memories", ("content",))):
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


def storage_items(decision, settings, *, source_metadata=None):
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
        identity = key("capture" if capture else "commitment", json.dumps([description, owner_id if capture else owner, item.due_at]))
        if identity in seen:
            continue
        seen.add(identity)
        result["commitments"].append(dict(extraction_key=identity, description=description,
                                          owner_name=owner, due_at=item.due_at))
        summary = summary_text(description, owner_name=owner, due_at=item.due_at)
        if capture:
            summary=summary_text(description,owner_name=owner,owner_id=owner_id,due_at=item.due_at)
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
    return result


def capture_decision(decision, body, metadata, settings):
    """Normalize an AI extraction using the original first-person statement.

    This creates no records. Existing atomic storage remains the only writer.
    """
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
    if not promise or not (len(decision.commitments)==1 or accepted_proposal):
        return strip_chat_extraction(decision) if not proposals else LLMDecision(action='none')
    description,wording=promise
    due=None
    if wording:
        try:
            # Absolute aware instants don't need a message clock; relative dates do.
            when=metadata.get('occurred_at') if metadata.get('timestamp_ms') is not None else None
            plan=SimpleNamespace(source=SimpleNamespace(occurred_at=datetime.fromisoformat(when.replace('Z','+00:00')) if when else None),
                policy=SimpleNamespace(timezone=metadata.get('capture_timezone')))
            due=resolved_deadline(wording,plan,SimpleNamespace(due_at=None))
        except (ValueError,AttributeError,TypeError,OverflowError):
            pass  # Preserve wording; never invent an exact deadline/timezone.
    return LLMDecision(action='store_only',commitments=[ExtractedCommitment(description=description,
        owner_name=metadata.get('author_name'),due_at=due.isoformat() if due else None)],
        decisions=decision.decisions,memories=decision.memories)
