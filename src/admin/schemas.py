import uuid

from pydantic import BaseModel, Field


class UserAdminSummary(BaseModel):
    id: uuid.UUID
    display_name: str
    blob_bytes_used: int
    blob_bytes_quota: int
    device_count: int
    entry_count: int
    created_at: int

    model_config = {"from_attributes": True}


class UserAdminDetail(UserAdminSummary):
    identity_pubkey: str | None
    updated_at: int
    # Account state fetched from Supabase Auth (None when Supabase admin is unconfigured).
    email: str | None = None
    email_verified: bool | None = None
    banned: bool | None = None


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
    devices_total: int
    devices_active: int
    devices_online: int
    entries_total: int
    entries_deleted: int
    blobs_total: int
    blobs_confirmed: int
    storage_bytes_used: int
    redis_memory_bytes: int | None
