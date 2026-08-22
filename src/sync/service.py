import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.blobs import service as blobs_service
from src.config import settings
from src.spaces.models import SpaceMembership
from src.sync.models import SyncCursor, SyncEntry
from src.sync.schemas import (
    AcceptedEntry,
    ConflictEntry,
    PushEntry,
)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


@dataclass(frozen=True)
class Withdrawal:
    """One entry, and the spaces it just left. Internal — not a wire schema."""

    client_id: str
    entry_type: str
    space_ids: list[str]


# ── Push ──────────────────────────────────────────────────────────────────────


async def push_entries(
    db: AsyncSession,
    user_id: str,
    device_id: str,
    entries: list[PushEntry],
) -> tuple[list[AcceptedEntry], list[ConflictEntry], list[Withdrawal]]:
    """Third return value: entries that left a space in this push.

    Fan-out only reaches the spaces an entry still carries, and pull matches on
    the same array — so without this, un-sharing is silent and every member
    keeps their copy forever.
    """
    accepted: list[AcceptedEntry] = []
    conflicts: list[ConflictEntry] = []
    withdrawals: list[Withdrawal] = []
    uid = uuid.UUID(user_id)
    did = uuid.UUID(device_id)

    # Counted once per push, not once per entry: the number only moves by what
    # this loop inserts, which it tracks itself.
    held = await db.scalar(
        select(func.count())
        .select_from(SyncEntry)
        .where(SyncEntry.user_id == uid, SyncEntry.deleted_at.is_(None))
    )
    room = settings.max_entries_per_user - (held or 0)

    for entry in entries:
        result, dropped, inserted = await _upsert_entry(db, uid, did, entry, room)
        if inserted:
            room -= 1
        if isinstance(result, AcceptedEntry):
            accepted.append(result)
            if dropped:
                withdrawals.append(
                    Withdrawal(
                        client_id=entry.client_id,
                        entry_type=entry.entry_type,
                        space_ids=[str(s) for s in dropped],
                    )
                )
        else:
            conflicts.append(result)

    return accepted, conflicts, withdrawals


async def _upsert_entry(
    db: AsyncSession,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    entry: PushEntry,
    room: int,
) -> tuple[AcceptedEntry | ConflictEntry, list[uuid.UUID], bool]:
    """Third return value: whether this created a row, so the caller can keep
    ``room`` honest across a batch without counting the table again."""
    # Checked before the lookup: an oversized row is refused whether it would be
    # an insert or an update, and refusing costs nothing.
    #
    # Both ciphertext fields, each against the ceiling on its own rather than as
    # a sum: metadata is a couple of hundred bytes in practice, and charging it
    # against the content budget would put the client's own limit - derived from
    # this number - a few bytes off and refuse rows that should have fit.
    if (
        len(entry.encrypted_content) > settings.max_entry_bytes
        or len(entry.encrypted_metadata or "") > settings.max_entry_bytes
    ):
        return ConflictEntry(client_id=entry.client_id, reason="entry_too_large"), [], False

    existing = await db.scalar(
        select(SyncEntry).where(
            SyncEntry.user_id == user_id,
            SyncEntry.client_id == entry.client_id,
            SyncEntry.entry_type == entry.entry_type,
        )
    )

    server_ts = _now_ms()

    if existing:
        # Tombstone always wins
        incoming_tombstone = entry.deleted_at is not None and existing.deleted_at is None
        if not incoming_tombstone and entry.updated_at <= existing.updated_at:
            return ConflictEntry(client_id=entry.client_id, reason="stale_update"), [], False

        # Spaces this push takes the entry out of. A tombstone keeps its spaces
        # so it can fan out as a delete, so this only ever fires on un-share.
        dropped = [s for s in (existing.space_ids or []) if s not in entry.space_ids]

        # The row is about to stop pointing at its current blob, either because
        # this push carries a new one (every image re-push uploads a fresh
        # object) or because it is a tombstone. Nothing else references it, so
        # release it - otherwise it occupies the user's quota forever with no
        # way to reclaim it.
        superseded_blob = existing.blob_key if existing.blob_key != entry.blob_key else None

        # The device that wrote it *last*, not the one that created it. Clients
        # suppress their own echo by comparing this to their device id, and a
        # row that kept its creator forever came back attributed to whichever
        # device first pushed it - so a tombstone pushed today by a device that
        # signed in since was not recognised as its own, and the client applied
        # its own deletion to its own local copy.
        existing.device_id = device_id
        existing.encrypted_content = entry.encrypted_content
        existing.encrypted_metadata = entry.encrypted_metadata
        existing.updated_at = entry.updated_at
        existing.server_ts = server_ts
        existing.deleted_at = entry.deleted_at
        existing.pinned = entry.pinned
        existing.blob_key = entry.blob_key
        existing.blob_size = entry.blob_size
        existing.space_ids = entry.space_ids
        existing.wrapped_keys = entry.wrapped_keys
        if superseded_blob:
            await blobs_service.release_blob(db, user_id, superseded_blob)
        await db.commit()
        return AcceptedEntry(client_id=entry.client_id, server_id=existing.id, server_ts=server_ts), dropped, False

    if await _belongs_to_someone_else(db, user_id, entry):
        return ConflictEntry(client_id=entry.client_id, reason="not_your_entry"), [], False

    # A full account can still be edited and emptied - the update path above is
    # already past this point, so tombstones and changes to rows that exist keep
    # working. Only a new row is refused.
    if room <= 0:
        return ConflictEntry(client_id=entry.client_id, reason="account_full"), [], False

    new_entry = SyncEntry(
        client_id=entry.client_id,
        user_id=user_id,
        device_id=device_id,
        entry_type=entry.entry_type,
        kind=entry.kind,
        encrypted_content=entry.encrypted_content,
        encrypted_metadata=entry.encrypted_metadata,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        server_ts=server_ts,
        deleted_at=entry.deleted_at,
        pinned=entry.pinned,
        blob_key=entry.blob_key,
        blob_size=entry.blob_size,
        space_ids=entry.space_ids,
        wrapped_keys=entry.wrapped_keys,
    )
    db.add(new_entry)
    await db.commit()
    await db.refresh(new_entry)
    return AcceptedEntry(client_id=entry.client_id, server_id=new_entry.id, server_ts=server_ts), [], True


async def _belongs_to_someone_else(db: AsyncSession, user_id: uuid.UUID, entry: PushEntry) -> bool:
    """Whether this push would plant a rival copy of somebody else's entry.

    Rows are keyed `(user_id, client_id, entry_type)`, so a push of an entry the
    caller did not write cannot overwrite the author's row - it inserts a second
    one carrying the same `client_id`. Both then fan out to the space, and every
    member has two rows claiming to be the same entry. Clients collapse them onto
    one item, so the practical result is that the author's text and name are
    replaced by whoever pushed last (client bug #8).

    Only checked when inserting: an existing row under this account means the
    caller already owns the entry.

    The space overlap is what keeps this from rejecting honest pushes. Client ids
    are UUIDv4, so two accounts holding one is not chance - but one *person* with
    two accounts and the same local history is real, and their entries collide by
    construction. Nobody is impersonated unless the two rows meet in a space, so
    that is exactly where this refuses.
    """
    if not entry.space_ids:
        return False
    rival = await db.scalar(
        select(SyncEntry.id).where(
            SyncEntry.client_id == entry.client_id,
            SyncEntry.entry_type == entry.entry_type,
            SyncEntry.user_id != user_id,
            SyncEntry.space_ids.overlap(entry.space_ids),
        )
    )
    return rival is not None


# ── Pull ──────────────────────────────────────────────────────────────────────


async def pull_entries(
    db: AsyncSession,
    user_id: str,
    after_ts: int,
    limit: int,
    entry_type: str,
) -> tuple[list[SyncEntry], int | None]:
    uid = uuid.UUID(user_id)

    # A device pulls its own user's entries plus anything shared into a space it
    # belongs to. Without the space arm, entries shared by another member only
    # ever arrive over the live WebSocket fan-out — so a member who was offline
    # when they were pushed would never receive them at all.
    visible = [and_(SyncEntry.user_id == uid, SyncEntry.server_ts > after_ts)]

    memberships = (
        await db.scalars(select(SpaceMembership).where(SpaceMembership.user_id == uid))
    ).all()
    for m in memberships:
        arm = and_(
            SyncEntry.space_ids.overlap([m.space_id]),
            SyncEntry.server_ts > after_ts,
        )
        # `history_from_ts` is the owner's share-history choice resolved at join
        # time; NULL means no floor. Applied per membership because the caller may
        # have full history in one space and post-join-only in another.
        if m.history_from_ts is not None:
            arm = and_(arm, SyncEntry.server_ts >= m.history_from_ts)
        visible.append(arm)

    q = select(SyncEntry).where(or_(*visible))
    if entry_type in ("clipboard", "note"):
        q = q.where(SyncEntry.entry_type == entry_type)

    q = q.order_by(SyncEntry.server_ts.asc()).limit(limit + 1)
    result = await db.scalars(q)
    rows = list(result.all())

    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = rows[-1].server_ts
    else:
        next_cursor = None

    return rows, next_cursor


# ── Cursor ────────────────────────────────────────────────────────────────────


async def update_cursor(db: AsyncSession, device_id: str, user_id: str, last_server_ts: int) -> None:
    did = uuid.UUID(device_id)
    uid = uuid.UUID(user_id)

    stmt = (
        pg_insert(SyncCursor)
        .values(device_id=did, user_id=uid, last_server_ts=last_server_ts)
        .on_conflict_do_update(
            index_elements=["device_id"],
            set_={"last_server_ts": last_server_ts},
            where=SyncCursor.last_server_ts < last_server_ts,
        )
    )
    await db.execute(stmt)
    await db.commit()

