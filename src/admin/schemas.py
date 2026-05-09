import uuid

from pydantic import BaseModel, Field


class UserAdminSummary(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    email_verified: bool
    suspended_at: int | None
    blob_bytes_used: int
    blob_bytes_quota: int
    device_count: int
    entry_count: int
    created_at: int

    model_config = {"from_attributes": True}


class UserAdminDetail(UserAdminSummary):
    identity_pubkey: str | None
    updated_at: int


class UserListResponse(BaseModel):
    users: list[UserAdminSummary]
    total: int
    offset: int
    limit: int


class QuotaUpdateRequest(BaseModel):
    blob_bytes_quota: int = Field(gt=0)


class SuspendRequest(BaseModel):
    suspend: bool


class StatsResponse(BaseModel):
    users_total: int
    users_verified: int
    users_suspended: int
    devices_total: int
    devices_active: int
    entries_total: int
    entries_deleted: int
    blobs_total: int
    blobs_confirmed: int
    storage_bytes_used: int
    ws_connections_active: int
    redis_memory_bytes: int | None
