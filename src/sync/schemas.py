import uuid
from typing import Literal

from pydantic import BaseModel, Field


# ── Push ──────────────────────────────────────────────────────────────────────

class PushEntry(BaseModel):
    client_id: str
    entry_type: Literal["clipboard", "note"]
    kind: str | None = None
    encrypted_content: str
    encrypted_metadata: str | None = None
    created_at: int
    updated_at: int
    pinned: bool = False
    deleted_at: int | None = None
    blob_key: str | None = None
    blob_size: int | None = None
    group_ids: list[uuid.UUID] = Field(default_factory=list)


class PushRequest(BaseModel):
    entries: list[PushEntry]


class AcceptedEntry(BaseModel):
    client_id: str
    server_id: uuid.UUID
    server_ts: int


class ConflictEntry(BaseModel):
    client_id: str
    reason: str


class PushResponse(BaseModel):
    accepted: list[AcceptedEntry]
    conflicts: list[ConflictEntry]


# ── Pull ──────────────────────────────────────────────────────────────────────

class SyncEntryOut(BaseModel):
    id: uuid.UUID
    client_id: str
    user_id: uuid.UUID
    device_id: uuid.UUID
    entry_type: str
    kind: str | None
    encrypted_content: str
    encrypted_metadata: str | None
    created_at: int
    updated_at: int
    server_ts: int
    deleted_at: int | None
    pinned: bool
    group_ids: list[uuid.UUID]
    blob_key: str | None
    blob_size: int | None

    model_config = {"from_attributes": True}


class PullResponse(BaseModel):
    entries: list[SyncEntryOut]
    next_cursor: int | None


# ── Cursor / Status ───────────────────────────────────────────────────────────

class CursorUpdateRequest(BaseModel):
    last_server_ts: int


class SyncStatusResponse(BaseModel):
    device_id: uuid.UUID
    last_server_ts: int
