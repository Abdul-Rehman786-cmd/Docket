"""Build permitted database records from validated extraction, with stable item keys."""

import hashlib
import json

from app.core.message_logging import message_text_for_log
from fastapi import HTTPException


async def store_ai_memory(settings, *, decision, **kwargs):
    from app.repositories.record_repository import persist_extraction

    if decision.action not in ("store_only", "store_and_query"):
        raise HTTPException(422, "A storage action is required")
    return await persist_extraction(settings, items=storage_items(decision, settings), **kwargs)


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
    return type(decision).model_validate(data)


def storage_items(decision, settings):
    result = {"commitments": [], "decisions": [], "memories": []}
    seen = set()

    def safe(value):
        return json.loads(message_text_for_log(value, settings)) if value is not None else None

    def key(kind, text):
        return kind + "-" + hashlib.sha256(text.casefold().encode()).hexdigest()[:24]

    for item in decision.commitments:
        description, owner = safe(item.description), safe(item.owner_name)
        identity = key("commitment", json.dumps([description, owner, item.due_at]))
        if identity in seen:
            continue
        seen.add(identity)
        result["commitments"].append(dict(extraction_key=identity, description=description,
                                          owner_name=owner, due_at=item.due_at))
        summary = f"{owner}: {description}" if owner else description
        if item.due_at:
            summary += f"; due {item.due_at}"
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
