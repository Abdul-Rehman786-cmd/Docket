from contextlib import asynccontextmanager
import logging
import os
from time import perf_counter

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.controllers.messages import router as messages_router
from app.controllers.records import router as records_router
from app.controllers.viewer import router as viewer_router
from app.repositories.message_receipts import MessageReceiptRepository
from app.controllers.ai import router as ai_router
from app.services.ai_service import AIService
from app.core.http_clients import HTTPClients, use_http_clients
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.repositories.record_repository import ensure_tenant_and_rooms_provisioned
from app.services.viewer_service import ViewerHub
from app.services.embedding_service import EmbeddingWorker
from app.services.storage_service import StorageRetentionWorker


@asynccontextmanager
async def lifespan(application):
    configure_logging(get_settings())
    application.state.http_clients = HTTPClients()
    application.state.ai_service = AIService()
    application.state.viewer_hub = ViewerHub()
    application.state.message_receipts.on_change = application.state.viewer_hub.publish
    application.state.ai_service.repository.on_change = application.state.viewer_hub.publish
    application.state.embedding_worker = EmbeddingWorker()
    application.state.retention_worker = StorageRetentionWorker(
        on_change=application.state.viewer_hub.publish
    )
    with use_http_clients(application.state.http_clients):
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                await ensure_tenant_and_rooms_provisioned()
            except Exception as exc:
                logging.getLogger("uvicorn.error").warning("Startup auto-provision warning: %s", exc)
        application.state.ai_service.start_recovery()
        application.state.embedding_worker.start()
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            application.state.retention_worker.start()
    try:
        yield
    finally:
        try:
            await application.state.ai_service.close()
        finally:
            await application.state.retention_worker.close()
            await application.state.embedding_worker.close()
            application.state.viewer_hub.close()
            application.state.message_receipts.on_change = None
            await application.state.http_clients.close()
            application.state.http_clients = None


app = FastAPI(title="Team Memory API", version="0.1.0", lifespan=lifespan)
app.state.message_receipts = MessageReceiptRepository()
app.state.ai_service = AIService()
app.include_router(records_router)
app.include_router(messages_router)
app.include_router(viewer_router)
app.include_router(ai_router)


@app.middleware("http")
async def prevent_message_caching(request, call_next):
    started = perf_counter()
    with use_http_clients(getattr(request.app.state, "http_clients", None)):
        response = await call_next(request)
    if request.url.path.startswith(("/messages", "/ai")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    route = request.scope.get("route")
    logging.getLogger("team_memory").debug(
        "route=%s status=%d duration_ms=%.2f",
        getattr(route, "path", "unmatched"), response.status_code,
        (perf_counter() - started) * 1000,
    )
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    # FastAPI's default 422 response echoes invalid input, which may contain chat secrets.
    logging.getLogger("team_memory").debug("Request validation rejected")
    return JSONResponse(status_code=422, content={"detail": "Invalid request payload"})


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
