import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.blobs import service as blobs_service
from src.blobs.models import Blob
from src.spaces.service import record_space_removals
from src.config import settings
from src.spaces.models import SpaceEntryRemoval, SpaceMembership
from src.sync.models import SyncCursor, SyncEntry
from src.sync.schemas import (
    AcceptedEntry,
    BreakdownOut,
    ConflictEntry,
    PushEntry,
    SyncEntryOut,
)

# Namespace for the two-key `pg_advisory_xact_lock(ns, hashtext(user_id))` a push
# takes. Arbitrary and stable; it only has to differ from other two-key users.
_PUSH_LOCK_NAMESPACE = 0x5EC5
# Tombstone rows an account may hold, as a multiple of the live-row cap.
_TOMBSTONE_CAP_FACTOR = 3


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


@dataclass(frozen=True)
class Withdrawal:
    """One entry, and the spaces it just left. Internal — not a wire schema."""

    client_id: str
    entry_type: str
    space_ids: list[str]


@dataclass
class _Room:
    """What is left of the account's two row budgets within one push."""

    live: int
    tombstones: int


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

    One transaction for the whole batch, committed once at the end. That is
    what makes the per-user advisory lock below mean anything (a transaction
    lock is released at the first commit), and it means a batch refused with a
    422 part of the way through leaves nothing behind.
    """
    accepted: list[AcceptedEntry] = []
    conflicts: list[ConflictEntry] = []
    withdrawals: list[Withdrawal] = []
    uid = uuid.UUID(user_id)
    did = uuid.UUID(device_id)

    # Serialises concurrent pushes from one account, so two of them cannot both
    # read the same count and each take the last free slot. Two-key form, so it
    # sits in its own namespace apart from the maintenance loop's one-key lock.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:uid))"),
        {"ns": _PUSH_LOCK_NAMESPACE, "uid": user_id},
    )

    # Counted once per push, not once per entry: the numbers only move by what
    # this loop writes, which it tracks itself.
    held = await db.scalar(
        select(func.count())
        .select_from(SyncEntry)
        .where(SyncEntry.user_id == uid, SyncEntry.deleted_at.is_(None))
    )
    buried = await db.scalar(
        select(func.count())
        .select_from(SyncEntry)
        .where(SyncEntry.user_id == uid, SyncEntry.deleted_at.is_not(None))
    )
    room = _Room(
        live=settings.max_entries_per_user - (held or 0),
        tombstones=settings.max_entries_per_user * _TOMBSTONE_CAP_FACTOR - (buried or 0),
    )

    for entry in entries:
        try:
            result, dropped = await _upsert_entry(db, uid, did, entry, room)
        except HTTPException:
            # Nothing from this batch survives a refusal, including the rows
            # already flushed ahead of the one that failed.
            await db.rollback()
            raise
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

    await db.commit()
    return accepted, conflicts, withdrawals


async def _upsert_entry(
    db: AsyncSession,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    entry: PushEntry,
    room: _Room,
) -> tuple[AcceptedEntry | ConflictEntry, list[uuid.UUID]]:
    """Write one entry into the push's transaction. Does not commit.

    Updates ``room`` in place when the write takes a live or a tombstone slot,
    so the caller's budgets stay honest across a batch without counting the
    table again.

    Raises 422 (`not_a_member`, `invalid_blob`) rather than returning a
    conflict: those are bodies no retry can fix, and the client pushes one entry
    per request.
    """
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
        return ConflictEntry(client_id=entry.client_id, reason="entry_too_large"), []

    existing = await db.scalar(
        select(SyncEntry).where(
            SyncEntry.user_id == user_id,
            SyncEntry.client_id == entry.client_id,
            SyncEntry.entry_type == entry.entry_type,
        )
    )

    # Nobody decrypts a tombstone, so it keeps no ciphertext, no key envelope
    # and no blob. Storing what the client sent would make deleted rows - which
    # the live-row cap does not count - a free place to park `max_entry_bytes`
    # per row.
    tombstone = entry.deleted_at is not None
    content = "" if tombstone else entry.encrypted_content
    metadata = None if tombstone else entry.encrypted_metadata
    wrapped_keys = "{}" if tombstone else entry.wrapped_keys
    blob_key = None if tombstone else entry.blob_key
    blob_size = None if tombstone else entry.blob_size

    # Spaces this push puts the entry *into*: all of them on an insert, and on
    # an update only the ones the row does not already carry. A tombstone keeps
    # the row's spaces so the delete reaches the same members, and the author
    # may have left one of them since - that must not stop the delete.
    already = set(existing.space_ids or []) if existing else set()
    added = [s for s in entry.space_ids if s not in already]
    if added:
        await _require_memberships(db, user_id, added)

    if blob_key is not None and (existing is None or existing.blob_key != blob_key):
        await _require_own_blob(db, user_id, blob_key)

    server_ts = _now_ms()

    if existing:
        # Tombstone always wins
        incoming_tombstone = tombstone and existing.deleted_at is None
        if not incoming_tombstone and entry.updated_at <= existing.updated_at:
            return ConflictEntry(client_id=entry.client_id, reason="stale_update"), []

        # The insert rule, applied to the spaces this update adds: sharing a row
        # this account holds into a space where another account already holds
        # the same entry is the same impersonation as inserting it there.
        if added and await _belongs_to_someone_else(db, user_id, entry, added):
            return ConflictEntry(client_id=entry.client_id, reason="not_your_entry"), []

        # Bringing a deleted row back takes a live slot, as an insert does.
        # Without this the live cap was the tombstone cap plus one update each.
        revived = existing.deleted_at is not None and not tombstone
        if revived and room.live <= 0:
            return ConflictEntry(client_id=entry.client_id, reason="account_full"), []

        # Spaces this push takes the entry out of. A tombstone keeps its spaces
        # so it can fan out as a delete, so this only ever fires on un-share.
        dropped = [s for s in (existing.space_ids or []) if s not in entry.space_ids]

        # The row is about to stop pointing at its current blob, either because
        # this push carries a new one (every image re-push uploads a fresh
        # object) or because it is a tombstone. Nothing else references it, so
        # release it - otherwise it occupies the user's quota forever with no
        # way to reclaim it.
        superseded_blob = existing.blob_key if existing.blob_key != blob_key else None

        # The device that wrote it *last*, not the one that created it. Clients
        # suppress their own echo by comparing this to their device id, and a
        # row that kept its creator forever came back attributed to whichever
        # device first pushed it - so a tombstone pushed today by a device that
        # signed in since was not recognised as its own, and the client applied
        # its own deletion to its own local copy.
        existing.device_id = device_id
        existing.encrypted_content = content
        existing.encrypted_metadata = metadata
        existing.updated_at = entry.updated_at
        existing.server_ts = server_ts
        existing.deleted_at = entry.deleted_at
        existing.pinned = entry.pinned
        existing.blob_key = blob_key
        existing.blob_size = blob_size
        existing.space_ids = entry.space_ids
        existing.wrapped_keys = wrapped_keys
        if revived:
            room.live -= 1
            room.tombstones += 1
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
        await db.flush()
        return AcceptedEntry(client_id=entry.client_id, server_id=existing.id, server_ts=server_ts), dropped

    if await _belongs_to_someone_else(db, user_id, entry, entry.space_ids):
        return ConflictEntry(client_id=entry.client_id, reason="not_your_entry"), []

    # A full account can still be edited and emptied - the update path above is
    # already past this point, so changes to rows that exist keep working. A new
    # tombstone is let through a full account too: it is the very thing that
    # frees space, and refusing it broke "remove from my devices" for a received
    # entry at quota - that hide is a brand-new tombstone row. Tombstones have a
    # budget of their own instead (`_TOMBSTONE_CAP_FACTOR` times the live cap),
    # so they are not an unmetered way to create rows.
    if tombstone:
        if room.tombstones <= 0:
            return ConflictEntry(client_id=entry.client_id, reason="account_full"), []
    elif room.live <= 0:
        return ConflictEntry(client_id=entry.client_id, reason="account_full"), []

    new_entry = SyncEntry(
        client_id=entry.client_id,
        user_id=user_id,
        device_id=device_id,
        entry_type=entry.entry_type,
        kind=entry.kind,
        encrypted_content=content,
        encrypted_metadata=metadata,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        server_ts=server_ts,
        deleted_at=entry.deleted_at,
        pinned=entry.pinned,
        blob_key=blob_key,
        blob_size=blob_size,
        space_ids=entry.space_ids,
        wrapped_keys=wrapped_keys,
    )
    db.add(new_entry)
    await db.flush()
    if tombstone:
        room.tombstones -= 1
    else:
        room.live -= 1
    return AcceptedEntry(client_id=entry.client_id, server_id=new_entry.id, server_ts=server_ts), []


async def _require_memberships(db: AsyncSession, user_id: uuid.UUID, space_ids: list[uuid.UUID]) -> None:
    """Refuse a push that puts an entry into a space the caller is not in.

    `space_ids` is the fan-out target list and what pull's space arm matches
    on, so without this any account could publish into any space whose id it
    had seen. The detail names nothing about which space failed or why: a space
    the caller was removed from and one that never existed read the same.
    """
    held = set(
        (
            await db.scalars(
                select(SpaceMembership.space_id).where(
                    SpaceMembership.user_id == user_id,
                    SpaceMembership.space_id.in_(space_ids),
                )
            )
        ).all()
    )
    if any(s not in held for s in space_ids):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="not_a_member")


async def _require_own_blob(db: AsyncSession, user_id: uuid.UUID, blob_key: str) -> None:
    """A row may only point at a blob this account uploaded and confirmed.

    Download access follows the entry (`_shares_space_with_blob`), so a row
    naming another account's key would otherwise be a way to read it.
    """
    owned = await db.scalar(
        select(Blob.key).where(Blob.key == blob_key, Blob.user_id == user_id, Blob.confirmed.is_(True))
    )
    if owned is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_blob")


async def _belongs_to_someone_else(
    db: AsyncSession, user_id: uuid.UUID, entry: PushEntry, space_ids: list[uuid.UUID]
) -> bool:
    """Whether this push would plant a rival copy of somebody else's entry.

    Rows are keyed `(user_id, client_id, entry_type)`, so a push of an entry the
    caller did not write cannot overwrite the author's row - it inserts a second
    one carrying the same `client_id`. Both then fan out to the space, and every
    member has two rows claiming to be the same entry. Clients collapse them onto
    one item, so the practical result is that the author's text and name are
    replaced by whoever pushed last (client bug #8).

    `space_ids` is what this push adds: every space on an insert, and on an
    update only the spaces the row did not carry before.

    The space overlap is what keeps this from rejecting honest pushes. Client ids
    are UUIDv4, so two accounts holding one is not chance - but one *person* with
    two accounts and the same local history is real, and their entries collide by
    construction. Nobody is impersonated unless the two rows meet in a space, so
    that is exactly where this refuses.
    """
    if not space_ids:
        return False
    rival = await db.scalar(
        select(SyncEntry.id)
        .where(
            SyncEntry.client_id == entry.client_id,
            SyncEntry.entry_type == entry.entry_type,
            SyncEntry.user_id != user_id,
            SyncEntry.space_ids.overlap(space_ids),
        )
        .limit(1)
    )
    return rival is not None


# ── Pull ──────────────────────────────────────────────────────────────────────


async def pull_entries(
    db: AsyncSession,
    user_id: str,
    after_ts: int,
    limit: int,
    entry_type: str,
) -> tuple[list[SyncEntryOut], list[SpaceEntryRemoval], int | None]:
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

    rows, entries_next = await _page(db, q, SyncEntry, limit)
    member_spaces = {m.space_id for m in memberships}
    out = [entry_view(r, uid, member_spaces) for r in rows]

    removals, removals_next = await _pull_removals(db, memberships, after_ts, limit, entry_type)

    return out, removals, merge_cursors(entries_next, removals_next)


def entry_view(row: SyncEntry, reader: uuid.UUID, reader_spaces: set[uuid.UUID]) -> SyncEntryOut:
    """A row as one reader may see it.

    The author sees their row whole. Anyone else reached it through a space, and
    sees only the spaces they are in and the wraps for those spaces: the other
    spaces an entry went to, and the personal wrap under the author's UMK, are
    the author's business.
    """
    out = SyncEntryOut.model_validate(row)
    if row.user_id == reader:
        return out
    shown = [s for s in (row.space_ids or []) if s in reader_spaces]
    try:
        keys: Any = json.loads(row.wrapped_keys or "{}")
    except json.JSONDecodeError:
        keys = {}
    kept = {str(s): keys[str(s)] for s in shown if str(s) in keys} if isinstance(keys, dict) else {}
    return out.model_copy(update={"space_ids": shown, "wrapped_keys": json.dumps(kept)})


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


async def _page[T: (SyncEntry, SpaceEntryRemoval)](
    db: AsyncSession, q: Select[tuple[T]], model: type[T], limit: int
) -> tuple[list[T], int | None]:
    """One page of `q`, and the cursor to resume from - never splitting a timestamp.

    The cursor is a bare `server_ts` and the next pull asks for `> cursor`, so a
    page that stopped in the middle of a run of rows sharing one `server_ts` would
    skip the rest of that run for good. Rows do share one: a removal stamps every
    row it strips with the same time, and a batch push lands many rows in one
    millisecond. So a page ends on the last complete timestamp, and when a single
    timestamp holds more than a whole page, that timestamp is returned whole.
    """
    ordered = q.order_by(model.server_ts.asc(), model.id.asc())
    rows = list((await db.scalars(ordered.limit(limit + 1))).all())
    if len(rows) <= limit:
        return rows, None
    boundary = rows[limit].server_ts
    page = rows[:limit]
    if page[-1].server_ts != boundary:
        return page, page[-1].server_ts
    page = [r for r in page if r.server_ts < boundary]
    if page:
        return page, page[-1].server_ts
    whole = list((await db.scalars(q.where(model.server_ts == boundary).order_by(model.id.asc()))).all())
    return whole, boundary


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
    return await _page(db, q, SpaceEntryRemoval, limit)


# ── Cursor ────────────────────────────────────────────────────────────────────


async def update_cursor(db: AsyncSession, device_id: str, user_id: str, last_server_ts: int) -> None:
    """Advance this device's cursor. Forward only, and only on the caller's row.

    The table's key is `device_id` alone, so the conflict target stays that; the
    `user_id` condition is what stops a write naming another account's device id
    from moving that account's cursor.
    """
    did = uuid.UUID(device_id)
    uid = uuid.UUID(user_id)

    stmt = (
        pg_insert(SyncCursor)
        .values(device_id=did, user_id=uid, last_server_ts=last_server_ts)
        .on_conflict_do_update(
            index_elements=["device_id"],
            set_={"last_server_ts": last_server_ts},
            where=and_(SyncCursor.user_id == uid, SyncCursor.last_server_ts < last_server_ts),
        )
    )
    await db.execute(stmt)
    await db.commit()


# ── Breakdown ─────────────────────────────────────────────────────────────────


async def account_breakdown(db: AsyncSession, user_id: str) -> BreakdownOut:
    """Live-row counts for the account, split by entry_type and kind.

    One `GROUP BY` over the `(user_id)` index instead of paging every row down to
    the client to tally there. Tombstones are excluded (`deleted_at IS NULL`), so
    this matches the account screen's "synced items" count. `text` absorbs every
    clipboard kind that is not image / file / html — including a legacy row with a
    null kind — so `text + image + file + html == clipboard` always holds.
    """
    uid = uuid.UUID(user_id)
    rows = await db.execute(
        select(SyncEntry.entry_type, SyncEntry.kind, func.count())
        .where(SyncEntry.user_id == uid, SyncEntry.deleted_at.is_(None))
        .group_by(SyncEntry.entry_type, SyncEntry.kind)
    )

    notes = 0
    named = {"image": 0, "file": 0, "html": 0}
    clipboard = 0
    for entry_type, kind, count in rows:
        if entry_type == "note":
            notes += count
            continue
        clipboard += count
        if kind in named:
            named[kind] += count

    text_rows = clipboard - named["image"] - named["file"] - named["html"]
    return BreakdownOut(
        clipboard=clipboard,
        notes=notes,
        total=clipboard + notes,
        text=text_rows,
        image=named["image"],
        file=named["file"],
        html=named["html"],
    )
