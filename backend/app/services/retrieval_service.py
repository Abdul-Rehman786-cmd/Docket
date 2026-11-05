"""Semantic results carry provenance; lexical fallback is explicitly labelled."""

import json
import logging
from fastapi import HTTPException

from app.core.embedding_model import EmbeddingError, get_embedder
from app.core.message_logging import message_text_for_log
from app.repositories.embedding_repository import search_embeddings, source_context
from app.repositories.record_repository import query_team_memory as keyword_query
from app.core.feature_logging import feature_event
from app.services.profile_service import enrich_retrieval


async def query_team_memory(settings, *, search_query=None, switch_room_id=None):
    reason = "disabled"
    if settings.local_embeddings_enabled and search_query:
        try:
            safe_query = json.loads(message_text_for_log(search_query, settings, max_length=4000))
            vectors = await get_embedder().embed([safe_query])
            rows = await search_embeddings(settings, vectors[0], switch_room_id=switch_room_id)
            if rows:
                context = await source_context(settings, rows)
                feature_event('retrieval','succeeded',reason='semantic',count=len(rows))
                return await enrich_retrieval(settings,
                    {**context, "memories": rows, "retrieval": [{"mode": "semantic", "source_field": "message_id"}]}, switch_room_id)
            reason = "no_vector_matches"
        except EmbeddingError as error:
            reason = str(error)
        except HTTPException as error:
            if error.status_code in (401,403):
                raise
            reason = "vector_search_unavailable"
    logging.getLogger("team_memory").info("Retrieval mode=keyword_fallback reason=%s", reason)
    records = await keyword_query(settings, search_query=search_query, switch_room_id=switch_room_id)
    feature_event('retrieval','succeeded',reason='keyword_fallback',count=sum(len(rows) for rows in records.values() if isinstance(rows,list)))
    records["retrieval"] = [{"mode": "keyword_fallback", "reason": reason}]
    return await enrich_retrieval(settings, records, switch_room_id)
