"""Restricted development-admin imports. No server path or remote URL input."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.config import get_settings
from app.core.http_clients import service_client
from app.core.security import require_admin_token
from app.models.documents import (ChunkView, DocumentReference, DocumentSearch,
    DocumentView, ImportMetadata, ImportResult, MAX_UPLOAD_BYTES, VersionView)
from app.repositories.document_repository import SupabaseDocumentRepository
from app.services.document_authorization import resolve_document_scope
from app.services.document_service import DocumentService

router = APIRouter(prefix="/api/v1/documents", tags=["development-admin documents"],
                   dependencies=[Depends(require_admin_token)])


async def document_scope(room_id: str = Query(min_length=1,max_length=200)):
    settings = get_settings()
    if not settings.document_ingestion_enabled:
        raise HTTPException(503,"document_ingestion_disabled")
    async with service_client("database") as client:
        return await resolve_document_scope(settings,room_id,client=client)


def document_repository(request: Request):
    return getattr(request.app.state,"document_repository",None) or SupabaseDocumentRepository()


def page(limit: int = Query(20,ge=1,le=50), offset: int = Query(0,ge=0,le=10000)):
    return dict(limit=limit,offset=offset)


@router.post("/imports", response_model=ImportResult, status_code=202)
async def import_document(request: Request, metadata: ImportMetadata = Depends(),
                          scope=Depends(document_scope), repository=Depends(document_repository)):
    raw = bytearray()
    async for part in request.stream():
        if len(raw)+len(part) > min(MAX_UPLOAD_BYTES,get_settings().switch_document_max_bytes):
            raise HTTPException(413,"document_too_large")
        raw.extend(part)
    result = await DocumentService(repository).import_bytes(scope,metadata,bytes(raw))
    return result


@router.post("/search", response_model=list[DocumentReference])
async def search_documents(body: DocumentSearch, scope=Depends(document_scope), repository=Depends(document_repository)):
    return await DocumentService(repository).search(scope,body)


@router.get("/imports/{import_id}",response_model=ImportResult)
async def import_status(import_id: UUID, scope=Depends(document_scope), repository=Depends(document_repository)):
    return await repository.call(scope,"import_status",dict(import_id=str(import_id)))


@router.get("",response_model=list[DocumentView])
async def list_documents(scope=Depends(document_scope), pagination=Depends(page), repository=Depends(document_repository)):
    return await repository.call(scope,"documents",pagination)


@router.get("/{document_id}",response_model=DocumentView)
async def document_detail(document_id: UUID, scope=Depends(document_scope), repository=Depends(document_repository)):
    return await repository.call(scope,"document",dict(document_id=str(document_id)))


@router.post("/{document_id}/archive",response_model=DocumentView)
async def archive_document(document_id: UUID, expected_version: int = Query(ge=1),
                           scope=Depends(document_scope), repository=Depends(document_repository)):
    return await repository.call(scope,"archive",dict(document_id=str(document_id),expected_version=expected_version))


@router.get("/{document_id}/versions",response_model=list[VersionView])
async def list_versions(document_id: UUID, scope=Depends(document_scope), pagination=Depends(page),
                        repository=Depends(document_repository)):
    return await repository.call(scope,"versions",dict(document_id=str(document_id),**pagination))


@router.get("/{document_id}/versions/{version_id}",response_model=VersionView)
async def version_detail(document_id: UUID, version_id: UUID, scope=Depends(document_scope),
                         repository=Depends(document_repository)):
    return await repository.call(scope,"version",dict(document_id=str(document_id),version_id=str(version_id)))


@router.get("/{document_id}/versions/{version_id}/chunks",response_model=list[ChunkView])
async def inspect_chunks(document_id: UUID, version_id: UUID, scope=Depends(document_scope), pagination=Depends(page),
                         repository=Depends(document_repository)):
    return await repository.call(scope,"chunks",dict(document_id=str(document_id),version_id=str(version_id),**pagination))


@router.get("/{document_id}/versions/{version_id}/chunks/{chunk_id}",response_model=ChunkView)
async def chunk_detail(document_id: UUID, version_id: UUID, chunk_id: UUID, scope=Depends(document_scope),
                        repository=Depends(document_repository)):
    return await repository.call(scope,"chunk",dict(document_id=str(document_id),version_id=str(version_id),chunk_id=str(chunk_id)))
