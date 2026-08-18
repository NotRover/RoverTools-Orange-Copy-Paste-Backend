import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import realtime as rt
from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.spaces import invites as invites_module
from src.spaces import service
from src.spaces.models import Space
from src.spaces.schemas import (
    CreateSpaceRequest,
    CreateSpaceResponse,
    DistributeKeysRequest,
    JoinRequest,
    JoinResponse,
    SpaceOut,
    UpdateSpaceRequest,
)

router = APIRouter(prefix="/spaces", tags=["spaces"])


@router.post("", response_model=CreateSpaceResponse, status_code=201)
async def create_space(
    body: CreateSpaceRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Create a new space owned by the current user and return its invite code.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    space, invite_code = await service.create_space(db, user_id, body)
    return CreateSpaceResponse(space_id=space.id, invite_code=invite_code)


@router.get("", response_model=list[SpaceOut])
async def list_spaces(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """List all spaces the current user is a member of, with live member presence.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    return await service.list_spaces(db, redis, user_id)


@router.get("/{space_id}", response_model=SpaceOut)
async def get_space(
    space_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Return details for a single space the current user belongs to.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    return await service.get_space(db, redis, space_id, user_id)


@router.patch("/{space_id}", response_model=SpaceOut)
async def update_space(
    space_id: uuid.UUID,
    body: UpdateSpaceRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Change a space's history policy (owner action).

    Requires: Bearer token + X-Device-Id header.
    Emits `space:history_opened` when the change gave members access to entries
    from before they joined - their pull cursor is past those rows, so nothing
    would fetch them otherwise.
    """
    user_id, _ = current
    _, opened = await service.set_share_history(db, space_id, user_id, body)
    if opened:
        await rt.publish_space_history_opened(redis, str(space_id))
    return await service.get_space(db, redis, space_id, user_id)


@router.post("/join", response_model=JoinResponse)
async def join_space(
    body: JoinRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Join a space using an invite code.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:membership_changed` to the space channel and to the joiner.
    """
    user_id, _ = current
    space = await service.join_space(db, user_id, body.invite_code)

    await rt.publish_space_membership_changed(redis, str(space.id), "joined", user_id)
    # The joiner's own socket isn't subscribed to the space channel yet (channel
    # sets are resolved at connect time), so tell them directly — their client
    # resubscribes and reconciles keys on this event.
    await rt.publish_membership_changed_to_user(redis, user_id, str(space.id), "joined")

    return JoinResponse(space_id=space.id, name=space.name)


@router.delete("/{space_id}/members/{member_user_id}", status_code=204)
async def remove_member(
    space_id: uuid.UUID,
    member_user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Remove a member from a space (owner action) or leave it (self action).

    Clears every remaining member's wrapped keyring so the owner's client
    rotates the Space Key — the departed member must not read new entries.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:membership_changed` to the space channel and to the removed member.
    """
    user_id, _ = current
    await service.remove_member(db, space_id, member_user_id, user_id)
    await rt.publish_space_membership_changed(redis, str(space_id), "left", str(member_user_id))
    # Removed members are (or may be) no longer on the space channel — notify
    # them directly so their client drops the space and resubscribes.
    await rt.publish_membership_changed_to_user(redis, str(member_user_id), str(space_id), "left")


@router.delete("/{space_id}/entries/{client_id}", status_code=204)
async def remove_space_entry(
    space_id: uuid.UUID,
    client_id: str,
    entry_type: str = "clipboard",
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Take a shared entry down from a space (owner action).

    Moderation, not deletion: the space id and its wrapped key copy are dropped
    from the entry. The author keeps their personal copy.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:entry_removed` to the space channel.
    """
    user_id, device_id = current
    author_id = await service.remove_entry_from_space(db, space_id, client_id, entry_type, user_id)
    # This device already dropped its copy when it issued the call.
    await rt.publish_space_entry_removed(
        redis, str(space_id), client_id, entry_type, author_id, origin_device=device_id
    )


@router.delete("/{space_id}", status_code=204)
async def delete_space(
    space_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Delete a space the current user owns.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:membership_changed` (action `deleted`) before the row goes away,
    while the space channel still has subscribers.
    """
    user_id, _ = current
    # Publish first: after the delete commits, nobody is left on the channel.
    await rt.publish_space_membership_changed(redis, str(space_id), "deleted", user_id)
    await service.delete_space(db, space_id, user_id)


@router.post("/{space_id}/invites", response_model=invites_module.InviteOut, status_code=201)
async def send_space_invite(
    space_id: uuid.UUID,
    body: invites_module.CreateInviteRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Send an addressed invite to join this space (owner action).

    Requires: Bearer token + X-Device-Id header.
    Emits `invite:received` to the invitee when they're a known user, and
    queues a best-effort invite email carrying the space's short code.
    """
    user_id, _ = current
    s = await db.scalar(select(Space).where(Space.id == space_id))
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")
    if str(s.owner_id) != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner only")
    return await invites_module.create_invite(db, redis, background, s, user_id, body.email)


@router.post("/{space_id}/keys", status_code=204)
async def distribute_keys(
    space_id: uuid.UUID,
    body: DistributeKeysRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Distribute per-member wrapped keyrings after a membership or rekey event.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:rekey` to each target member's user channel.
    """
    user_id, _ = current
    await service.distribute_keys(db, space_id, user_id, body)
    for entry in body.wrapped_keyrings:
        await rt.publish_space_rekey(redis, str(space_id), str(entry.user_id), entry.wrapped_space_keys)
