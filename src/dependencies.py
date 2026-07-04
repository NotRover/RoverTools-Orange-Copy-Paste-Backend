from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis

from src.auth.tokens import decode_supabase_token
from src.redis_client import get_redis_pool

bearer_scheme = HTTPBearer()


async def get_redis(redis: Redis = Depends(get_redis_pool)) -> Redis:
    return redis


async def get_current_user_only(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    """User id from the verified Supabase JWT `sub` claim.

    Use for endpoints that don't act on a specific device (bootstrap, device
    registration, device listing/revocation).
    """
    payload = decode_supabase_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")
    return user_id


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
) -> tuple[str, str]:
    """(user_id, device_id).

    `user_id` comes from the verified Supabase JWT `sub`; `device_id` from the
    `X-Device-Id` header — the caller's registered device. Device scoping is
    within the caller's own account (cursor + presence), so the header is trusted
    without a per-request DB lookup, keeping the dependency stateless.
    """
    user_id = await get_current_user_only(credentials)
    if not x_device_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Device-Id header")
    return user_id, x_device_id
