"""Document-only contracts. A shared development admin is not an individual user."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 1024


@dataclass(frozen=True)
class DocumentScope:
    tenant_id: str
    room_ids: tuple[str, ...]
    actor: str = "development-admin"


class ImportMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    import_id: UUID
    source_key: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=0, le=1_000_000)


class DocumentView(BaseModel):
    id: UUID
    tenant_id: UUID
    room_id: UUID
    source_key: str
    source_type: Literal["markdown", "text", "pdf", "docx", "pptx"]
    title: str
    created_by: str
    latest_version: int
    published_version_id: UUID | None
    active: bool
    created_at: datetime


class VersionView(BaseModel):
    id: UUID
    document_id: UUID
    version_number: int
    filename: str
    title: str
    source_text: str
    content_hash: str
    recipe: str
    model_recipe: str
    status: Literal["processing", "completed", "failed"]
    error: str | None
    created_at: datetime


class ChunkView(BaseModel):
    id: UUID
    document_id: UUID
    version_id: UUID
    position: int
    text: str
    content_hash: str
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    section: str | None
    status: Literal["pending", "processing", "completed", "failed"]
    attempts: int
    error: str | None


class ImportResult(BaseModel):
    import_id: UUID
    document_id: UUID
    version_id: UUID
    version_number: int
    status: Literal["processing", "completed", "failed"]
    duplicate: bool


class DocumentSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    document_id: UUID | None = None
    limit: int = Field(default=5, ge=1, le=20)


class DocumentReference(BaseModel):
    source_type: Literal["document"] = "document"
    document_id: UUID
    version_id: UUID
    version_number: int
    chunk_id: UUID
    title: str
    section: str | None
    start_line: int
    end_line: int
    text: str
    similarity: float
    citation: str
    location_basis: Literal["stored_text_lines"] = "stored_text_lines"
