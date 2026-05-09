import uuid

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import User
from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.groups import service
from src.groups.schemas import (
    CreateGroupRequest,
    CreateGroupResponse,
    DistributeKeysRequest,
    GroupOut,
    InviteRequest,
    InviteResponse,
    JoinRequest,
    JoinResponse,
)
from src.realtime import pubsub as rt

router = APIRouter(prefix="/groups", tags=["groups"])


@router.post("", response_model=CreateGroupResponse, status_code=201)
async def create_group(
    body: CreateGroupRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    group, invite_code = await service.create_group(db, user_id, body)
    return CreateGroupResponse(group_id=group.id, invite_code=invite_code)


@router.get("", response_model=list[GroupOut])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    return await service.list_groups(db, user_id)


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    return await service.get_group(db, group_id, user_id)


@router.post("/{group_id}/invite", response_model=InviteResponse)
async def refresh_invite(
    group_id: uuid.UUID,
    body: InviteRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    return await service.refresh_invite(db, group_id, user_id)


@router.post("/join", response_model=JoinResponse)
async def join_group(
    body: JoinRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    result, group = await service.join_group(db, user_id, body)

    if group.group_type == "live_share":
        # Fetch joining user's display_name for the sharing:accepted payload
        joining_user = await db.scalar(select(User).where(User.id == uuid.UUID(user_id)))
        display_name = joining_user.display_name if joining_user else ""

        await rt.publish_sharing_accepted(
            redis,
            owner_user_id=str(group.owner_id),
            share_group_id=str(result.group_id),
            new_member={"id": user_id, "display_name": display_name},
            wrapped_group_key=body.wrapped_group_key,
        )
    else:
        await rt.publish_group_membership_changed(redis, str(result.group_id), "joined", user_id)

    return result


@router.delete("/{group_id}/members/{member_user_id}", status_code=204)
async def remove_member(
    group_id: uuid.UUID,
    member_user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    await service.remove_member(db, group_id, member_user_id, user_id)
    await rt.publish_group_membership_changed(redis, str(group_id), "left", str(member_user_id))


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    await service.delete_group(db, group_id, user_id)


@router.post("/{group_id}/keys", status_code=204)
async def distribute_keys(
    group_id: uuid.UUID,
    body: DistributeKeysRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    await service.distribute_keys(db, group_id, user_id, body)
    for entry in body.wrapped_keys:
        await rt.publish_group_rekey(redis, str(group_id), str(entry.user_id), entry.wrapped_group_key)
