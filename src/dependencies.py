import logging
import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import Device
from src.auth.tokens import verify_supabase_token
from src.database import get_db
from src.redis_client import get_redis_pool

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer()

# How long a "this device is yours and live" answer is reused. Revocation deletes
# the key (see `forget_device`), so the window only matters if that delete fails.
DEVICE_CACHE_SECONDS = 60


async def get_redis(redis: Redis = Depends(get_redis_pool)) -> Redis:
    return redis


def device_cache_key(user_id: str, device_id: str) -> str:
    return f"dev:{user_id}:{device_id}"


async def forget_device(redis: Redis, user_id: str, device_id: str) -> None:
    """Drop the cached positive device check, so a revoked device is refused at once."""
    try:
        await redis.delete(device_cache_key(user_id, device_id))
    except RedisError as exc:
        logger.warning("could not drop device cache for %s: %s", device_id, exc)


def parse_device_id(raw: str | None) -> uuid.UUID:
    """`X-Device-Id` as a UUID. 400 when missing or malformed."""
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Device-Id header")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Device-Id header") from exc


async def check_device(db: AsyncSession, redis: Redis, user_id: str, device_id: uuid.UUID) -> None:
    """Refuse a device that is not the caller's live, registered device.

    401 `device_revoked` when it was revoked: the client signs itself out on
    that. 403 `device_unknown` when no such row exists or it belongs to another
    account. A positive answer is cached in Redis for `DEVICE_CACHE_SECONDS`; a
    Redis outage falls back to the database rather than failing the request.
    """
    key = device_cache_key(user_id, str(device_id))
    try:
        if await redis.get(key):
            return
    except RedisError as exc:
        logger.warning("device cache unavailable: %s", exc)

    device = await db.scalar(select(Device).where(Device.id == device_id))
    if device is None or str(device.user_id) != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="device_unknown")
    if device.revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="device_revoked")

    try:
        await redis.set(key, "1", ex=DEVICE_CACHE_SECONDS)
    except RedisError as exc:
        logger.warning("device cache unavailable: %s", exc)


async def get_current_claims(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """Full verified Supabase JWT payload, for routes that need more than `sub`
    (bootstrap mirrors the `email` claim; invites match against it, and should
    refuse `email_verified: false` when the claim is present; account reset reads
    `amr`).

    Stores the verified `sub` on `request.state.user_id`, which is what the rate
    limiter keys on (see `src/limiter.py`)."""
    payload = await verify_supabase_token(credentials.credentials)
    if not payload.get("sub"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")
    request.state.user_id = payload["sub"]
    return payload


async def get_current_user_only(
    claims: dict = Depends(get_current_claims),
) -> str:
    """User id from the verified Supabase JWT `sub` claim.

    Use for endpoints that don't act on a specific device (bootstrap, device
    registration, device listing/revocation).
    """
    return claims["sub"]


async def get_current_user_id(
    claims: dict = Depends(get_current_claims),
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> tuple[str, str]:
    """(user_id, device_id).

    `user_id` comes from the verified Supabase JWT `sub`; `device_id` from the
    `X-Device-Id` header, checked against the `devices` table: it must be a UUID
    (400), belong to `sub` and exist (403 `device_unknown`), and not be revoked
    (401 `device_revoked`). Only after that is it trusted as the origin device
    for fan-out, cursors and presence.
    """
    device_id = parse_device_id(x_device_id)
    await check_device(db, redis, claims["sub"], device_id)
    return claims["sub"], str(device_id)
