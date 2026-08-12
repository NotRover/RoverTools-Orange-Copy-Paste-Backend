"""Addressed group invites — the persistent counterpart to bearer invite codes.

An invite targets one email, survives the invitee being offline, and gives the
inviter visibility into its fate. Both pool groups and Live Share sessions use
it: creating one publishes `invite:received` to the invitee (when their profile
is known) and sends a best-effort email carrying the group's short code as the
fallback join path. Accepting joins the group directly by invite id.

Status lifecycle: pending → accepted | declined (invitee) | revoked (inviter).
Expiry is judged against `expires_at` at read/accept time rather than by a
background transition, so no sweeper is needed.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import email, realtime as rt
from src.auth.models import Profile
from src.database import get_db
from src.dependencies import get_current_claims, get_current_user_id, get_redis
from src.groups import service as groups_service
from src.groups.models import Group, GroupInvite, GroupMembership

_INVITE_TTL_HOURS = 72


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


# ── Schemas ─────────────────────────────────────────────────────────────


class CreateInviteRequest(BaseModel):
    email: EmailStr


class InviteOut(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    group_name: str
    group_type: str
    inviter_id: uuid.UUID
    inviter_name: str
    invitee_email: str
    status: str
    created_at: int
    expires_at: int

    model_config = {"from_attributes": True}


class InviteListResponse(BaseModel):
    sent: list[InviteOut]
    received: list[InviteOut]


class AcceptResponse(BaseModel):
    group_id: uuid.UUID
    name: str
    group_type: str


# ── Service ─────────────────────────────────────────────────────────────


async def _invite_to_out(db: AsyncSession, inv: GroupInvite, g: Group | None = None) -> InviteOut:
    if g is None:
        g = await db.scalar(select(Group).where(Group.id == inv.group_id))
    inviter = await db.scalar(select(Profile).where(Profile.id == inv.inviter_id))
    return InviteOut(
        id=inv.id,
        group_id=inv.group_id,
        group_name=g.name if g else "",
        group_type=g.group_type if g else "pool",
        inviter_id=inv.inviter_id,
        inviter_name=inviter.display_name if inviter else "",
        invitee_email=inv.invitee_email,
        status=inv.status,
        created_at=inv.created_at,
        expires_at=inv.expires_at,
    )


async def create_invite(
    db: AsyncSession,
    redis: Redis,
    background: BackgroundTasks,
    group: Group,
    inviter_id: str,
    invitee_email: str,
) -> InviteOut:
    """Create (or refresh) a pending invite from `inviter_id` to `invitee_email`
    for `group`, notify the invitee over WS when resolvable, and queue the
    invite email. Callers have already verified the inviter may invite."""
    now = _now_ms()
    normalized = invitee_email.lower()
    inviter_uuid = uuid.UUID(inviter_id)

    invitee = await db.scalar(select(Profile).where(Profile.email == normalized))
    if invitee and invitee.id == inviter_uuid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That's your own email")

    if invitee:
        already_member = await db.scalar(
            select(GroupMembership).where(
                GroupMembership.group_id == group.id, GroupMembership.user_id == invitee.id
            )
        )
        if already_member:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already a member")

    # One live invite per (group, email): refresh the pending row instead of
    # stacking duplicates the invitee would see as repeated notifications.
    invite = await db.scalar(
        select(GroupInvite).where(
            GroupInvite.group_id == group.id,
            GroupInvite.invitee_email == normalized,
            GroupInvite.status == "pending",
        )
    )
    expires_at = int((datetime.now(UTC) + timedelta(hours=_INVITE_TTL_HOURS)).timestamp() * 1000)
    if invite:
        invite.expires_at = expires_at
        invite.invitee_user_id = invitee.id if invitee else None
    else:
        invite = GroupInvite(
            group_id=group.id,
            inviter_id=inviter_uuid,
            invitee_email=normalized,
            invitee_user_id=invitee.id if invitee else None,
            status="pending",
            created_at=now,
            expires_at=expires_at,
        )
        db.add(invite)
    await db.commit()
    await db.refresh(invite)

    out = await _invite_to_out(db, invite, group)

    if invitee:
        await rt.publish_invite_received(redis, str(invitee.id), out.model_dump(mode="json"))

    inviter = await db.scalar(select(Profile).where(Profile.id == inviter_uuid))
    code = groups_service.format_invite_code(group.invite_code or "")
    background.add_task(
        email.send_sharing_invite, normalized, "", inviter.display_name if inviter else "", code
    )
    return out


def _is_expired(inv: GroupInvite) -> bool:
    return inv.expires_at < _now_ms()


async def _get_invite_for_invitee(db: AsyncSession, invite_id: uuid.UUID, claims: dict) -> GroupInvite:
    inv = await db.scalar(select(GroupInvite).where(GroupInvite.id == invite_id))
    if not inv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    uid = uuid.UUID(claims["sub"])
    caller_email = (claims.get("email") or "").lower()
    if inv.invitee_user_id != uid and (not caller_email or inv.invitee_email != caller_email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your invite")
    if inv.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Invite already {inv.status}")
    if _is_expired(inv):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite expired")
    return inv


# ── Router ──────────────────────────────────────────────────────────────

router = APIRouter(prefix="/invites", tags=["invites"])


@router.get("", response_model=InviteListResponse)
async def list_invites(
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(get_current_claims),
):
    """List the caller's pending received invites and their sent invites.

    Received invites match by resolved user id or by the token's email claim;
    sent invites include recent non-pending ones so the inviter sees outcomes.

    Requires: Bearer token (Supabase JWT).
    """
    uid = uuid.UUID(claims["sub"])
    caller_email = (claims.get("email") or "").lower()
    now = _now_ms()

    received_rows = (
        await db.scalars(
            select(GroupInvite).where(
                GroupInvite.status == "pending",
                GroupInvite.expires_at > now,
                (GroupInvite.invitee_user_id == uid)
                | (GroupInvite.invitee_email == caller_email if caller_email else False),
            )
        )
    ).all()

    sent_rows = (
        await db.scalars(
            select(GroupInvite)
            .where(GroupInvite.inviter_id == uid)
            .order_by(GroupInvite.created_at.desc())
            .limit(50)
        )
    ).all()

    return InviteListResponse(
        sent=[await _invite_to_out(db, i) for i in sent_rows],
        received=[await _invite_to_out(db, i) for i in received_rows],
    )


@router.post("/{invite_id}/accept", response_model=AcceptResponse)
async def accept_invite(
    invite_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    claims: dict = Depends(get_current_claims),
):
    """Accept an invite addressed to the caller and join its group.

    Emits the same events as an invite-code join (`sharing:accepted` for
    live_share, `group:membership_changed` otherwise) plus `invite:updated`
    to the inviter.

    Requires: Bearer token (Supabase JWT).
    """
    inv = await _get_invite_for_invitee(db, invite_id, claims)
    uid = uuid.UUID(claims["sub"])

    g = await db.scalar(select(Group).where(Group.id == inv.group_id))
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group no longer exists")

    await groups_service.add_membership(db, g, uid)
    inv.status = "accepted"
    inv.invitee_user_id = uid
    await db.commit()

    joining_user = await db.scalar(select(Profile).where(Profile.id == uid))
    user_id = str(uid)
    if g.group_type == "live_share":
        await rt.publish_sharing_accepted(
            redis,
            owner_user_id=str(g.owner_id),
            share_group_id=str(g.id),
            new_member={
                "id": user_id,
                "display_name": joining_user.display_name if joining_user else "",
                "identity_pubkey": joining_user.identity_pubkey if joining_user else None,
            },
            wrapped_group_key=None,
        )
    else:
        await rt.publish_group_membership_changed(redis, str(g.id), "joined", user_id)
    await rt.publish_membership_changed_to_user(redis, user_id, str(g.id), "joined")
    await rt.publish_invite_updated(redis, str(inv.inviter_id), str(inv.id), "accepted", str(g.id))

    return AcceptResponse(group_id=g.id, name=g.name, group_type=g.group_type)


@router.post("/{invite_id}/decline", status_code=204)
async def decline_invite(
    invite_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    claims: dict = Depends(get_current_claims),
):
    """Decline an invite addressed to the caller.

    Emits `invite:updated` to the inviter.

    Requires: Bearer token (Supabase JWT).
    """
    inv = await _get_invite_for_invitee(db, invite_id, claims)
    inv.status = "declined"
    inv.invitee_user_id = uuid.UUID(claims["sub"])
    await db.commit()
    await rt.publish_invite_updated(redis, str(inv.inviter_id), str(inv.id), "declined", str(inv.group_id))


@router.delete("/{invite_id}", status_code=204)
async def revoke_invite(
    invite_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Revoke a pending invite the caller sent.

    Emits `invite:updated` to the invitee when they're a known user.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    inv = await db.scalar(select(GroupInvite).where(GroupInvite.id == invite_id))
    if not inv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    if str(inv.inviter_id) != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your invite")
    if inv.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Invite already {inv.status}")
    inv.status = "revoked"
    await db.commit()
    if inv.invitee_user_id:
        await rt.publish_invite_updated(
            redis, str(inv.invitee_user_id), str(inv.id), "revoked", str(inv.group_id)
        )
