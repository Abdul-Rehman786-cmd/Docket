from contextlib import asynccontextmanager
import logging
from importlib.util import find_spec
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
from app.services.viewer_service import ViewerHub
from app.services.embedding_service import EmbeddingWorker
from app.controllers.documents import router as documents_router
from app.repositories.document_repository import SupabaseDocumentRepository
from app.services.document_worker import DocumentWorker
from app.controllers.document_attachments import router as document_attachments_router
from app.services.document_attachments import DocumentAttachments


@asynccontextmanager
async def lifespan(application):
    configure_logging(get_settings())
    DocumentAttachments.log_configuration(get_settings())
    if get_settings().local_embeddings_enabled:
        missing=[name for name in ("onnxruntime","tokenizers") if find_spec(name) is None]
        if missing:
            logging.getLogger("team_memory").warning(
                "document stage=embedding_setup reason=dependencies_missing packages=%s; start FastAPI using backend/.venv/Scripts/python.exe",
                ",".join(missing))
    application.state.http_clients = HTTPClients()
    application.state.ai_service = AIService()
    application.state.viewer_hub = ViewerHub()
    application.state.message_receipts.on_change = application.state.viewer_hub.publish
    application.state.ai_service.repository.on_change = application.state.viewer_hub.publish
    application.state.embedding_worker = EmbeddingWorker()
    application.state.document_worker = DocumentWorker(SupabaseDocumentRepository())
    application.state.document_attachments = DocumentAttachments()
    with use_http_clients(application.state.http_clients):
        application.state.ai_service.start_recovery()
        application.state.embedding_worker.start()
        if get_settings().document_ingestion_enabled:
            application.state.document_worker.start()
        if get_settings().switch_document_attachments_enabled:
            application.state.document_attachments.start()
    try:
        yield
    finally:
        try:
            await application.state.ai_service.close()
        finally:
            await application.state.embedding_worker.close()
            await application.state.document_worker.close()
            await application.state.document_attachments.close()
            application.state.viewer_hub.close()
            application.state.message_receipts.on_change = None
            await application.state.http_clients.close()
            application.state.http_clients = None


app = FastAPI(title="Team Memory API", version="0.1.0", lifespan=lifespan)
app.state.message_receipts = MessageReceiptRepository()
app.state.ai_service = AIService()
app.state.document_attachments = DocumentAttachments()
app.include_router(records_router)
app.include_router(messages_router)
app.include_router(viewer_router)
app.include_router(ai_router)
app.include_router(documents_router)
app.include_router(document_attachments_router)


@app.middleware("http")
async def prevent_message_caching(request, call_next):
    started = perf_counter()
    with use_http_clients(getattr(request.app.state, "http_clients", None)):
        response = await call_next(request)
    if request.url.path.startswith(("/messages", "/ai", "/api/v1/documents", "/api/v1/document-attachments")):
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
