from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.controllers.messages import router as messages_router
from app.controllers.records import router as records_router
from app.controllers.viewer import router as viewer_router
from app.repositories.message_receipts import MessageReceiptRepository
from app.controllers.ai import router as ai_router
from app.services.ai_service import AIService


@asynccontextmanager
async def lifespan(application):
    application.state.ai_service = AIService()
    try:
        yield
    finally:
        await application.state.ai_service.close()


app = FastAPI(title="Team Memory API", version="0.1.0", lifespan=lifespan)
app.state.message_receipts = MessageReceiptRepository()
app.state.ai_service = AIService()
app.include_router(records_router)
app.include_router(messages_router)
app.include_router(viewer_router)
app.include_router(ai_router)


@app.middleware("http")
async def prevent_message_caching(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/messages", "/ai")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    # FastAPI's default 422 response echoes invalid input, which may contain chat secrets.
    return JSONResponse(status_code=422, content={"detail": "Invalid request payload"})


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
