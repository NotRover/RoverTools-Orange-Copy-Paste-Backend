import base64
import os
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from passlib.context import CryptContext
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import Device, User
from src.auth.schemas import LoginRequest, RegisterRequest
from src.config import settings

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def hash_password(password: str) -> str:
    return _pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


def generate_kdf_salt() -> str:
    return base64.b64encode(os.urandom(16)).decode()


# ── Registration ──────────────────────────────────────────────────────────────


async def register_user(db: AsyncSession, req: RegisterRequest) -> User:
    existing = await db.scalar(select(User).where(User.email == req.email))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    now = _now_ms()
    user = User(
        email=req.email,
        display_name=req.display_name,
        password_hash=hash_password(req.password),
        kdf_salt=generate_kdf_salt(),
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ── Login ─────────────────────────────────────────────────────────────────────


async def login_user(db: AsyncSession, req: LoginRequest) -> tuple[User, Device]:
    user = await db.scalar(select(User).where(User.email == req.email))
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if user.suspended_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended",
        )

    now = _now_ms()
    device = Device(
        user_id=user.id,
        device_name=req.device_name,
        platform=req.platform,
        app_version=req.app_version,
        device_pubkey=req.device_pubkey,
        created_at=now,
        last_seen_at=now,
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return user, device


# ── Token rotation ────────────────────────────────────────────────────────────


async def store_refresh_token(db: AsyncSession, device: Device, raw_token: str) -> None:
    device.refresh_token_hash = _pwd_ctx.hash(raw_token)
    device.last_seen_at = _now_ms()
    await db.commit()


async def rotate_refresh_token(
    db: AsyncSession,
    device_id: uuid.UUID,
    raw_token: str,
) -> tuple[User, Device]:
    device = await db.scalar(select(Device).where(Device.id == device_id))
    if not device or device.revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device not found or revoked")

    if not device.refresh_token_hash or not _pwd_ctx.verify(raw_token, device.refresh_token_hash):
        device.revoked = True
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    user = await db.scalar(select(User).where(User.id == device.user_id))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return user, device


# ── Device management ─────────────────────────────────────────────────────────


async def revoke_device(db: AsyncSession, device_id: uuid.UUID, requesting_user_id: str) -> None:
    device = await db.scalar(select(Device).where(Device.id == device_id))
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if str(device.user_id) != requesting_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your device")
    device.revoked = True
    device.refresh_token_hash = None
    await db.commit()


async def get_user_devices(db: AsyncSession, user_id: str, current_device_id: str) -> list[dict]:
    result = await db.scalars(select(Device).where(Device.user_id == uuid.UUID(user_id), Device.revoked.is_(False)))
    devices = result.all()
    return [
        {
            "id": d.id,
            "device_name": d.device_name,
            "platform": d.platform,
            "app_version": d.app_version,
            "last_seen_at": d.last_seen_at,
            "is_current": str(d.id) == current_device_id,
        }
        for d in devices
    ]


# ── Public key management ─────────────────────────────────────────────────────


async def store_public_keys(
    db: AsyncSession,
    user_id: str,
    device_id: str,
    identity_pubkey: str,
    device_pubkey: str,
) -> None:
    user = await db.scalar(select(User).where(User.id == uuid.UUID(user_id)))
    device = await db.scalar(select(Device).where(Device.id == uuid.UUID(device_id)))
    if not user or not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    user.identity_pubkey = identity_pubkey
    device.device_pubkey = device_pubkey
    await db.commit()


async def store_wrapped_umk(
    db: AsyncSession,
    target_device_id: uuid.UUID,
    requesting_user_id: str,
    wrapped_umk: str,
) -> None:
    target_device = await db.scalar(select(Device).where(Device.id == target_device_id))
    if not target_device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if str(target_device.user_id) != requesting_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your device")
    target_device.wrapped_umk = wrapped_umk
    await db.commit()


# ── Email verification ────────────────────────────────────────────────────────

_VERIFY_PREFIX = "email_verify:"


async def generate_verification_token(redis: Redis, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    await redis.setex(
        f"{_VERIFY_PREFIX}{token}",
        settings.email_verify_token_ttl,
        user_id,
    )
    return token


async def verify_email(db: AsyncSession, redis: Redis, token: str) -> User:
    key = f"{_VERIFY_PREFIX}{token}"
    user_id = await redis.get(key)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token",
        )

    user = await db.scalar(select(User).where(User.id == uuid.UUID(user_id)))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.email_verified = True
    await db.commit()
    await redis.delete(key)
    return user


# ── Password reset ────────────────────────────────────────────────────────────

_RESET_PREFIX = "pwd_reset:"


async def request_password_reset(db: AsyncSession, redis: Redis, email: str) -> tuple[User, str] | None:
    """Returns (user, token) if found, None if not (caller should not reveal whether email exists)."""
    user = await db.scalar(select(User).where(User.email == email))
    if not user:
        return None

    token = secrets.token_urlsafe(32)
    await redis.setex(
        f"{_RESET_PREFIX}{token}",
        settings.password_reset_token_ttl,
        str(user.id),
    )
    return user, token


async def confirm_password_reset(db: AsyncSession, redis: Redis, token: str, new_password: str) -> None:
    key = f"{_RESET_PREFIX}{token}"
    user_id = await redis.get(key)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    user = await db.scalar(select(User).where(User.id == uuid.UUID(user_id)))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.password_hash = hash_password(new_password)
    user.updated_at = _now_ms()
    await db.commit()
    await redis.delete(key)
