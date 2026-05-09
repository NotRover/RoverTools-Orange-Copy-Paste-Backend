import uuid

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import User
from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.realtime import pubsub as rt
from src.sharing import service
from src.sharing.schemas import (
    InviteRequest,
    InviteResponse,
    ScopeUpdateRequest,
    SessionOut,
)

router = APIRouter(prefix="/sharing", tags=["sharing"])


@router.post("/invite", response_model=InviteResponse, status_code=201)
async def create_invite(
    body: InviteRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    result, invitee = await service.create_invite(db, user_id, body.email, body.share_scope)

    # Fetch the inviting owner's display_name for the WS payload
    owner = await db.scalar(select(User).where(User.id == uuid.UUID(user_id)))
    from_user = {
        "id": user_id,
        "display_name": owner.display_name if owner else "",
    }

    # Push real-time invite to the invitee if they're already a registered user
    if invitee:
        await rt.publish_sharing_invite(
            redis,
            invitee_user_id=str(invitee.id),
            share_group_id=str(result.share_group_id),
            from_user=from_user,
            invite_code=result.invite_code,
            expires_at=result.expires_at,
        )
        # Queue invite email
        from src.worker.tasks.email import send_sharing_invite_email
        send_sharing_invite_email.delay(
            invitee.email,
            invitee.display_name,
            from_user["display_name"],
            result.invite_code,
        )

    return result


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    return await service.list_sessions(db, user_id)


@router.patch("/sessions/{share_group_id}/scope", status_code=204)
async def update_scope(
    share_group_id: uuid.UUID,
    body: ScopeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    await service.update_scope(db, share_group_id, user_id, body)
    await rt.publish_sharing_scope_changed(redis, str(share_group_id), user_id, body.share_scope)


@router.delete("/sessions/{share_group_id}", status_code=204)
async def end_session(
    share_group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    # Publish before delete so the group channel still has subscribers
    await rt.publish_sharing_ended(redis, str(share_group_id), user_id)
    await service.end_session(db, share_group_id, user_id)


@router.delete("/sessions/{share_group_id}/leave", status_code=204)
async def leave_session(
    share_group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    leaving_scope = await service.leave_session(db, share_group_id, user_id)
    await rt.publish_sharing_scope_changed(
        redis, str(share_group_id), user_id, leaving_scope
    )
