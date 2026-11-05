"""Independent backend worker: committed memory text is the durable job source."""

import asyncio
import logging

from fastapi import HTTPException

from app.core.config import get_settings
from app.core.embedding_model import EmbeddingError, get_embedder, validate_vector
from app.repositories.embedding_repository import claim_embeddings, finish_embedding

logger = logging.getLogger("team_memory")


class EmbeddingWorker:
    def __init__(self):
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def process_once(self, settings):
        jobs = await claim_embeddings(settings)
        # At most eight claimed items; one item failing must not fail its neighbors.
        # Single-item inference is deliberately bounded, sharing one loaded CPU model.
        for job in jobs:
            try:
                vectors = await get_embedder().embed([job["content"]])
                vector = validate_vector(vectors[0])
            except EmbeddingError as error:
                accepted = await finish_embedding(settings, job, error=str(error))
                logger.info("Embedding ref=%s state=%s reason=%s", job["id"],
                            "failed" if accepted else "superseded", str(error))
            else:
                accepted = await finish_embedding(settings, job, vector=vector)
                logger.info("Embedding ref=%s state=%s dimensions=384", job["id"],
                            "completed" if accepted else "superseded")
        return len(jobs)

    async def run(self):
        while True:
            settings = get_settings()
            delay = 2
            db_ready = (
                settings.sqlite_configured
                if settings.effective_database_engine == "sqlite"
                else settings.supabase_configured
            )
            if settings.local_embeddings_enabled and db_ready and settings.switch_tenant_id and settings.allowed_switch_rooms:
                try:
                    await self.process_once(settings)
                except (HTTPException, EmbeddingError):
                    logger.warning("Embedding worker state=retry_wait reason=database_or_model_unavailable")
                    delay = 30
                except Exception:
                    logger.warning("Embedding worker state=retry_wait reason=worker_failed")
                    delay = 30
            await asyncio.sleep(delay)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
