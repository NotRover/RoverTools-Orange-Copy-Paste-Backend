"""Addressed space invites — the persistent counterpart to bearer invite codes.

An invite targets one email that already belongs to an account - an address
nobody has signed up with is rejected, since the invite has nowhere to appear
and no device key to wrap the space key to. It survives the invitee being
offline and gives the inviter visibility into its fate. Creating one publishes
`invite:received` to the invitee and sends a best-effort email carrying the
space's short code as the fallback join path. Accepting joins the space directly
by invite id.

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
from src.spaces import service as spaces_service
from src.spaces.models import Space, SpaceInvite, SpaceMembership

_INVITE_TTL_HOURS = 72


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


# ── Schemas ─────────────────────────────────────────────────────────────


class CreateInviteRequest(BaseModel):
    email: EmailStr


class InviteOut(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    inviter_id: uuid.UUID
    inviter_name: str
    invitee_email: str
    status: str
    created_at: int
    expires_at: int
    # The invitee's X25519 identity public key, so the inviter's client can wrap
    # the Space Key for them before they are a member. Sent only to the inviter
    # (see `list_invites`) - it is public key material either way, but there is no
    # reason for it to travel further than the one caller that uses it.
    invitee_identity_pubkey: str | None = None
    # Whether a wrapped keyring is already attached, waiting for the accept.
    has_space_key: bool = False

    model_config = {"from_attributes": True}


class AttachKeyRequest(BaseModel):
    # JSON array of X25519-wrapped Space Keys, newest first, wrapped for the
    # invitee's identity key. Opaque to the server.
    wrapped_space_keys: str


class InviteListResponse(BaseModel):
    sent: list[InviteOut]
    received: list[InviteOut]


class AcceptResponse(BaseModel):
    space_id: uuid.UUID
    name: str


# ── Service ─────────────────────────────────────────────────────────────


async def _invite_to_out(
    db: AsyncSession, inv: SpaceInvite, s: Space | None = None, *, for_inviter: bool = False
) -> InviteOut:
    if s is None:
        s = await db.scalar(select(Space).where(Space.id == inv.space_id))
    inviter = await db.scalar(select(Profile).where(Profile.id == inv.inviter_id))
    invitee_pubkey = None
    if for_inviter and inv.invitee_user_id is not None:
        invitee_pubkey = await db.scalar(
            select(Profile.identity_pubkey).where(Profile.id == inv.invitee_user_id)
        )
    return InviteOut(
        id=inv.id,
        space_id=inv.space_id,
        space_name=s.name if s else "",
        inviter_id=inv.inviter_id,
        inviter_name=inviter.display_name if inviter else "",
        invitee_email=inv.invitee_email,
        status=inv.status,
        created_at=inv.created_at,
        expires_at=inv.expires_at,
        invitee_identity_pubkey=invitee_pubkey,
        has_space_key=inv.wrapped_space_keys is not None,
    )


async def create_invite(
    db: AsyncSession,
    redis: Redis,
    background: BackgroundTasks,
    space: Space,
    inviter_id: str,
    invitee_email: str,
) -> InviteOut:
    """Create (or refresh) a pending invite from `inviter_id` to `invitee_email`
    for `space`, notify the invitee over WS when resolvable, and queue the
    invite email. Callers have already verified the inviter may invite."""
    now = _now_ms()
    normalized = invitee_email.lower()
    inviter_uuid = uuid.UUID(inviter_id)

    invitee = await db.scalar(select(Profile).where(Profile.email == normalized))
    if invitee is None:
        # An invite is an addressed offer: it has to reach an inbox in the app,
        # and the space key has to be wrapped to a real device key. Neither is
        # possible for an address nobody has signed up with, so the row would
        # sit pending until it expired.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No RoverTools account uses that email",
        )
    if invitee.id == inviter_uuid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That's your own email")

    already_member = await db.scalar(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space.id, SpaceMembership.user_id == invitee.id
        )
    )
    if already_member:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already a member")

    # One live invite per (space, email): refresh the pending row instead of
    # stacking duplicates the invitee would see as repeated notifications.
    invite = await db.scalar(
        select(SpaceInvite).where(
            SpaceInvite.space_id == space.id,
            SpaceInvite.invitee_email == normalized,
            SpaceInvite.status == "pending",
        )
    )
    expires_at = int((datetime.now(UTC) + timedelta(hours=_INVITE_TTL_HOURS)).timestamp() * 1000)
    if invite:
        invite.expires_at = expires_at
        invite.invitee_user_id = invitee.id
        # A re-invite starts the handover again: the ring may have rotated since,
        # and attaching the new one is the inviter's next call.
        invite.wrapped_space_keys = None
    else:
        invite = SpaceInvite(
            space_id=space.id,
            inviter_id=inviter_uuid,
            invitee_email=normalized,
            invitee_user_id=invitee.id,
            status="pending",
            created_at=now,
            expires_at=expires_at,
        )
        db.add(invite)
    await db.commit()
    await db.refresh(invite)

    out = await _invite_to_out(db, invite, space, for_inviter=True)

    # The invitee's copy does not carry their own public key back to them, nor the
    # inviter's wrap state - both are the inviter's business.
    await rt.publish_invite_received(
        redis,
        str(invitee.id),
        out.model_dump(mode="json", exclude={"invitee_identity_pubkey", "has_space_key"}),
    )

    inviter = await db.scalar(select(Profile).where(Profile.id == inviter_uuid))
    code = spaces_service.format_invite_code(space.invite_code or "")
    background.add_task(
        email.send_sharing_invite, normalized, "", inviter.display_name if inviter else "", code
    )
    return out


def _is_expired(inv: SpaceInvite) -> bool:
    return inv.expires_at < _now_ms()


async def _get_invite_for_invitee(db: AsyncSession, invite_id: uuid.UUID, claims: dict) -> SpaceInvite:
    inv = await db.scalar(select(SpaceInvite).where(SpaceInvite.id == invite_id))
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
            select(SpaceInvite).where(
                SpaceInvite.status == "pending",
                SpaceInvite.expires_at > now,
                (SpaceInvite.invitee_user_id == uid)
                | (SpaceInvite.invitee_email == caller_email if caller_email else False),
            )
        )
    ).all()

    sent_rows = (
        await db.scalars(
            select(SpaceInvite)
            .where(SpaceInvite.inviter_id == uid)
            .order_by(SpaceInvite.created_at.desc())
            .limit(50)
        )
    ).all()

    return InviteListResponse(
        sent=[await _invite_to_out(db, i, for_inviter=True) for i in sent_rows],
        received=[await _invite_to_out(db, i) for i in received_rows],
    )


@router.put("/{invite_id}/key", status_code=204)
async def attach_invite_key(
    invite_id: uuid.UUID,
    body: AttachKeyRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(get_current_claims),
):
    """Attach the Space Key, wrapped for the invitee, to a pending invite.

    The inviter's client calls this right after creating the invite, using the
    `invitee_identity_pubkey` the create returned. The wrap moves into the
    membership row when the invite is accepted, which is what lets a new member
    read the space immediately rather than waiting for another member's app to be
    running.

    Inviter only, and only while the invite is pending: a spent invite has already
    handed over whatever it carried.

    Requires: Bearer token (Supabase JWT).
    """
    uid = uuid.UUID(claims["sub"])
    inv = await db.scalar(select(SpaceInvite).where(SpaceInvite.id == invite_id))
    if not inv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    if inv.inviter_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your invite")
    if inv.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Invite already {inv.status}"
        )
    inv.wrapped_space_keys = body.wrapped_space_keys
    await db.commit()


@router.post("/{invite_id}/accept", response_model=AcceptResponse)
async def accept_invite(
    invite_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    claims: dict = Depends(get_current_claims),
):
    """Accept an invite addressed to the caller and join its space.

    Emits the same events as an invite-code join (`space:membership_changed`)
    plus `invite:updated` to the inviter.

    Requires: Bearer token (Supabase JWT).
    """
    inv = await _get_invite_for_invitee(db, invite_id, claims)
    uid = uuid.UUID(claims["sub"])

    s = await db.scalar(select(Space).where(Space.id == inv.space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space no longer exists")

    # The inviter may have wrapped the keyring for this account already, in which
    # case the membership starts with a key and this member can read the space
    # straight away - nobody else has to be online.
    await spaces_service.add_membership(
        db, s, uid, wrapped_space_keys=inv.wrapped_space_keys, wrapped_by=inv.inviter_id
    )
    inv.status = "accepted"
    inv.invitee_user_id = uid
    # The wrap has moved to the membership row; leaving a copy on a spent invite
    # keeps key material around for no reason.
    inv.wrapped_space_keys = None
    await db.commit()

    user_id = str(uid)
    await rt.publish_space_membership_changed(redis, str(s.id), "joined", user_id)
    await rt.publish_membership_changed_to_user(redis, user_id, str(s.id), "joined")
    await rt.publish_invite_updated(redis, str(inv.inviter_id), str(inv.id), "accepted", str(s.id))

    return AcceptResponse(space_id=s.id, name=s.name)


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
    await rt.publish_invite_updated(redis, str(inv.inviter_id), str(inv.id), "declined", str(inv.space_id))


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
    inv = await db.scalar(select(SpaceInvite).where(SpaceInvite.id == invite_id))
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
            redis, str(inv.invitee_user_id), str(inv.id), "revoked", str(inv.space_id)
        )
