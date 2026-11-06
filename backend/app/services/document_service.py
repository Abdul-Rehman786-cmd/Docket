"""Import orchestration and document-only semantic retrieval adapter."""

import asyncio
import hashlib
import json
import logging
from pathlib import PurePosixPath

from fastapi import HTTPException

from app.core.embedding_model import EmbeddingError, RECIPE, get_embedder, validate_vector
from app.models.documents import MAX_UPLOAD_BYTES, DocumentReference, ImportResult
from app.services.document_chunker import CHUNK_RECIPE, digest, local_chunks
from app.services.document_extraction import FORMATS, MAX_TEXT, ExtractionError, extract_text

_import_slots = asyncio.Semaphore(2)


class DocumentService:
    def __init__(self, repository, chunker=local_chunks, embedder=None):
        self.repository, self.chunker = repository, chunker
        self.embedder = embedder or get_embedder()
        self.max_text_chars=MAX_TEXT

    async def import_bytes(self, scope, metadata, raw):
        if _import_slots.locked():
            raise HTTPException(429,"document_import_busy",headers={"Retry-After":"2"})
        async with _import_slots:
            return await self._import_bytes(scope,metadata,raw)

    async def _import_bytes(self, scope, metadata, raw):
        if len(scope.room_ids) != 1:
            raise HTTPException(403,"document_scope_denied")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413,"document_too_large")
        suffix = PurePosixPath(metadata.filename.replace("\\", "/")).suffix.lower()
        try:
            text=await extract_text(raw,metadata.filename)
        except ExtractionError as error:
            code=str(error)
            status=415 if code=="unsupported_format" else 413 if code in {"attachment_too_large","extracted_text_too_large"} else 422
            raise HTTPException(status,"document_"+code) from None
        if len(text)>self.max_text_chars:
            raise HTTPException(413,"document_extracted_text_too_large")
        if any(ord(c) < 32 for value in (metadata.title,metadata.filename,metadata.source_key) for c in value):
            raise HTTPException(422,"document_invalid_metadata")
        try:
            chunks = await self.chunker(text)
        except EmbeddingError as error:
            reason=str(error) if str(error) in {"model_not_installed","model_checksum_failed","dependencies_missing"} else "tokenizer_failure"
            logging.getLogger("team_memory.documents").warning("document stage=tokenizer_failed reason=%s",reason)
            raise HTTPException(503,"document_tokenizer_unavailable") from None
        except ValueError:
            raise HTTPException(422,"document_chunking_limit") from None
        data = dict(import_id=str(metadata.import_id),source_key=metadata.source_key,
                    filename=metadata.filename,title=metadata.title,expected_version=metadata.expected_version,
                    source_text=text,content_hash=digest(text),recipe=CHUNK_RECIPE+(":extracted-text:v1:"+FORMATS[suffix] if suffix not in (".md",".txt") else ""),model_recipe=RECIPE,
                    room_id=scope.room_ids[0],actor=scope.actor,
                    source_type=FORMATS[suffix])
        data["request_hash"] = hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
        data["chunks"] = chunks
        return ImportResult.model_validate(await self.repository.call(scope,"import",data))

    async def search(self, scope, request):
        try:
            vector = validate_vector((await self.embedder.embed([request.query]))[0])
        except EmbeddingError:
            raise HTTPException(503,"document_query_embedding_unavailable") from None
        rows = await self.repository.call(scope,"search",dict(vector=vector,model_recipe=RECIPE,
                    document_id=str(request.document_id) if request.document_id else None,limit=request.limit))
        return [DocumentReference(**row, citation=f"document:{row['document_id']}:{row['version_id']}:{row['chunk_id']}")
                for row in rows]
