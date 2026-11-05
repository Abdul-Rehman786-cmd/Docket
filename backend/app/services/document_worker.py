"""Bounded document worker. Chunk rows own durable leases, independently of messages."""

import asyncio
import logging

from app.core.embedding_model import EmbeddingError, RECIPE, get_embedder, validate_vector
from app.core.config import get_settings
from app.core.http_clients import service_client
from app.services.document_authorization import resolve_document_scope

SAFE_ERRORS = {"model_not_installed","model_checksum_failed","dependencies_missing",
               "invalid_vector","invalid_input","input_too_long","inference_failed"}


class DocumentWorker:
    def __init__(self, repository, embedder=None, settings_provider=None):
        self.repository, self.embedder, self.task = repository, embedder or get_embedder(), None
        self.settings_provider = settings_provider or get_settings

    async def process_once(self, scope):
        jobs = await self.repository.call(scope,"claim",{})
        for job in jobs:
            if job["model_recipe"] != RECIPE:
                continue
            error, vector = None, None
            try:
                vector = validate_vector((await self.embedder.embed([job["text"]]))[0])
            except EmbeddingError as failure:
                error = str(failure) if str(failure) in SAFE_ERRORS else "inference_failed"
            await self.repository.call(scope,"finish",dict(id=job["id"],claim_token=job["claim_token"],
                content_hash=job["content_hash"],model_recipe=RECIPE,vector=vector,error=error))
        return len(jobs)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        logging.getLogger("team_memory").info("Document worker state=started")
        while True:
            settings = self.settings_provider()
            if (settings.document_ingestion_enabled and settings.local_embeddings_enabled
                and settings.supabase_configured and settings.switch_tenant_id):
                try:
                    async with service_client("database") as client:
                        scope = await resolve_document_scope(settings,client=client)
                    await self.process_once(scope)
                except Exception:
                    # No raw response, source text, connection information or credentials.
                    logging.getLogger("team_memory").warning("Document worker reason=processing_unavailable")
                    await asyncio.sleep(30)
            await asyncio.sleep(2)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
