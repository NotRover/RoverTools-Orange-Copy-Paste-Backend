from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.realtime import pubsub as rt
from src.settings import service
from src.settings.schemas import SettingsOut, SettingsPutRequest, SettingsPutResponse

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
async def get_settings(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    row = await service.get_settings(db, user_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No settings stored yet")
    return SettingsOut(encrypted_blob=row.encrypted_blob, updated_at=row.updated_at)


@router.put("", response_model=SettingsPutResponse)
async def put_settings(
    body: SettingsPutRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, device_id = current
    result = await service.put_settings(db, user_id, body)

    if result.winner == "client":
        # Notify other devices that settings changed
        await rt.publish_settings_updated(redis, user_id, result.updated_at)

    return result
