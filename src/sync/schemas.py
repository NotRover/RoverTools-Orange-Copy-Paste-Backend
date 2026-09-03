import uuid
from typing import Literal

from pydantic import BaseModel, Field

from src.config import settings


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
    space_ids: list[uuid.UUID] = Field(default_factory=list)
    # CEK envelope map: {"personal": wrapped, "<space_id>": wrapped, ...}. Opaque.
    wrapped_keys: str = "{}"


class PushRequest(BaseModel):
    # A hard 422 rather than per-entry conflicts: the client pushes one entry per
    # request, so anything near this is not the app asking.
    entries: list[PushEntry] = Field(default_factory=list, max_length=settings.max_push_batch)


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
    space_ids: list[uuid.UUID]
    wrapped_keys: str
    blob_key: str | None
    blob_size: int | None

    model_config = {"from_attributes": True}


class RemovalOut(BaseModel):
    """One entry that left one space, for a device catching up.

    Carries the same fields as the `space:entry_removed` event, so a client
    applies both through one code path. `author_id` and `removed_by` are what
    separate an author withdrawing their own post from a space owner moderating
    it - without the pair, a client can only guess, and guessing told everyone
    that the author had been moderated.
    """

    space_id: uuid.UUID
    client_id: str
    entry_type: str
    author_id: uuid.UUID
    removed_by: uuid.UUID
    server_ts: int

    model_config = {"from_attributes": True}


class PullResponse(BaseModel):
    entries: list[SyncEntryOut]
    # Additive, and safe for a client that ignores it: such a client is exactly
    # as well off as it was before this field existed. Apply these *before*
    # `entries` - an entry re-shared after a withdrawal carries a newer
    # `server_ts` than the removal, so removal-then-entry lands on the right
    # final state while the reverse order drops a live entry.
    removals: list[RemovalOut] = Field(default_factory=list)
    next_cursor: int | None


# ── Cursor ────────────────────────────────────────────────────────────────────


class CursorUpdateRequest(BaseModel):
    last_server_ts: int


# ── Breakdown ─────────────────────────────────────────────────────────────────


class BreakdownOut(BaseModel):
    """How many live rows the account holds, split the way the account screen
    draws its cloud bar. Counts only, from one aggregate query — no ciphertext
    crosses the wire, so it answers in milliseconds where paging every row did
    not. `kind` is the plaintext label the server already routes on; `text` is
    every clipboard row that is not one of the three named kinds (an older row
    with no kind lands here too), so the parts always sum to `clipboard`.
    """

    clipboard: int
    notes: int
    total: int
    text: int
    image: int
    file: int
    html: int
