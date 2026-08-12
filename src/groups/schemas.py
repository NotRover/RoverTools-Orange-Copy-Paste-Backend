import uuid
from typing import Literal

from pydantic import BaseModel, Field


# ── Group ─────────────────────────────────────────────────────────────────────


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    group_type: Literal["pool"] = "pool"
    # May members who join later read entries pushed before they joined?
    share_history: bool = True


class CreateGroupResponse(BaseModel):
    group_id: uuid.UUID
    invite_code: str


class MemberOut(BaseModel):
    user_id: uuid.UUID
    role: str
    joined_at: int
    # The member's X25519 identity public key, needed by the owner to wrap the
    # Group Key for them. None until that member registers their keys — such a
    # member cannot be wrapped for yet and is retried on the next distribution.
    identity_pubkey: str | None = None
    # Whether this member already holds a wrapped Group Key. Lets the owner wrap
    # only for members who need one, instead of re-distributing to everybody on
    # every reconcile — which the server echoes back as `group:rekey` and would
    # otherwise drive an endless distribute/reconcile cycle. The key itself is
    # never exposed, only its presence.
    has_group_key: bool = False


class GroupOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    group_type: str
    invite_code: str | None
    invite_expires_at: int | None
    max_members: int | None
    share_history: bool = True
    created_at: int
    members: list[MemberOut] = Field(default_factory=list)
    # The *requesting* member's own wrapped Group Key, so a client can recover it
    # after a restart. Group keys live in memory only on the client, and the
    # `group:rekey` WebSocket event is fire-and-forget — without this, a member who
    # restarted could never decrypt the group again. Never exposes another member's key.
    my_wrapped_group_key: str | None = None

    model_config = {"from_attributes": True}


# ── Invite / Join ─────────────────────────────────────────────────────────────


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
