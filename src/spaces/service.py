import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src import realtime as rt
from src.auth.models import Profile
from src.spaces.models import Space, SpaceComment, SpaceInvite, SpaceJoinRequest, SpaceMembership
from src.spaces.schemas import (
    CommentCountOut,
    CreateCommentRequest,
    CreateSpaceRequest,
    DistributeKeysRequest,
    MemberOut,
    SpaceOut,
    UpdateSpaceRequest,
)
from src.sync.models import SyncEntry

_INVITE_TTL_HOURS = 72

# Human-typeable invite alphabet: no I/L/O/0/1, so a code survives being read
# aloud or retyped from a screenshot. 8 chars of 31 symbols ≈ 8.5e11 codes —
# ample for short-lived, rate-limited, single-space secrets.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def new_invite_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _new_invite_expiry() -> int:
    return int((datetime.now(UTC) + timedelta(hours=_INVITE_TTL_HOURS)).timestamp() * 1000)


def normalize_invite_code(raw: str) -> str:
    """Canonicalize user input for the short code format: strip separators and
    uppercase. Anything longer than the short format is a legacy
    `token_urlsafe` code, which is case-sensitive — returned untouched."""
    stripped = raw.strip().replace("-", "").replace(" ", "")
    if len(stripped) == _CODE_LENGTH and stripped.isalnum():
        return stripped.upper()
    return raw.strip()


def format_invite_code(code: str) -> str:
    """Display form of a short code (`KX7Q2M4X` → `KX7Q-2M4X`); legacy codes
    pass through unchanged."""
    if len(code) == _CODE_LENGTH and code.isalnum():
        return f"{code[:4]}-{code[4:]}"
    return code


def may_approve(s: Space, role: str | None) -> bool:
    """Who may answer the door: the owner always, members if the owner said so.

    `role` is None for a caller who is not a member at all. One function, one
    definition - `SpaceOut.i_can_approve` is this call, which is why no client
    re-implements the rule and none of them can disagree with the server.
    """
    if role is None:
        return False
    return role == "owner" or s.members_can_approve


async def create_space(db: AsyncSession, user_id: str, req: CreateSpaceRequest) -> tuple[Space, str]:
    uid = uuid.UUID(user_id)
    invite_code = new_invite_code()

    space = Space(
        owner_id=uid,
        name=req.name,
        invite_code=invite_code,
        invite_expires_at=_new_invite_expiry(),
        share_history=req.share_history,
        created_at=_now_ms(),
    )
    db.add(space)
    await db.flush()

    membership = SpaceMembership(
        space_id=space.id,
        user_id=uid,
        role="owner",
        # The owner always sees the full space history — they authored it.
        history_from_ts=None,
        joined_at=_now_ms(),
    )
    db.add(membership)
    await db.commit()
    await db.refresh(space)
    return space, invite_code


async def list_spaces(db: AsyncSession, redis: Redis, user_id: str) -> list[SpaceOut]:
    uid = uuid.UUID(user_id)
    memberships = await db.scalars(select(SpaceMembership).where(SpaceMembership.user_id == uid))
    space_ids = [m.space_id for m in memberships.all()]

    out = []
    for sid in space_ids:
        s = await db.scalar(select(Space).where(Space.id == sid))
        if s:
            out.append(await _space_to_out(db, redis, s, uid))
    return out


async def get_space(db: AsyncSession, redis: Redis, space_id: uuid.UUID, user_id: str) -> SpaceOut:
    uid = uuid.UUID(user_id)
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")

    member = await db.scalar(
        select(SpaceMembership).where(SpaceMembership.space_id == space_id, SpaceMembership.user_id == uid)
    )
    if not member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")

    return await _space_to_out(db, redis, s, uid)


async def _space_to_out(
    db: AsyncSession, redis: Redis, s: Space, requesting_user_id: uuid.UUID
) -> SpaceOut:
    # Include each member's identity public key so the owner's client can wrap the
    # Space Key for them, and a live presence snapshot so member lists can show
    # who is reachable. Only public key material is exposed — never the wrapped
    # keyrings themselves, which are per-member secrets.
    rows = (
        await db.execute(
            select(
                SpaceMembership,
                Profile.identity_pubkey,
                Profile.display_name,
                Profile.avatar_url,
            )
            .outerjoin(Profile, Profile.id == SpaceMembership.user_id)
            .where(SpaceMembership.space_id == s.id)
        )
    ).all()
    members = [
        MemberOut(
            user_id=m.user_id,
            display_name=display_name or "",
            avatar_url=avatar_url,
            role=m.role,
            joined_at=m.joined_at,
            identity_pubkey=pubkey,
            has_space_key=m.wrapped_space_keys is not None,
            online=await rt.user_is_online(redis, str(m.user_id)),
        )
        for m, pubkey, display_name, avatar_url in rows
    ]
    mine = next((m for m, _, _, _ in rows if m.user_id == requesting_user_id), None)
    i_can_approve = may_approve(s, mine.role if mine else None)
    # Counted only for somebody who could act on it: a member who cannot approve
    # has no use for the number, and it is not theirs to see.
    pending = 0
    if i_can_approve:
        pending = (
            await db.scalar(
                select(func.count())
                .select_from(SpaceJoinRequest)
                .where(SpaceJoinRequest.space_id == s.id, SpaceJoinRequest.status == "pending")
            )
        ) or 0
    return SpaceOut(
        id=s.id,
        owner_id=s.owner_id,
        name=s.name,
        invite_code=s.invite_code,
        invite_expires_at=s.invite_expires_at,
        share_history=s.share_history,
        created_at=s.created_at,
        members=members,
        my_wrapped_space_keys=mine.wrapped_space_keys if mine else None,
        my_wrapped_by=mine.wrapped_by if mine else None,
        key_fingerprint=s.key_fingerprint,
        rekey_requested_at=s.rekey_requested_at,
        members_can_approve=s.members_can_approve,
        i_can_approve=i_can_approve,
        pending_join_requests=pending,
    )


async def update_space(
    db: AsyncSession, space_id: uuid.UUID, requesting_user_id: str, req: UpdateSpaceRequest
) -> tuple[Space, bool]:
    """Change a space's owner-only settings: history policy, approval policy.

    Returns the space and whether this call opened the back catalogue, which the
    caller uses to decide whether members need to be told to go and fetch it.

    Both fields are optional and applied independently, so a client can flip one
    without having to restate the other and risk clobbering it.

    Turning it on clears every current member's floor as well as setting the
    policy for future joiners: an owner who says "share the history" means the
    people already in the space, and they are the only ones who were stuck.
    Turning it off leaves existing floors alone - a member who can already read
    the history keeps it, and revoking that is not something a pull filter could
    enforce anyway once the entries are on their device.
    """
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    if s.owner_id != uuid.UUID(requesting_user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can change this")

    if req.members_can_approve is not None:
        s.members_can_approve = req.members_can_approve
    if req.share_history is not None:
        s.share_history = req.share_history
    # Whether a floor actually comes off, not whether the flag changed: only then
    # does anyone have older entries to go and fetch. Read first so the answer is
    # a plain list rather than a driver-specific row count.
    floored = []
    if req.share_history:
        floored = list(
            await db.scalars(
                select(SpaceMembership.user_id).where(
                    SpaceMembership.space_id == space_id,
                    SpaceMembership.history_from_ts.is_not(None),
                )
            )
        )
        if floored:
            await db.execute(
                update(SpaceMembership)
                .where(SpaceMembership.space_id == space_id, SpaceMembership.history_from_ts.is_not(None))
                .values(history_from_ts=None)
            )
    opened = bool(floored)
    await db.commit()
    await db.refresh(s)
    return s, opened


async def add_membership(
    db: AsyncSession,
    s: Space,
    uid: uuid.UUID,
    wrapped_space_keys: str | None = None,
    wrapped_by: uuid.UUID | None = None,
) -> None:
    """Idempotently add `uid` to `s`, resolving the owner's history policy at
    join time (so later policy changes don't retroactively expand what an
    existing member sees). Shared by the invite-code join and the
    addressed-invite accept paths.

    `wrapped_space_keys` is the accept path handing over a ring the inviter
    wrapped before this member existed in the space. Setting it here is what makes
    a new member able to read the space the moment they join, instead of waiting
    for somebody else's app to be running.
    """
    now = _now_ms()

    existing = await db.scalar(
        select(SpaceMembership).where(SpaceMembership.space_id == s.id, SpaceMembership.user_id == uid)
    )
    if existing:
        return

    membership = SpaceMembership(
        space_id=s.id,
        user_id=uid,
        role="member",
        wrapped_space_keys=wrapped_space_keys,
        wrapped_by=wrapped_by,
        history_from_ts=None if s.share_history else now,
        joined_at=now,
    )
    db.add(membership)
    await db.commit()


async def remove_member(
    db: AsyncSession, space_id: uuid.UUID, target_user_id: uuid.UUID, requesting_user_id: str
) -> None:
    rid = uuid.UUID(requesting_user_id)
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")

    is_owner = s.owner_id == rid
    is_self = target_user_id == rid
    if not is_owner and not is_self:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")
    if target_user_id == s.owner_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Owner cannot leave; delete the space")

    m = await db.scalar(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id, SpaceMembership.user_id == target_user_id
        )
    )
    if not m:
        return

    await db.delete(m)
    # A departure invalidates the Space Key: clear the other members' wrapped
    # keyrings so the owner's next reconcile mints a fresh key and redistributes.
    # Until that lands, members decrypt with the keys they hold in memory —
    # nothing breaks, they just show `has_space_key=false` briefly. Without this,
    # the removed member could keep reading new entries forever.
    #
    # The owner's own wrap is deliberately left alone, and `rekey_requested_at`
    # carries the signal instead. The owner's ring lives only in memory, and this
    # wrap is the only way it survives a restart: clearing it too meant a restart
    # between the departure and the redistribution destroyed the previous keys,
    # and with them every entry ever shared in the space.
    remaining = await db.scalars(select(SpaceMembership).where(SpaceMembership.space_id == space_id))
    for member in remaining.all():
        if member.user_id != s.owner_id:
            member.wrapped_space_keys = None
            member.wrapped_by = None
    s.rekey_requested_at = _now_ms()
    # Any key already wrapped onto a pending invite is now the *previous* key, and
    # a joiner adopting it would fail the fingerprint check the owner is about to
    # publish. Drop it: they join without a key and the ordinary distribution path
    # picks them up, which is the same place they were before invites carried one.
    pending = await db.scalars(
        select(SpaceInvite).where(
            SpaceInvite.space_id == space_id,
            SpaceInvite.wrapped_space_keys.is_not(None),
        )
    )
    for inv in pending.all():
        inv.wrapped_space_keys = None
    await db.commit()


async def delete_space(db: AsyncSession, space_id: uuid.UUID, user_id: str) -> None:
    uid = uuid.UUID(user_id)
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    if s.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")
    await db.delete(s)
    await db.commit()


async def distribute_keys(
    db: AsyncSession, space_id: uuid.UUID, user_id: str, req: DistributeKeysRequest
) -> list[uuid.UUID]:
    """Store per-member wrapped keyrings. Returns the members actually written.

    Any member may hand the key over, not only the owner. Every member already
    holds the Space Key in memory and could pass it on by other means, so
    owner-only was never a boundary against a member who wanted to leak - only
    against one who never intended to, at the cost of leaving a newcomer blocked
    whenever the owner's app was closed.

    What the caller may not do is decide what the key *is*: `key_fingerprint` is
    honoured only from the owner, who is the only one that mints one. A member
    relaying a ring has nothing new to declare, and writing here would let it
    redefine what everyone else verifies against - which is the check that catches
    a bad ring in the first place.

    `wrapped_by` records the caller so the recipient knows whose public key to
    compute its shared secret against.
    """
    uid = uuid.UUID(user_id)
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")

    caller = await db.scalar(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id, SpaceMembership.user_id == uid
        )
    )
    if not caller:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")

    is_owner = s.owner_id == uid
    if is_owner and req.key_fingerprint:
        s.key_fingerprint = req.key_fingerprint

    written: list[uuid.UUID] = []
    for entry in req.wrapped_keyrings:
        m = await db.scalar(
            select(SpaceMembership).where(
                SpaceMembership.space_id == space_id,
                SpaceMembership.user_id == entry.user_id,
            )
        )
        if not m:
            continue
        m.wrapped_space_keys = entry.wrapped_space_keys
        m.wrapped_by = uid
        written.append(m.user_id)

    # The owner has re-wrapped for whoever needed it, so the departure that owed a
    # rekey is settled. Only the owner can settle it: a member relaying the old
    # ring has not rotated anything.
    if is_owner and s.rekey_requested_at is not None:
        remaining = (
            await db.scalars(select(SpaceMembership).where(SpaceMembership.space_id == space_id))
        ).all()
        if all(m.wrapped_space_keys is not None for m in remaining):
            s.rekey_requested_at = None

    await db.commit()
    return written


async def remove_entry_from_space(
    db: AsyncSession, space_id: uuid.UUID, client_id: str, entry_type: str, user_id: str
) -> str:
    """Take a shared entry down from a space. Returns the author's id.

    Two callers, one effect. The space owner may take down anything in the space
    (moderation); any member may take down what they themselves shared
    (unsharing). Nobody else gets to touch it.

    Moderation, not deletion: the space id and its wrapped copy of the CEK are
    dropped from the entry, so the space stops carrying it and future members
    cannot decrypt it. The author's own row survives — they keep their personal
    copy, which is wrapped under their UMK and unaffected.

    Pull's space arm matches on `space_ids` overlap, so a row that just lost the
    space is invisible to members from here on. The `space:entry_removed` event
    is what tells the members already holding a copy to drop it.
    """
    uid = uuid.UUID(user_id)
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")

    rows = await db.scalars(
        select(SyncEntry).where(
            SyncEntry.client_id == client_id,
            SyncEntry.entry_type == entry_type,
            SyncEntry.space_ids.overlap([space_id]),
        )
    )
    matched = rows.all()
    if not matched:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not in this space")

    # A non-owner may only take down rows they wrote. Narrowing rather than
    # rejecting outright keeps the owner path untouched: they still clear every
    # row carrying this client_id, whoever wrote it.
    if s.owner_id != uid:
        matched = [row for row in matched if row.user_id == uid]
        if not matched:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the space owner or the member who shared it can remove this",
            )

    author_id = str(matched[0].user_id)
    server_ts = _now_ms()
    for row in matched:
        row.space_ids = [sid for sid in row.space_ids if sid != space_id]
        try:
            keys = json.loads(row.wrapped_keys or "{}")
        except json.JSONDecodeError:
            keys = {}
        if isinstance(keys, dict) and keys.pop(str(space_id), None) is not None:
            row.wrapped_keys = json.dumps(keys)
        row.server_ts = server_ts
    await db.commit()
    return author_id


# ── Comments ──────────────────────────────────────────────────────────────────


async def _require_member(db: AsyncSession, space_id: uuid.UUID, uid: uuid.UUID) -> Space:
    """The gate every comment route shares: the space exists and this user is in it.

    Membership is the whole permission model for reading and writing comments —
    a space is a room, and everyone in it can talk. Who may *delete* is narrower
    and lives in `delete_comment`.
    """
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    member = await db.scalar(
        select(SpaceMembership).where(SpaceMembership.space_id == space_id, SpaceMembership.user_id == uid)
    )
    if not member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")
    return s


async def add_comment(
    db: AsyncSession, space_id: uuid.UUID, user_id: str, req: CreateCommentRequest
) -> SpaceComment:
    """Post a comment on an entry in a space.

    The entry itself is not checked. A member can hold a copy of something that
    has since been taken down, and rejecting the comment then would fail the one
    person who still has the item on screen. An orphaned thread is harmless: it
    is only ever read by asking for that entry's comments.
    """
    uid = uuid.UUID(user_id)
    await _require_member(db, space_id, uid)

    row = SpaceComment(
        space_id=space_id,
        client_id=req.client_id,
        entry_type=req.entry_type,
        author_id=uid,
        encrypted_body=req.encrypted_body,
        wrapped_key=req.wrapped_key,
        created_at=_now_ms(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_comments(
    db: AsyncSession, space_id: uuid.UUID, user_id: str, client_id: str, entry_type: str
) -> list[SpaceComment]:
    """One entry's thread, oldest first — the order it is read in."""
    uid = uuid.UUID(user_id)
    await _require_member(db, space_id, uid)
    rows = await db.scalars(
        select(SpaceComment)
        .where(
            SpaceComment.space_id == space_id,
            SpaceComment.client_id == client_id,
            SpaceComment.entry_type == entry_type,
        )
        .order_by(SpaceComment.created_at)
    )
    return list(rows.all())


async def comment_counts(db: AsyncSession, space_id: uuid.UUID, user_id: str) -> list[CommentCountOut]:
    """Every commented-on entry in the space, with its tally and newest comment.

    One request per space rather than one per card: a feed of a hundred items
    would otherwise open a hundred threads just to draw the chips.
    """
    uid = uuid.UUID(user_id)
    await _require_member(db, space_id, uid)
    rows = await db.execute(
        select(
            SpaceComment.client_id,
            SpaceComment.entry_type,
            func.count().label("n"),
            func.max(SpaceComment.created_at).label("latest"),
        )
        .where(SpaceComment.space_id == space_id)
        .group_by(SpaceComment.client_id, SpaceComment.entry_type)
    )
    return [
        CommentCountOut(client_id=cid, entry_type=et, count=n, latest_at=latest)
        for cid, et, n, latest in rows.all()
    ]


async def delete_comment(
    db: AsyncSession, space_id: uuid.UUID, comment_id: uuid.UUID, user_id: str
) -> SpaceComment:
    """Delete a comment. Returns the row as it was, for the fan-out event.

    Same two callers as taking an entry down: its author, and the space owner as
    moderator. A hard delete rather than a tombstone — comments are never stored
    locally, so a client that was offline simply never sees it again.
    """
    uid = uuid.UUID(user_id)
    s = await _require_member(db, space_id, uid)

    row = await db.scalar(
        select(SpaceComment).where(SpaceComment.id == comment_id, SpaceComment.space_id == space_id)
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    if row.author_id != uid and s.owner_id != uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the space owner or the member who wrote it can delete this comment",
        )

    # Detached copy: the caller needs the fields after the row is gone.
    snapshot = SpaceComment(
        id=row.id,
        space_id=row.space_id,
        client_id=row.client_id,
        entry_type=row.entry_type,
        author_id=row.author_id,
        encrypted_body=row.encrypted_body,
        wrapped_key=row.wrapped_key,
        created_at=row.created_at,
    )
    await db.execute(sa_delete(SpaceComment).where(SpaceComment.id == comment_id))
    await db.commit()
    return snapshot
