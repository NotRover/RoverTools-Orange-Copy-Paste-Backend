
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis

from src.redis_client import get_redis_pool

bearer_scheme = HTTPBearer()


async def get_redis(redis: Redis = Depends(get_redis_pool)) -> Redis:
    return redis


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    redis: Redis = Depends(get_redis),
) -> tuple[str, str]:
    """Returns (user_id, device_id) from a validated JWT."""
    from src.auth.jwt import decode_access_token

    token = credentials.credentials
    payload = decode_access_token(token)

    # Check revocation list
    jti = payload.get("jti")
    if jti and await redis.exists(f"revoked:{jti}"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")

    user_id = payload.get("sub")
    device_id = payload.get("did")
    if not user_id or not device_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")

    return user_id, device_id
