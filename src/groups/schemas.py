import uuid
from typing import Literal

from pydantic import BaseModel, Field


# ── Group ─────────────────────────────────────────────────────────────────────

class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    group_type: Literal["pool"] = "pool"


class CreateGroupResponse(BaseModel):
    group_id: uuid.UUID
    invite_code: str


class MemberOut(BaseModel):
    user_id: uuid.UUID
    role: str
    joined_at: int


class GroupOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    group_type: str
    invite_code: str | None
    invite_expires_at: int | None
    max_members: int
    created_at: int
    members: list[MemberOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


# ── Invite / Join ─────────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    email: str | None = None


class InviteResponse(BaseModel):
    invite_code: str
    expires_at: int


class JoinRequest(BaseModel):
    invite_code: str
    wrapped_group_key: str | None = None


class JoinResponse(BaseModel):
    group_id: uuid.UUID
    name: str
    group_type: str


# ── Key distribution ──────────────────────────────────────────────────────────

class WrappedKeyEntry(BaseModel):
    user_id: uuid.UUID
    wrapped_group_key: str


class DistributeKeysRequest(BaseModel):
    wrapped_keys: list[WrappedKeyEntry]
