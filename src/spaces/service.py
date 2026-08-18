import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src import realtime as rt
from src.auth.models import Profile
from src.spaces.models import Space, SpaceMembership
from src.spaces.schemas import (
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
    my_wrapped = next(
        (m.wrapped_space_keys for m, _, _, _ in rows if m.user_id == requesting_user_id), None
    )
    return SpaceOut(
        id=s.id,
        owner_id=s.owner_id,
        name=s.name,
        invite_code=s.invite_code,
        invite_expires_at=s.invite_expires_at,
        share_history=s.share_history,
        created_at=s.created_at,
        members=members,
        my_wrapped_space_keys=my_wrapped,
    )


async def set_share_history(
    db: AsyncSession, space_id: uuid.UUID, requesting_user_id: str, req: UpdateSpaceRequest
) -> tuple[Space, bool]:
    """Change a space's history policy. Owner only.

    Returns the space and whether this call opened the back catalogue, which the
    caller uses to decide whether members need to be told to go and fetch it.

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


async def add_membership(db: AsyncSession, s: Space, uid: uuid.UUID) -> None:
    """Idempotently add `uid` to `s`, resolving the owner's history policy at
    join time (so later policy changes don't retroactively expand what an
    existing member sees). Shared by the invite-code join and the
    addressed-invite accept paths."""
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
        history_from_ts=None if s.share_history else now,
        joined_at=now,
    )
    db.add(membership)
    await db.commit()


async def join_space(db: AsyncSession, user_id: str, invite_code: str) -> Space:
    uid = uuid.UUID(user_id)
    code = normalize_invite_code(invite_code)
    s = await db.scalar(select(Space).where(Space.invite_code == code))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invite code")

    if s.invite_expires_at and s.invite_expires_at < _now_ms():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite code expired")

    await add_membership(db, s, uid)
    return s


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
    # A departure invalidates the Space Key: clear every remaining member's
    # wrapped keyring so the owner's next reconcile mints a fresh key and
    # redistributes. Until that lands, members decrypt with the keys they hold
    # in memory — nothing breaks, they just show `has_space_key=false` briefly.
    # Without this, the removed member could keep reading new entries forever.
    remaining = await db.scalars(select(SpaceMembership).where(SpaceMembership.space_id == space_id))
    for member in remaining.all():
        member.wrapped_space_keys = None
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
) -> None:
    uid = uuid.UUID(user_id)
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    if s.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")

    for entry in req.wrapped_keyrings:
        m = await db.scalar(
            select(SpaceMembership).where(
                SpaceMembership.space_id == space_id,
                SpaceMembership.user_id == entry.user_id,
            )
        )
        if m:
            m.wrapped_space_keys = entry.wrapped_space_keys
    await db.commit()


async def remove_entry_from_space(
    db: AsyncSession, space_id: uuid.UUID, client_id: str, entry_type: str, user_id: str
) -> str:
    """Take a shared entry down from a space (owner action). Returns the author's id.

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
    if s.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")

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
