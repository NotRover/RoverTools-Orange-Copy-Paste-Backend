"""Live Share — real-time clipboard/note sharing sessions.

A Live Share session is just a `group_type='live_share'` group (capped at 5
members) over the same tables as pools; this module holds its schemas, service
logic, and router together. Invites are delivered over WebSocket to online users
and by email (best-effort, via BackgroundTasks) to the invitee.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import realtime as rt
from src.auth.models import Profile
from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.groups import invites as invites_module
from src.groups.models import Group, GroupMembership
from src.groups.service import new_invite_code

_INVITE_TTL_HOURS = 24
_MAX_MEMBERS = 5

ShareScope = Literal["clipboard", "notes", "both"]


# ── Schemas ─────────────────────────────────────────────────────────────────────


class InviteRequest(BaseModel):
    email: EmailStr
    share_scope: ShareScope = "clipboard"


class InviteResponse(BaseModel):
    share_group_id: uuid.UUID
    invite_code: str
    expires_at: int


class SessionMember(BaseModel):
    user_id: uuid.UUID
    display_name: str
    # Provider avatar URL (Google), or None — clients fall back to initials.
    avatar_url: str | None = None
    scope: str
    # X25519 identity public key — lets the owner (re)wrap the session Group Key
    # for this member; None until the member registers keys.
    identity_pubkey: str | None = None
    has_group_key: bool = False
    # Presence snapshot resolved from Redis at request time: true when any of
    # this member's devices is connected. Without it clients have no way to
    # tell who is actually reachable in the session.
    online: bool = False


class SessionOut(BaseModel):
    share_group_id: uuid.UUID
    owner_id: uuid.UUID
    members: list[SessionMember]
    my_scope: str
    active_since: int
    # The requester's own wrapped Group Key. Session keys live in memory only on
    # clients, so this is how a session survives an app restart — same recovery
    # contract as GroupOut.my_wrapped_group_key for pools.
    my_wrapped_group_key: str | None = None


class ScopeUpdateRequest(BaseModel):
    share_scope: ShareScope


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


# ── Service ─────────────────────────────────────────────────────────────────────


async def create_invite(db: AsyncSession, user_id: str, share_scope: str) -> InviteResponse:
    uid = uuid.UUID(user_id)
    invite_code = new_invite_code()
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

    db.add(GroupMembership(group_id=group.id, user_id=uid, role="owner", share_scope=share_scope, joined_at=now))
    await db.commit()
    await db.refresh(group)

    return InviteResponse(share_group_id=group.id, invite_code=invite_code, expires_at=expires_at)


async def list_sessions(db: AsyncSession, redis: Redis, user_id: str) -> list[SessionOut]:
    uid = uuid.UUID(user_id)
    memberships = await db.scalars(select(GroupMembership).where(GroupMembership.user_id == uid))
    sessions: list[SessionOut] = []
    for m in memberships.all():
        g = await db.scalar(select(Group).where(Group.id == m.group_id, Group.group_type == "live_share"))
        if g:
            sessions.append(await _session_to_out(db, redis, g, m))
    return sessions


async def _session_to_out(
    db: AsyncSession, redis: Redis, g: Group, my_membership: GroupMembership
) -> SessionOut:
    rows = await db.scalars(select(GroupMembership).where(GroupMembership.group_id == g.id))
    members: list[SessionMember] = []
    for m in rows.all():
        profile = await db.scalar(select(Profile).where(Profile.id == m.user_id))
        members.append(
            SessionMember(
                user_id=m.user_id,
                display_name=profile.display_name if profile else str(m.user_id),
                avatar_url=profile.avatar_url if profile else None,
                scope=m.share_scope,
                identity_pubkey=profile.identity_pubkey if profile else None,
                has_group_key=m.wrapped_group_key is not None,
                online=await rt.user_is_online(redis, str(m.user_id)),
            )
        )
    return SessionOut(
        share_group_id=g.id,
        owner_id=g.owner_id,
        members=members,
        my_scope=my_membership.share_scope,
        active_since=g.created_at,
        my_wrapped_group_key=my_membership.wrapped_group_key,
    )


async def update_scope(db: AsyncSession, share_group_id: uuid.UUID, user_id: str, share_scope: str) -> None:
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.id == share_group_id, Group.group_type == "live_share"))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    m = await db.scalar(
        select(GroupMembership).where(GroupMembership.group_id == share_group_id, GroupMembership.user_id == uid)
    )
    if not m:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member")
    m.share_scope = share_scope
    await db.commit()


async def end_session(db: AsyncSession, share_group_id: uuid.UUID, user_id: str) -> None:
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.id == share_group_id, Group.group_type == "live_share"))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if g.owner_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")
    await db.delete(g)
    await db.commit()


async def leave_session(db: AsyncSession, share_group_id: uuid.UUID, user_id: str) -> str:
    """Returns the leaving member's share_scope for the scope_changed broadcast."""
    uid = uuid.UUID(user_id)
    g = await db.scalar(select(Group).where(Group.id == share_group_id, Group.group_type == "live_share"))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if g.owner_id == uid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Owner must use DELETE /sharing/sessions/{id} to dissolve the session",
        )
    m = await db.scalar(
        select(GroupMembership).where(GroupMembership.group_id == share_group_id, GroupMembership.user_id == uid)
    )
    leaving_scope = m.share_scope if m else "none"
    if m:
        await db.delete(m)
        await db.commit()
    return leaving_scope


# ── Router ──────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/sharing", tags=["sharing"])


@router.post("/invite", response_model=InviteResponse, status_code=201)
async def send_invite(
    body: InviteRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Create a Live Share session and send an addressed invite for it.

    Requires: Bearer token + X-Device-Id header. The invite is persisted (shows
    under pending invites for both sides), pushed live as `invite:received`
    when the invitee is a known user, and emailed best-effort with the join code.
    """
    user_id, _ = current
    result = await create_invite(db, user_id, body.share_scope)

    group = await db.scalar(select(Group).where(Group.id == result.share_group_id))
    if group:
        try:
            await invites_module.create_invite(db, redis, background, group, user_id, body.email)
        except HTTPException:
            # Invalid invitee (own email, already a member): don't leave an
            # orphaned session behind the error.
            await db.delete(group)
            await db.commit()
            raise

    return result


@router.get("/sessions", response_model=list[SessionOut])
async def get_sessions(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """List the current user's active Live Share sessions and their members.

    Requires: Bearer token + X-Device-Id header.
    Each member carries an `online` presence snapshot resolved from Redis.
    """
    user_id, _ = current
    return await list_sessions(db, redis, user_id)


@router.patch("/sessions/{share_group_id}/scope", status_code=204)
async def patch_scope(
    share_group_id: uuid.UUID,
    body: ScopeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Change the current user's share scope within a Live Share session.

    Requires: Bearer token + X-Device-Id header.
    Emits `sharing:scope_changed` to the session's group channel.
    """
    user_id, _ = current
    await update_scope(db, share_group_id, user_id, body.share_scope)
    await rt.publish_sharing_scope_changed(redis, str(share_group_id), user_id, body.share_scope)


@router.delete("/sessions/{share_group_id}", status_code=204)
async def dissolve_session(
    share_group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Dissolve a Live Share session (owner only), disconnecting all members.

    Requires: Bearer token + X-Device-Id header.
    Emits `sharing:ended` to the session's group channel before deletion.
    """
    user_id, _ = current
    # Publish before delete so the group channel still has subscribers.
    await rt.publish_sharing_ended(redis, str(share_group_id), user_id)
    await end_session(db, share_group_id, user_id)


@router.delete("/sessions/{share_group_id}/leave", status_code=204)
async def leave(
    share_group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Leave a Live Share session (non-owner members only).

    Requires: Bearer token + X-Device-Id header.
    Emits `sharing:scope_changed` to the session's group channel.
    """
    user_id, _ = current
    leaving_scope = await leave_session(db, share_group_id, user_id)
    await rt.publish_sharing_scope_changed(redis, str(share_group_id), user_id, leaving_scope)
