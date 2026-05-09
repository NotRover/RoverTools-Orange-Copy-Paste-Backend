import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import schemas, service
from src.auth.jwt import create_access_token, create_refresh_token
from src.config import settings
from src.database import get_db
from src.dependencies import get_current_user_id, get_redis
from src.limiter import limiter

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Registration ──────────────────────────────────────────────────────────────


@router.post("/register", response_model=schemas.RegisterResponse, status_code=201)
async def register(
    body: schemas.RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    user = await service.register_user(db, body)

    token = await service.generate_verification_token(redis, str(user.id))
    verify_url = f"{settings.app_base_url}/verify-email?token={token}"

    from src.worker.tasks.email import send_verification_email

    send_verification_email.delay(user.email, user.display_name, verify_url)

    return schemas.RegisterResponse(user_id=user.id)


# ── Email verification ────────────────────────────────────────────────────────


@router.post("/verify-email", response_model=schemas.MessageResponse)
async def verify_email(
    body: schemas.VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await service.verify_email(db, redis, body.token)
    return schemas.MessageResponse(message="Email verified successfully.")


@router.post("/resend-verification", response_model=schemas.MessageResponse, status_code=202)
@limiter.limit("3/hour")
async def resend_verification(
    request: Request,
    body: schemas.ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    from sqlalchemy import select
    from src.auth.models import User

    user = await db.scalar(select(User).where(User.email == body.email))
    if user and not user.email_verified:
        token = await service.generate_verification_token(redis, str(user.id))
        verify_url = f"{settings.app_base_url}/verify-email?token={token}"
        from src.worker.tasks.email import send_verification_email

        send_verification_email.delay(user.email, user.display_name, verify_url)

    # Always return 202 — don't reveal whether email exists
    return schemas.MessageResponse(message="If that address is registered and unverified, a new email has been sent.")


# ── Login ─────────────────────────────────────────────────────────────────────


@router.post("/login", response_model=schemas.LoginResponse)
@limiter.limit("10/minute;30/hour")
async def login(
    request: Request,
    body: schemas.LoginRequest,
    db: AsyncSession = Depends(get_db),
):
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


# ── Token refresh ─────────────────────────────────────────────────────────────


@router.post("/refresh", response_model=schemas.RefreshResponse)
async def refresh(body: schemas.RefreshRequest, db: AsyncSession = Depends(get_db)):
    user, device = await service.rotate_refresh_token(db, body.device_id, body.refresh_token)

    new_access, _jti = create_access_token(str(user.id), str(device.id))
    new_refresh = create_refresh_token()
    await service.store_refresh_token(db, device, new_refresh)

    return schemas.RefreshResponse(access_token=new_access, refresh_token=new_refresh)


# ── Logout ────────────────────────────────────────────────────────────────────


@router.post("/logout", status_code=204)
async def logout(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    device_id = body.get("device_id")
    if device_id:
        await service.revoke_device(db, uuid.UUID(str(device_id)), user_id)


# ── Device management ─────────────────────────────────────────────────────────


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


# ── Public key management ─────────────────────────────────────────────────────


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


# ── Password reset ────────────────────────────────────────────────────────────


@router.post("/password-reset/request", response_model=schemas.MessageResponse, status_code=202)
@limiter.limit("5/15minutes")
async def password_reset_request(
    request: Request,
    body: schemas.PasswordResetRequestBody,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    result = await service.request_password_reset(db, redis, body.email)
    if result:
        user, token = result
        reset_url = f"{settings.app_base_url}/reset-password?token={token}"
        from src.worker.tasks.email import send_password_reset_email

        send_password_reset_email.delay(user.email, user.display_name, reset_url)

    # Always return the same message — don't reveal whether email is registered
    return schemas.MessageResponse(message="If that email address is registered, you'll receive a reset link shortly.")


@router.post("/password-reset/confirm", response_model=schemas.MessageResponse)
async def password_reset_confirm(
    body: schemas.PasswordResetConfirmBody,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await service.confirm_password_reset(db, redis, body.token, body.new_password)
    return schemas.MessageResponse(message="Password updated successfully.")
