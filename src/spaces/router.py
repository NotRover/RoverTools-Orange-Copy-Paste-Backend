import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import realtime as rt
from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.spaces import invites as invites_module
from src.spaces import join_requests as join_requests_module
from src.spaces import service
from src.spaces.models import Space
from src.spaces.schemas import (
    CommentCountOut,
    CommentOut,
    CreateCommentRequest,
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


@router.get(
    "/my-join-requests", response_model=list[join_requests_module.MyJoinRequestOut]
)
async def list_my_join_requests(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Spaces the caller has asked to join and is still waiting on.

    Declared above `/{space_id}` deliberately: that route takes a UUID, so a
    literal path segment declared after it is shadowed into a 422.

    A pending request is not a membership, so none of these spaces appear in
    `GET /spaces`. This is the only thing standing between the requester and a
    blank screen while they wait.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    return await join_requests_module.list_my_requests(db, user_id)


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
    """Change a space's owner-only settings (owner action).

    Two independent switches: whether joiners see the back catalogue, and
    whether members may approve join requests or only the owner. Both are
    optional, so setting one never clobbers the other.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:history_opened` when the change gave members access to entries
    from before they joined - their pull cursor is past those rows, so nothing
    would fetch them otherwise.
    """
    user_id, _ = current
    _, opened = await service.update_space(db, space_id, user_id, body)
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
    """Ask to join a space using its invite code.

    The code introduces a space; it does not authorise entry to one. This raises
    a join request that somebody already inside approves, and the approval is
    what hands over the Space Key. Re-pasting a code you already used is a no-op,
    and a code you were already turned down on stays turned down.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:join_requested` to whoever may approve it.
    """
    user_id, _ = current
    space, row, outcome = await join_requests_module.request_join(db, user_id, body.invite_code)

    if outcome == "member":
        # Nothing to announce: they are already in. Reported as pending because
        # from the caller's side there is nothing left to wait for either way,
        # and the next space list will show them the space.
        return JoinResponse(status="pending", space_name=space.name)
    if outcome == "declined":
        return JoinResponse(status="declined", space_name=space.name)

    if row is not None:
        # Only the people who can act on it, which is why the channel is chosen
        # here rather than inside the publisher.
        channel = (
            f"space:{space.id}" if space.members_can_approve else f"user:{space.owner_id}"
        )
        await rt.publish_join_requested(
            redis,
            channel,
            {
                "space_id": str(space.id),
                "request_id": str(row.id),
                "user_id": str(row.user_id),
            },
        )

    return JoinResponse(status="pending", space_name=space.name)


@router.get("/{space_id}/join-requests", response_model=list[join_requests_module.JoinRequestOut])
async def list_join_requests(
    space_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Pending join requests on this space.

    Anyone who may approve, which is the owner and - if the owner said so -
    members. Each row carries the requester's identity public key, so the
    approval can wrap the Space Key in the same action.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    return await join_requests_module.list_requests(db, space_id, user_id)


@router.post("/{space_id}/join-requests/{request_id}/approve", status_code=204)
async def approve_join_request(
    space_id: uuid.UUID,
    request_id: uuid.UUID,
    body: join_requests_module.ApproveJoinRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Let the requester in, handing over the Space Key in the same call.

    The approver is holding the keyring at this moment - they are the one
    clicking - so the membership is created with a key already in it and the new
    member can read the space straight away.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:membership_changed` to the space and the new member, and
    `space:join_decided` to them as well.
    """
    user_id, _ = current
    _, row = await join_requests_module.approve(
        db, space_id, request_id, user_id, body.wrapped_space_keys
    )
    joiner = str(row.user_id)
    await rt.publish_space_membership_changed(redis, str(space_id), "joined", joiner)
    # Their socket isn't subscribed to the space channel yet - channel sets are
    # resolved at connect time - so both of these go to them directly.
    await rt.publish_membership_changed_to_user(redis, joiner, str(space_id), "joined")
    await rt.publish_join_decided(redis, joiner, str(space_id), "approved")


@router.post("/{space_id}/join-requests/{request_id}/decline", status_code=204)
async def decline_join_request(
    space_id: uuid.UUID,
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Turn a join request down.

    The row is kept rather than deleted: together with the one-row-per-person
    rule it is what stops somebody who still holds the code from knocking again.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:join_decided` to the requester.
    """
    user_id, _ = current
    _, row = await join_requests_module.decline(db, space_id, request_id, user_id)
    await rt.publish_join_decided(redis, str(row.user_id), str(space_id), "declined")


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
    """Take a shared entry down from a space.

    Allowed for the space owner (any entry) and for the member who shared it
    (their own). Moderation, not deletion: the space id and its wrapped key copy
    are dropped from the entry. The author keeps their personal copy.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:entry_removed` to the space channel.
    """
    user_id, device_id = current
    author_id = await service.remove_entry_from_space(db, space_id, client_id, entry_type, user_id)
    # This device already dropped its copy when it issued the call.
    await rt.publish_space_entry_removed(
        redis,
        str(space_id),
        client_id,
        entry_type,
        author_id,
        removed_by=user_id,
        origin_device=device_id,
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

    Any member of the space may call this, not only the owner - see
    `service.distribute_keys` for why that costs nothing and what it fixes. The
    fingerprint in the body is still owner-only.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:rekey` to each target member's user channel.
    """
    user_id, _ = current
    written = await service.distribute_keys(db, space_id, user_id, body)
    # Announce only what was stored. A keyring for someone who has since left is
    # dropped by the service, and telling them to reconcile would send them
    # looking for a space they are no longer in.
    stored = {str(u) for u in written}
    for entry in body.wrapped_keyrings:
        if str(entry.user_id) in stored:
            await rt.publish_space_rekey(
                redis, str(space_id), str(entry.user_id), entry.wrapped_space_keys
            )


# ── Comments ──────────────────────────────────────────────────────────────────


@router.post("/{space_id}/comments", response_model=CommentOut, status_code=201)
async def add_space_comment(
    space_id: uuid.UUID,
    body: CreateCommentRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Comment on an entry shared into a space. Any member may comment.

    The body arrives encrypted and leaves encrypted: it is stored as given and
    echoed to the space channel as given, so the fan-out costs no extra fetch
    and the server learns nothing either way.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:comment` (action `created`) to the space channel.
    """
    user_id, device_id = current
    row = await service.add_comment(db, space_id, user_id, body)
    out = CommentOut.model_validate(row)
    # The posting device already has it on screen.
    await rt.publish_space_comment(redis, str(space_id), "created", out.model_dump(mode="json"), device_id)
    return out


@router.get("/{space_id}/comments", response_model=list[CommentOut])
async def list_space_comments(
    space_id: uuid.UUID,
    client_id: str,
    entry_type: str = "clipboard",
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """One entry's thread, oldest first.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    rows = await service.list_comments(db, space_id, user_id, client_id, entry_type)
    return [CommentOut.model_validate(r) for r in rows]


@router.get("/{space_id}/comments/counts", response_model=list[CommentCountOut])
async def list_space_comment_counts(
    space_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Comment tallies for every commented-on entry in the space, so a feed can
    draw its chips in one request instead of one per card.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    return await service.comment_counts(db, space_id, user_id)


@router.delete("/{space_id}/comments/{comment_id}", status_code=204)
async def delete_space_comment(
    space_id: uuid.UUID,
    comment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Delete a comment: its author, or the space owner as moderator.

    Requires: Bearer token + X-Device-Id header.
    Emits `space:comment` (action `deleted`) to the space channel.
    """
    user_id, device_id = current
    row = await service.delete_comment(db, space_id, comment_id, user_id)
    await rt.publish_space_comment(
        redis,
        str(space_id),
        "deleted",
        {
            "id": str(row.id),
            "space_id": str(row.space_id),
            "client_id": row.client_id,
            "entry_type": row.entry_type,
            "author_id": str(row.author_id),
            "deleted_by": user_id,
        },
        device_id,
    )
