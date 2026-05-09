import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.groups.models import Group, GroupMembership
from src.groups.schemas import (
    CreateGroupRequest,
    DistributeKeysRequest,
    GroupOut,
    InviteResponse,
    JoinRequest,
    JoinResponse,
    MemberOut,
    WrappedKeyEntry,
)

_INVITE_TTL_HOURS = 72
_LIVE_SHARE_MAX_MEMBERS = 5


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _new_invite_code() -> str:
    return secrets.token_urlsafe(24)


async def create_group(db: AsyncSession, user_id: str, req: CreateGroupRequest) -> tuple[Group, str]:
    uid = uuid.UUID(user_id)
    invite_code = _new_invite_code()
    expires_at = int((datetime.now(UTC) + timedelta(hours=_INVITE_TTL_HOURS)).timestamp() * 1000)

    group = Group(
        owner_id=uid,
        name=req.name,
        group_type=req.group_type,
        invite_code=invite_code,
        invite_expires_at=expires_at,
        max_members=0,
        created_at=_now_ms(),
    )
    db.add(group)
    await db.flush()

    membership = GroupMembership(
        group_id=group.id,
        user_id=uid,
        role="owner",
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
    members_rows = await db.scalars(select(GroupMembership).where(GroupMembership.group_id == g.id))
    members = [MemberOut(user_id=m.user_id, role=m.role, joined_at=m.joined_at) for m in members_rows.all()]
    return GroupOut(
        id=g.id,
        owner_id=g.owner_id,
        name=g.name,
        group_type=g.group_type,
        invite_code=g.invite_code,
        invite_expires_at=g.invite_expires_at,
        max_members=g.max_members,
        created_at=g.created_at,
        members=members,
    )


async def refresh_invite(db: AsyncSession, group_id: uuid.UUID, user_id: str) -> InviteResponse:
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.id == group_id))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    if g.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")

    g.invite_code = _new_invite_code()
    g.invite_expires_at = int((datetime.now(UTC) + timedelta(hours=_INVITE_TTL_HOURS)).timestamp() * 1000)
    await db.commit()
    return InviteResponse(invite_code=g.invite_code, expires_at=g.invite_expires_at)


async def join_group(db: AsyncSession, user_id: str, req: JoinRequest) -> tuple[JoinResponse, Group]:
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.invite_code == req.invite_code))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invite code")

    now = _now_ms()
    if g.invite_expires_at and g.invite_expires_at < now:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite code expired")

    # Check membership cap
    if g.max_members > 0:
        count_result = await db.scalars(select(GroupMembership).where(GroupMembership.group_id == g.id))
        count = len(list(count_result.all()))
        if count >= g.max_members:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group is full")

    # Idempotent — already a member
    existing = await db.scalar(select(GroupMembership).where(GroupMembership.group_id == g.id, GroupMembership.user_id == uid))
    if not existing:
        membership = GroupMembership(
            group_id=g.id,
            user_id=uid,
            role="member",
            wrapped_group_key=req.wrapped_group_key,
            joined_at=now,
        )
        db.add(membership)
        await db.commit()

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
