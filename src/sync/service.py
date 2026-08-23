import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.blobs import service as blobs_service
from src.spaces.service import record_space_removals
from src.config import settings
from src.spaces.models import SpaceEntryRemoval, SpaceMembership
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
        # Same transaction as the strip. The event this push triggers reaches
        # whoever is connected; this is what a member who was offline reads
        # later, and pull cannot infer it because the row no longer carries the
        # space id it would have to match on.
        #
        # `removed_by` is the author here by construction - this path is a push
        # from the owner of the entry, so un-sharing is always self-inflicted.
        # The moderation case goes through `remove_entry_from_space` instead and
        # records whoever took it down.
        if dropped:
            await record_space_removals(
                db,
                space_ids=dropped,
                client_id=entry.client_id,
                entry_type=entry.entry_type,
                author_id=user_id,
                removed_by=user_id,
                server_ts=server_ts,
            )
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
) -> tuple[list[SyncEntry], list[SpaceEntryRemoval], int | None]:
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
    rows, entries_next = _paginate(rows, limit)

    removals, removals_next = await _pull_removals(db, memberships, after_ts, limit, entry_type)

    return rows, removals, merge_cursors(entries_next, removals_next)


def merge_cursors(entries_next: int | None, removals_next: int | None) -> int | None:
    """One watermark over two streams.

    A pull now returns two independently paginated streams against a single
    cursor, so the cursor may only advance to a point *both* are complete to.
    Taking the entry stream's position while the removal stream was truncated
    earlier steps the device past removals it never received - and a missed
    removal is the one fact in this system that nothing else recovers, which is
    the entire reason the removals stream exists.

    So: the lower of the two truncation points, and `None` (meaning "caught up")
    only when neither stream was truncated. Erring low re-sends part of the
    un-truncated stream on the next round, which costs bytes and nothing else -
    entries merge last-write-wins and a removal for an entry you no longer hold
    is a no-op, so both are safe to apply twice.
    """
    truncations = [c for c in (entries_next, removals_next) if c is not None]
    return min(truncations) if truncations else None


def _paginate(rows: list, limit: int) -> tuple[list, int | None]:
    """Trim an over-fetched page and report where it stopped."""
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, rows[-1].server_ts
    return rows, None


async def _pull_removals(
    db: AsyncSession,
    memberships: Sequence[SpaceMembership],
    after_ts: int,
    limit: int,
    entry_type: str,
) -> tuple[list[SpaceEntryRemoval], int | None]:
    """Entries that left this caller's spaces since their cursor.

    The second half of a pull, and the only way a device that was not connected
    at the time can learn a withdrawal happened: removing an entry from a space
    strips the space id from `space_ids`, and the entries query above matches on
    exactly that, so the row it would need to notice is not in the result set at
    all - it is absent, which is indistinguishable from an entry that never
    existed.

    `history_from_ts` is applied the same way it is for entries, so a member who
    joined after a withdrawal is not told about content they never held.
    """
    if not memberships:
        return [], None

    arms = []
    for m in memberships:
        arm = and_(
            SpaceEntryRemoval.space_id == m.space_id,
            SpaceEntryRemoval.server_ts > after_ts,
        )
        if m.history_from_ts is not None:
            arm = and_(arm, SpaceEntryRemoval.server_ts >= m.history_from_ts)
        arms.append(arm)

    q = select(SpaceEntryRemoval).where(or_(*arms))
    if entry_type in ("clipboard", "note"):
        q = q.where(SpaceEntryRemoval.entry_type == entry_type)
    q = q.order_by(SpaceEntryRemoval.server_ts.asc()).limit(limit + 1)

    rows = list((await db.scalars(q)).all())
    return _paginate(rows, limit)


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

