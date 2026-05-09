import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import User
from src.groups.models import Group, GroupMembership
from src.sharing.schemas import InviteResponse, ScopeUpdateRequest, SessionMember, SessionOut

_INVITE_TTL_HOURS = 24
_MAX_MEMBERS = 5


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


async def create_invite(
    db: AsyncSession,
    user_id: str,
    email: str,
    share_scope: str,
) -> tuple[InviteResponse, User | None]:
    uid = uuid.UUID(user_id)
    invite_code = secrets.token_urlsafe(24)
    expires_at = int((datetime.now(UTC) + timedelta(hours=_INVITE_TTL_HOURS)).timestamp() * 1000)
    now = _now_ms()

    group = Group(
        owner_id=uid,
        name="Live Share",
        group_type="live_share",
        invite_code=invite_code,
        invite_expires_at=expires_at,
        max_members=_MAX_MEMBERS,
        created_at=now,
    )
    db.add(group)
    await db.flush()

    membership = GroupMembership(
        group_id=group.id,
        user_id=uid,
        role="owner",
        share_scope=share_scope,
        joined_at=now,
    )
    db.add(membership)
    await db.commit()
    await db.refresh(group)

    # Look up invitee for WS notification — None if not yet registered
    invitee = await db.scalar(select(User).where(User.email == email))

    return InviteResponse(
        share_group_id=group.id,
        invite_code=invite_code,
        expires_at=expires_at,
    ), invitee


async def list_sessions(db: AsyncSession, user_id: str) -> list[SessionOut]:
    uid = uuid.UUID(user_id)
    memberships = await db.scalars(
        select(GroupMembership).where(GroupMembership.user_id == uid)
    )
    sessions = []
    for m in memberships.all():
        g = await db.scalar(
            select(Group).where(Group.id == m.group_id, Group.group_type == "live_share")
        )
        if not g:
            continue
        sessions.append(await _session_to_out(db, g, uid, m.share_scope))
    return sessions


async def _session_to_out(
    db: AsyncSession, g: Group, requesting_uid: uuid.UUID, my_scope: str
) -> SessionOut:
    members_rows = await db.scalars(
        select(GroupMembership).where(GroupMembership.group_id == g.id)
    )
    members = []
    for m in members_rows.all():
        user = await db.scalar(select(User).where(User.id == m.user_id))
        display_name = user.display_name if user else str(m.user_id)
        members.append(
            SessionMember(
                user_id=m.user_id,
                display_name=display_name,
                scope=m.share_scope,
            )
        )
    return SessionOut(
        share_group_id=g.id,
        members=members,
        my_scope=my_scope,
        active_since=g.created_at,
    )


async def update_scope(
    db: AsyncSession, share_group_id: uuid.UUID, user_id: str, req: ScopeUpdateRequest
) -> None:
    uid = uuid.UUID(user_id)
    g = await db.scalar(
        select(Group).where(Group.id == share_group_id, Group.group_type == "live_share")
    )
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    m = await db.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == share_group_id, GroupMembership.user_id == uid
        )
    )
    if not m:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")

    m.share_scope = req.share_scope
    await db.commit()


async def end_session(db: AsyncSession, share_group_id: uuid.UUID, user_id: str) -> None:
    uid = uuid.UUID(user_id)
    g = await db.scalar(
        select(Group).where(Group.id == share_group_id, Group.group_type == "live_share")
    )
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if g.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")

    await db.delete(g)
    await db.commit()


async def leave_session(db: AsyncSession, share_group_id: uuid.UUID, user_id: str) -> str:
    """Returns the leaving member's share_scope for the scope_changed broadcast."""
    uid = uuid.UUID(user_id)
    g = await db.scalar(
        select(Group).where(Group.id == share_group_id, Group.group_type == "live_share")
    )
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if g.owner_id == uid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Owner must use DELETE /sharing/sessions/{id} to dissolve the session",
        )

    m = await db.scalar(
        select(GroupMembership).where(
            GroupMembership.group_id == share_group_id, GroupMembership.user_id == uid
        )
    )
    leaving_scope = m.share_scope if m else "none"
    if m:
        await db.delete(m)
        await db.commit()
    return leaving_scope
