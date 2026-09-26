import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from src.config import settings

# The one shape a `client_id` may take anywhere it is accepted as input: a
# canonical lowercase UUID, which is what every client generates (UUIDv4). The
# column stays TEXT; this is the gate in front of it.
CLIENT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
ClientId = Annotated[str, StringConstraints(pattern=CLIENT_ID_PATTERN, min_length=36, max_length=36)]

# The server's own blob key format, `{user_id}/{32 hex}` (see blobs.service).
BLOB_KEY_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{32}$"

# The plaintext kind labels the client writes. Empty string is a tombstone: the
# client does not know (or care) what kind a deleted row was.
EntryKind = Literal["text", "image", "html", "file", "note", ""]

_INT64_MAX = 2**63 - 1
# A client clock may run a little ahead of the server's; much more than this is
# a row that would sort ahead of everything written honestly for as long as it
# exists.
_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000
_MAX_WRAPPED_KEYS_BYTES = 8192
_MAX_SPACES_PER_ENTRY = 32


# ── Push ──────────────────────────────────────────────────────────────────────


class PushEntry(BaseModel):
    client_id: ClientId
    entry_type: Literal["clipboard", "note"]
    kind: EntryKind | None = None
    encrypted_content: str
    encrypted_metadata: str | None = None
    created_at: int = Field(ge=0, le=_INT64_MAX)
    updated_at: int = Field(ge=0, le=_INT64_MAX)
    pinned: bool = False
    deleted_at: int | None = Field(default=None, ge=0, le=_INT64_MAX)
    blob_key: str | None = Field(default=None, max_length=128, pattern=BLOB_KEY_PATTERN)
    blob_size: int | None = Field(default=None, ge=0, le=_INT64_MAX)
    space_ids: list[uuid.UUID] = Field(default_factory=list, max_length=_MAX_SPACES_PER_ENTRY)
    # CEK envelope map: {"personal": wrapped, "<space_id>": wrapped, ...}. Opaque.
    wrapped_keys: str = Field(default="{}", max_length=_MAX_WRAPPED_KEYS_BYTES)

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def _not_far_in_the_future(cls, v: int | None) -> int | None:
        # `updated_at` decides last-write-wins, so a row stamped a year ahead
        # would beat every honest edit for a year. Refused, not clamped: a
        # silently rewritten timestamp is a row the client no longer agrees with.
        if v is not None and v > int(datetime.now(UTC).timestamp() * 1000) + _MAX_FUTURE_SKEW_MS:
            raise ValueError("timestamp is more than 5 minutes in the future")
        return v


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
    last_server_ts: int = Field(ge=0, le=_INT64_MAX)


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
