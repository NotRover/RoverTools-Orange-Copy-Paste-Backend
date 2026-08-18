import uuid
from typing import Literal

from pydantic import BaseModel, Field


# ── Space ─────────────────────────────────────────────────────────────────────


class CreateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # May members who join later read entries pushed before they joined?
    share_history: bool = True


class UpdateSpaceRequest(BaseModel):
    # Owner-only. Turning this ON also drops the floor for members who already
    # joined, so "share the history" means what it says rather than applying to
    # future joiners only. Turning it OFF never takes history away from someone
    # who can already read it - it only changes what the next joiner gets.
    share_history: bool


class CreateSpaceResponse(BaseModel):
    space_id: uuid.UUID
    invite_code: str


class MemberOut(BaseModel):
    user_id: uuid.UUID
    display_name: str = ""
    # Provider avatar URL (Google), or None — clients fall back to initials.
    avatar_url: str | None = None
    role: str
    joined_at: int
    # The member's X25519 identity public key, needed by the owner to wrap the
    # Space Key for them. None until that member registers their keys — such a
    # member cannot be wrapped for yet and is retried on the next distribution.
    identity_pubkey: str | None = None
    # Whether this member already holds a wrapped keyring. Lets the owner wrap
    # only for members who need one, instead of re-distributing to everybody on
    # every reconcile — which the server echoes back as `space:rekey` and would
    # otherwise drive an endless distribute/reconcile cycle. The keyring itself
    # is never exposed, only its presence.
    has_space_key: bool = False
    # Presence snapshot resolved from Redis at request time: true when any of
    # this member's devices is connected.
    online: bool = False


class SpaceOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    invite_code: str | None
    invite_expires_at: int | None
    share_history: bool = True
    created_at: int
    members: list[MemberOut] = Field(default_factory=list)
    # The *requesting* member's own wrapped keyring, so a client can recover it
    # after a restart. Space Keys live in memory only on the client, and the
    # `space:rekey` WebSocket event is fire-and-forget — without this, a member
    # who restarted could never decrypt the space again. Never exposes another
    # member's keyring.
    my_wrapped_space_keys: str | None = None

    model_config = {"from_attributes": True}


# ── Join ──────────────────────────────────────────────────────────────────────


class JoinRequest(BaseModel):
    invite_code: str


class JoinResponse(BaseModel):
    space_id: uuid.UUID
    name: str


# ── Key distribution ──────────────────────────────────────────────────────────


class WrappedKeyringEntry(BaseModel):
    user_id: uuid.UUID
    # JSON array of X25519-wrapped Space Keys, newest first — opaque to the server.
    wrapped_space_keys: str


class DistributeKeysRequest(BaseModel):
    wrapped_keyrings: list[WrappedKeyringEntry]


# ── Comments ──────────────────────────────────────────────────────────────────


class CreateCommentRequest(BaseModel):
    # The entry being commented on, addressed the way every space route
    # addresses one.
    client_id: str = Field(min_length=1, max_length=128)
    entry_type: Literal["clipboard", "note"] = "clipboard"
    # Sealed under a random per-comment key; the server never sees the text or
    # who was mentioned in it.
    encrypted_body: str = Field(min_length=1, max_length=16384)
    # That key, X25519-wrapped under the Space Key. Opaque to the server.
    wrapped_key: str = Field(min_length=1, max_length=1024)


class CommentOut(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    client_id: str
    entry_type: str
    author_id: uuid.UUID
    encrypted_body: str
    wrapped_key: str
    created_at: int

    model_config = {"from_attributes": True}


class CommentCountOut(BaseModel):
    """One entry's comment tally, for the chips on the feed.

    `latest_at` is what lets a client show an unread marker without pulling
    every thread: it compares the newest comment against the last time this
    device opened that entry.
    """

    client_id: str
    entry_type: str
    count: int
    latest_at: int
