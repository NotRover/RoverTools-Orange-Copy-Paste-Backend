"""Join requests - what redeeming a space's invite code produces now.

The code is a multi-use bearer capability: one per space, eight characters, no
rotate route. Redeeming it used to grant membership outright, so a forwarded mail
or a screenshot in a group chat was a silent join and the owner found out by
noticing a name in the member list. The code was doing two jobs, introduction and
authorisation. This module is the second one.

Addressed invites do not come through here. The owner naming an email *is* the
approval, and that path already pre-wraps the key.

Approval is what makes this faster rather than slower. A code-joiner always had
to wait for somebody's app to wrap a Space Key for them - they just waited inside
the space, looking at nothing. Whoever approves is by definition online and
holding the ring at that instant, so the wrap goes out with the decision and the
requester lands able to read.

Status lifecycle: pending -> approved | declined. A declined row is kept: with the
unique index on (space_id, user_id) it is what stops somebody who still holds the
code from knocking again.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import Profile
from src.spaces import service as spaces_service
from src.spaces.models import Space, SpaceJoinRequest, SpaceMembership


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


# ── Schemas ─────────────────────────────────────────────────────────────


class JoinRequestOut(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    user_id: uuid.UUID
    display_name: str = ""
    avatar_url: str | None = None
    status: str
    created_at: int
    # The requester's X25519 identity public key, so the approver's client can
    # wrap the Space Key for them in the same action as the approval. None means
    # they have not registered keys yet - approving is still allowed, and they
    # pick a key up on the next distribution like any other keyless member.
    identity_pubkey: str | None = None


class ApproveJoinRequest(BaseModel):
    # JSON array of X25519-wrapped Space Keys, newest first, wrapped for the
    # requester's identity key. Opaque to the server. Optional because an
    # approver whose own ring has not arrived yet should still be able to let
    # somebody in - the requester then waits for a key the way any member does.
    wrapped_space_keys: str | None = Field(default=None, max_length=8192)


# ── Policy ──────────────────────────────────────────────────────────────


async def _role_of(db: AsyncSession, space_id: uuid.UUID, uid: uuid.UUID) -> str | None:
    return await db.scalar(
        select(SpaceMembership.role).where(
            SpaceMembership.space_id == space_id, SpaceMembership.user_id == uid
        )
    )


async def _require_approver(db: AsyncSession, space_id: uuid.UUID, uid: uuid.UUID) -> Space:
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    if not spaces_service.may_approve(s, await _role_of(db, space_id, uid)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner can approve join requests for this space",
        )
    return s


# ── Service ─────────────────────────────────────────────────────────────


async def _to_out(db: AsyncSession, r: SpaceJoinRequest, s: Space) -> JoinRequestOut:
    p = await db.scalar(select(Profile).where(Profile.id == r.user_id))
    return JoinRequestOut(
        id=r.id,
        space_id=r.space_id,
        space_name=s.name,
        user_id=r.user_id,
        display_name=p.display_name if p else "",
        avatar_url=p.avatar_url if p else None,
        status=r.status,
        created_at=r.created_at,
        identity_pubkey=p.identity_pubkey if p else None,
    )


async def request_join(
    db: AsyncSession, user_id: str, invite_code: str
) -> tuple[Space, SpaceJoinRequest | None, str]:
    """Redeem an invite code as a request to join, not as a join.

    Returns the space, the request row when one is now pending, and the status
    to report. Four answers, and only one of them is new work:

    - already a member: `"member"`, and no row. Re-pasting a code you already
      used should be a no-op, not an error.
    - a pending row exists: `"pending"`, that row, and nothing published. The
      unique index makes this the collision case rather than a second knock.
    - a declined row exists: `"declined"`. The row is left exactly as it is,
      which is the point of keeping it.
    - otherwise: a fresh pending row.
    """
    uid = uuid.UUID(user_id)
    code = spaces_service.normalize_invite_code(invite_code)
    s = await db.scalar(select(Space).where(Space.invite_code == code))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invite code")

    if s.invite_expires_at and s.invite_expires_at < _now_ms():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite code expired")

    if await _role_of(db, s.id, uid) is not None:
        return s, None, "member"

    existing = await db.scalar(
        select(SpaceJoinRequest).where(
            SpaceJoinRequest.space_id == s.id, SpaceJoinRequest.user_id == uid
        )
    )
    if existing:
        if existing.status == "pending":
            return s, existing, "pending"
        if existing.status == "declined":
            return s, None, "declined"
        # 'approved' without a membership means the member left afterwards.
        # Leaving them locked out of a code they hold would be a dead end, so
        # the spent row is reopened rather than duplicated.
        existing.status = "pending"
        existing.wrapped_space_keys = None
        existing.wrapped_by = None
        existing.decided_at = None
        existing.decided_by = None
        existing.created_at = _now_ms()
        await db.commit()
        return s, existing, "pending"

    row = SpaceJoinRequest(space_id=s.id, user_id=uid, status="pending", created_at=_now_ms())
    db.add(row)
    await db.commit()
    return s, row, "pending"


async def list_requests(
    db: AsyncSession, space_id: uuid.UUID, requesting_user_id: str
) -> list[JoinRequestOut]:
    """Pending requests on one space, for somebody who may approve them."""
    uid = uuid.UUID(requesting_user_id)
    s = await _require_approver(db, space_id, uid)
    rows = await db.scalars(
        select(SpaceJoinRequest)
        .where(SpaceJoinRequest.space_id == space_id, SpaceJoinRequest.status == "pending")
        .order_by(SpaceJoinRequest.created_at)
    )
    return [await _to_out(db, r, s) for r in rows.all()]


class MyJoinRequestOut(BaseModel):
    """One of the caller's own outstanding knocks.

    A pending request is not a membership, so the space is absent from
    `GET /spaces` entirely - without this the requester would have nothing on
    screen at all, which is the state the old instant-join left them in. Carries
    the name and nothing else: they are not in the space, so there is nothing
    else of it they are entitled to.
    """

    space_id: uuid.UUID
    space_name: str
    created_at: int


async def list_my_requests(db: AsyncSession, requesting_user_id: str) -> list[MyJoinRequestOut]:
    uid = uuid.UUID(requesting_user_id)
    rows = (
        await db.execute(
            select(SpaceJoinRequest, Space.name)
            .join(Space, Space.id == SpaceJoinRequest.space_id)
            .where(SpaceJoinRequest.user_id == uid, SpaceJoinRequest.status == "pending")
            .order_by(SpaceJoinRequest.created_at)
        )
    ).all()
    return [
        MyJoinRequestOut(space_id=r.space_id, space_name=name, created_at=r.created_at)
        for r, name in rows
    ]


async def _pending_row(
    db: AsyncSession, space_id: uuid.UUID, request_id: uuid.UUID
) -> SpaceJoinRequest:
    r = await db.scalar(
        select(SpaceJoinRequest).where(
            SpaceJoinRequest.id == request_id, SpaceJoinRequest.space_id == space_id
        )
    )
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if r.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Request already {r.status}"
        )
    return r


async def approve(
    db: AsyncSession,
    space_id: uuid.UUID,
    request_id: uuid.UUID,
    requesting_user_id: str,
    wrapped_space_keys: str | None,
) -> tuple[Space, SpaceJoinRequest]:
    """Let the requester in, handing over the Space Key in the same breath.

    The wrap is what makes this one step instead of two: the approver is holding
    the ring right now - they are the one clicking - so the membership is created
    with a key already in it and the new member can read the space immediately.
    """
    uid = uuid.UUID(requesting_user_id)
    s = await _require_approver(db, space_id, uid)
    r = await _pending_row(db, space_id, request_id)

    await spaces_service.add_membership(
        db,
        s,
        r.user_id,
        wrapped_space_keys=wrapped_space_keys,
        wrapped_by=uid if wrapped_space_keys else None,
    )
    r.status = "approved"
    r.decided_at = _now_ms()
    r.decided_by = uid
    # The wrap has moved into the membership row; a spent request has no reason
    # to keep key material around.
    r.wrapped_space_keys = None
    r.wrapped_by = None
    await db.commit()
    return s, r


async def decline(
    db: AsyncSession, space_id: uuid.UUID, request_id: uuid.UUID, requesting_user_id: str
) -> tuple[Space, SpaceJoinRequest]:
    """Turn a request down. The row stays, which is what keeps the code spent."""
    uid = uuid.UUID(requesting_user_id)
    s = await _require_approver(db, space_id, uid)
    r = await _pending_row(db, space_id, request_id)
    r.status = "declined"
    r.decided_at = _now_ms()
    r.decided_by = uid
    await db.commit()
    return s, r
