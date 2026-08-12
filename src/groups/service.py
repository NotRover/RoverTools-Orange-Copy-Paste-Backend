import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import Profile
from src.groups.models import Group, GroupMembership
from src.groups.schemas import (
    CreateGroupRequest,
    DistributeKeysRequest,
    GroupOut,
    InviteResponse,
    JoinRequest,
    JoinResponse,
    MemberOut,
)

_INVITE_TTL_HOURS = 72
_LIVE_SHARE_MAX_MEMBERS = 5

# Human-typeable invite alphabet: no I/L/O/0/1, so a code survives being read
# aloud or retyped from a screenshot. 8 chars of 31 symbols ≈ 8.5e11 codes —
# ample for short-lived, rate-limited, single-group secrets.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def new_invite_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


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


async def create_group(db: AsyncSession, user_id: str, req: CreateGroupRequest) -> tuple[Group, str]:
    uid = uuid.UUID(user_id)
    invite_code = new_invite_code()
    expires_at = int((datetime.now(UTC) + timedelta(hours=_INVITE_TTL_HOURS)).timestamp() * 1000)

    group = Group(
        owner_id=uid,
        name=req.name,
        group_type=req.group_type,
        invite_code=invite_code,
        invite_expires_at=expires_at,
        max_members=None,  # pools are unlimited
        share_history=req.share_history,
        created_at=_now_ms(),
    )
    db.add(group)
    await db.flush()

    membership = GroupMembership(
        group_id=group.id,
        user_id=uid,
        role="owner",
        # The owner always sees the full group history — they authored it.
        history_from_ts=None,
        joined_at=_now_ms(),
    )
    db.add(membership)
    await db.commit()
    await db.refresh(group)
    return group, invite_code


async def list_groups(db: AsyncSession, user_id: str) -> list[GroupOut]:
    uid = uuid.UUID(user_id)
    memberships = await db.scalars(select(GroupMembership).where(GroupMembership.user_id == uid))
    group_ids = [m.group_id for m in memberships.all()]

    groups_out = []
    for gid in group_ids:
        g = await db.scalar(select(Group).where(Group.id == gid, Group.group_type == "pool"))
        if g:
            groups_out.append(await _group_to_out(db, g, uid))
    return groups_out


async def get_group(db: AsyncSession, group_id: uuid.UUID, user_id: str) -> GroupOut:
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.id == group_id))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    member = await db.scalar(
        select(GroupMembership).where(GroupMembership.group_id == group_id, GroupMembership.user_id == uid)
    )
    if not member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")

    return await _group_to_out(db, g, uid)


async def _group_to_out(db: AsyncSession, g: Group, requesting_user_id: uuid.UUID) -> GroupOut:
    # Include each member's identity public key so the owner's client can wrap the
    # Group Key for them. Only public key material is exposed — never the wrapped
    # keys themselves, which are per-member secrets.
    rows = (
        await db.execute(
            select(GroupMembership, Profile.identity_pubkey, Profile.display_name)
            .outerjoin(Profile, Profile.id == GroupMembership.user_id)
            .where(GroupMembership.group_id == g.id)
        )
    ).all()
    members = [
        MemberOut(
            user_id=m.user_id,
            display_name=display_name or "",
            role=m.role,
            joined_at=m.joined_at,
            identity_pubkey=pubkey,
            has_group_key=m.wrapped_group_key is not None,
        )
        for m, pubkey, display_name in rows
    ]
    my_wrapped = next(
        (m.wrapped_group_key for m, _, _ in rows if m.user_id == requesting_user_id), None
    )
    return GroupOut(
        id=g.id,
        owner_id=g.owner_id,
        name=g.name,
        group_type=g.group_type,
        invite_code=g.invite_code,
        invite_expires_at=g.invite_expires_at,
        max_members=g.max_members,
        share_history=g.share_history,
        created_at=g.created_at,
        members=members,
        my_wrapped_group_key=my_wrapped,
    )


async def refresh_invite(db: AsyncSession, group_id: uuid.UUID, user_id: str) -> InviteResponse:
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.id == group_id))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    if g.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")

    new_code: str = new_invite_code()
    new_expires: int = int((datetime.now(UTC) + timedelta(hours=_INVITE_TTL_HOURS)).timestamp() * 1000)
    g.invite_code = new_code
    g.invite_expires_at = new_expires
    await db.commit()
    return InviteResponse(invite_code=new_code, expires_at=new_expires)


async def add_membership(
    db: AsyncSession, g: Group, uid: uuid.UUID, wrapped_group_key: str | None = None
) -> None:
    """Idempotently add `uid` to `g`, enforcing the member cap and resolving the
    owner's history policy at join time (so later policy changes don't
    retroactively expand what an existing member sees). Shared by the
    invite-code join and the addressed-invite accept paths."""
    now = _now_ms()

    existing = await db.scalar(
        select(GroupMembership).where(GroupMembership.group_id == g.id, GroupMembership.user_id == uid)
    )
    if existing:
        return

    # Check membership cap (NULL = unlimited)
    if g.max_members:
        count_result = await db.scalars(select(GroupMembership).where(GroupMembership.group_id == g.id))
        count = len(list(count_result.all()))
        if count >= g.max_members:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group is full")

    membership = GroupMembership(
        group_id=g.id,
        user_id=uid,
        role="member",
        wrapped_group_key=wrapped_group_key,
        history_from_ts=None if g.share_history else now,
        joined_at=now,
    )
    db.add(membership)
    await db.commit()


async def join_group(db: AsyncSession, user_id: str, req: JoinRequest) -> tuple[JoinResponse, Group]:
    uid = uuid.UUID(user_id)
    code = normalize_invite_code(req.invite_code)
    g = await db.scalar(select(Group).where(Group.invite_code == code))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invite code")

    now = _now_ms()
    if g.invite_expires_at and g.invite_expires_at < now:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite code expired")

    await add_membership(db, g, uid, req.wrapped_group_key)

    return JoinResponse(group_id=g.id, name=g.name, group_type=g.group_type), g


async def remove_member(db: AsyncSession, group_id: uuid.UUID, target_user_id: uuid.UUID, requesting_user_id: str) -> None:
    rid = uuid.UUID(requesting_user_id)
    g = await db.scalar(select(Group).where(Group.id == group_id))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    is_owner = g.owner_id == rid
    is_self = target_user_id == rid
    if not is_owner and not is_self:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")

    m = await db.scalar(
        select(GroupMembership).where(GroupMembership.group_id == group_id, GroupMembership.user_id == target_user_id)
    )
    if m:
        await db.delete(m)
        await db.commit()


async def delete_group(db: AsyncSession, group_id: uuid.UUID, user_id: str) -> None:
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.id == group_id))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    if g.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")
    await db.delete(g)
    await db.commit()


async def distribute_keys(db: AsyncSession, group_id: uuid.UUID, user_id: str, req: DistributeKeysRequest) -> None:
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.id == group_id))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    if g.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")

    for entry in req.wrapped_keys:
        m = await db.scalar(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.user_id == entry.user_id,
            )
        )
        if m:
            m.wrapped_group_key = entry.wrapped_group_key
    await db.commit()
