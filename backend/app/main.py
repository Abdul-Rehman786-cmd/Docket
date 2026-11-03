from fastapi import FastAPI
from app.controllers.records import router as records_router

app = FastAPI(title="Team Memory API", version="0.1.0")
app.include_router(records_router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
