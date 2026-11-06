from pathlib import Path
import re

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from app.models.viewer import (
    EnforceSqliteSpacePayload,
    EnvVariablesPayload,
    PruneByTimelinePayload,
    ReminderConfigPayload,
)
from app.services.env_service import read_env_variables, save_viewer_settings, write_env_variables
from app.core.security import require_admin_token
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


@router.get("/viewer/env", dependencies=[Depends(require_admin_token)])
def credential_settings(request: Request) -> JSONResponse:
    return JSONResponse(content=read_env_variables(), headers=viewer_headers(request))


@router.post("/viewer/env", dependencies=[Depends(require_admin_token)])
def update_credential_settings(payload: EnvVariablesPayload, request: Request) -> JSONResponse:
    save_viewer_settings(payload.variables)
    return JSONResponse(content={"status": "ok"}, headers=viewer_headers(request))


@router.get("/viewer/reminders/status", dependencies=[Depends(require_admin_token)])
def viewer_reminders_status(request: Request) -> JSONResponse:
    from app.core.config import get_settings
    settings = get_settings()
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(
        content={
            "enabled": settings.reminders_enabled,
            "delivery_enabled": settings.reminder_delivery_enabled,
            "lead_time": settings.reminder_lead_time,
            "lead_unit": settings.reminder_lead_unit,
            "lead_minutes": settings.reminder_lead_minutes,
        },
        headers=headers,
    )


@router.post("/viewer/reminders/configure", dependencies=[Depends(require_admin_token)])
async def viewer_reminders_configure(payload: ReminderConfigPayload, request: Request) -> JSONResponse:
    from app.services.reminder_settings_service import configure_reminders
    res = await configure_reminders(
        enabled=payload.enabled,
        lead_time=payload.lead_time,
        lead_unit=payload.lead_unit,
        lead_minutes=payload.lead_minutes,
        delivery_enabled=payload.delivery_enabled,
    )
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(content=res, headers=headers)


@router.post("/viewer/migrate-db", dependencies=[Depends(require_admin_token)])
async def migrate_database(request: Request) -> JSONResponse:
    res = await migrate_supabase_to_sqlite()
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(content=res, headers=headers)


@router.get("/viewer/storage/status", dependencies=[Depends(require_admin_token)])
def viewer_storage_status(request: Request) -> JSONResponse:
    res = get_storage_status()
    headers = viewer_headers(request)
    headers["Cache-Control"] = "no-store, private"
    return JSONResponse(content=res, headers=headers)


@router.post("/viewer/storage/prune-timeline", dependencies=[Depends(require_admin_token)])
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


@router.post("/viewer/storage/enforce-space", dependencies=[Depends(require_admin_token)])
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
