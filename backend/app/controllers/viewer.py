import asyncio
from pathlib import Path
import re

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from app.models.viewer import (
    EnforceSqliteSpacePayload,
    EnvVariablesPayload,
    PruneByTimelinePayload,
)
from app.services.env_service import OPTIONAL_KEYS, read_env_variables, write_env_variables
from app.services.migration_service import migrate_supabase_to_sqlite
from app.services.storage_service import (
    configure_and_enforce_sqlite_space,
    get_storage_status,
    prune_data_by_timeline,
)
from app.services.viewer_service import serve_viewer

router = APIRouter(include_in_schema=False)
VIEWS = Path(__file__).resolve().parents[1] / "views"
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}


def viewer_headers(request: Request):
    headers = dict(HEADERS)
    # Explicit same-host ws/wss source for browsers that don't map CSP 'self'
    # to WebSockets. Validate before inserting a Host-derived value into CSP.
    host = request.url.netloc
    if re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?", host):
        scheme = "wss" if request.url.scheme == "https" else "ws"
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "connect-src 'self';", f"connect-src 'self' {scheme}://{host};"
        )
    return headers


@router.get("/")
def message_viewer(request: Request) -> FileResponse:
    """Public page shell; private data requires authentication through the API."""
    return FileResponse(VIEWS / "messages.html", media_type="text/html", headers=viewer_headers(request))


@router.get("/viewer.css")
def viewer_styles(request: Request) -> FileResponse:
    return FileResponse(VIEWS / "messages.css", media_type="text/css", headers=viewer_headers(request))


@router.get("/viewer.js")
def viewer_script(request: Request) -> FileResponse:
    return FileResponse(VIEWS / "messages.js", media_type="text/javascript", headers=viewer_headers(request))


@router.websocket("/viewer/ws")
async def viewer_socket(websocket: WebSocket):
    await serve_viewer(websocket)


@router.get("/viewer/env")
def get_env_variables(request: Request) -> JSONResponse:
    raise HTTPException(503,'Environment access is disabled; configure secrets locally')


@router.post("/viewer/env")
def update_env_variables(payload: EnvVariablesPayload, request: Request) -> JSONResponse:
    write_env_variables(payload.variables)
    if any(k in payload.variables for k in ("STORAGE_RETENTION_VALUE", "STORAGE_RETENTION_UNIT")):
        worker = getattr(request.app.state, "retention_worker", None)
        if worker:
            asyncio.create_task(worker.process_once())
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(content={"status": "ok"}, headers=headers)


@router.post("/viewer/migrate-db")
async def migrate_database(request: Request) -> JSONResponse:
    res = await migrate_supabase_to_sqlite()
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(content=res, headers=headers)


@router.get("/viewer/storage/status")
def viewer_storage_status(request: Request) -> JSONResponse:
    res = get_storage_status()
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(content=res, headers=headers)


@router.post("/viewer/storage/prune-timeline")
async def viewer_prune_timeline(payload: PruneByTimelinePayload, request: Request) -> JSONResponse:
    write_env_variables(
        {
            "STORAGE_RETENTION_VALUE": str(payload.retention_value),
            "STORAGE_RETENTION_UNIT": payload.retention_unit,
        }
    )
    res = await prune_data_by_timeline(
        engine=payload.engine,
        retention_value=payload.retention_value,
        retention_unit=payload.retention_unit,
        cutoff_iso=payload.cutoff_iso,
    )
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(content=res, headers=headers)


@router.post("/viewer/storage/enforce-space")
def viewer_enforce_sqlite_space(payload: EnforceSqliteSpacePayload, request: Request) -> JSONResponse:
    res = configure_and_enforce_sqlite_space(
        max_space_value=payload.max_space_value,
        max_space_unit=payload.max_space_unit,
        enabled=payload.enabled,
        persist_env=True,
    )
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(content=res, headers=headers)
