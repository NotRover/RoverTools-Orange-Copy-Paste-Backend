import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import schemas, service
from src.auth.jwt import create_access_token, create_refresh_token
from src.config import settings
from src.dependencies import get_current_user_id, get_redis
from src.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=schemas.RegisterResponse, status_code=201)
async def register(body: schemas.RegisterRequest, db: AsyncSession = Depends(get_db)):
    user = await service.register_user(db, body)
    return schemas.RegisterResponse(user_id=user.id)


@router.post("/login", response_model=schemas.LoginResponse)
async def login(body: schemas.LoginRequest, db: AsyncSession = Depends(get_db)):
    user, device = await service.login_user(db, body)

    access_token, _jti = create_access_token(str(user.id), str(device.id))
    refresh_token = create_refresh_token()
    await service.store_refresh_token(db, device, refresh_token)

    return schemas.LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        device_id=device.id,
        kdf_salt=user.kdf_salt,
        user=schemas.UserOut.model_validate(user),
    )


@router.post("/refresh", response_model=schemas.RefreshResponse)
async def refresh(body: schemas.RefreshRequest, db: AsyncSession = Depends(get_db)):
    user, device = await service.rotate_refresh_token(db, body.device_id, body.refresh_token)

    new_access, _jti = create_access_token(str(user.id), str(device.id))
    new_refresh = create_refresh_token()
    await service.store_refresh_token(db, device, new_refresh)

    return schemas.RefreshResponse(access_token=new_access, refresh_token=new_refresh)


@router.post("/logout", status_code=204)
async def logout(
    body: dict,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    device_id = body.get("device_id")
    if device_id:
        await service.revoke_device(db, uuid.UUID(str(device_id)), user_id)


@router.delete("/devices/{device_id}", status_code=204)
async def revoke_device(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    await service.revoke_device(db, device_id, user_id)


@router.get("/devices", response_model=list[schemas.DeviceOut])
async def list_devices(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, device_id = current
    devices = await service.get_user_devices(db, user_id, device_id)
    return [schemas.DeviceOut(**d) for d in devices]


@router.post("/keys/register", status_code=204)
async def register_keys(
    body: schemas.RegisterPublicKeysRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, device_id = current
    await service.store_public_keys(db, user_id, device_id, body.identity_pubkey, body.device_pubkey)


@router.post("/devices/{device_id}/key-wrap", status_code=204)
async def wrap_device_umk(
    device_id: uuid.UUID,
    body: schemas.WrapUmkRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    await service.store_wrapped_umk(db, device_id, user_id, body.wrapped_umk)


@router.post("/password-reset/request", status_code=204)
async def password_reset_request(body: schemas.PasswordResetRequestBody):
    # TODO: queue Celery email task (Phase 7)
    pass


@router.post("/password-reset/confirm", status_code=204)
async def password_reset_confirm(body: schemas.PasswordResetConfirmBody):
    # TODO: validate token, update password hash (Phase 7)
    pass
