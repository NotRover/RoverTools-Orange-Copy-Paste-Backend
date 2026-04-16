import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from jose import JWTError, jwt

from src.config import settings


def _now_ts() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def create_access_token(user_id: str, device_id: str) -> tuple[str, str]:
    """Returns (token, jti)."""
    jti = str(uuid.uuid4())
    expire = datetime.now(UTC) + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload = {
        "sub": user_id,
        "did": device_id,
        "jti": jti,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(expire.timestamp()),
        "iss": "orange-clipboard-api",
    }
    token = jwt.encode(payload, settings.jwt_private_key, algorithm=settings.jwt_algorithm)
    return token, jti


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_iss": True},
            issuer="orange-clipboard-api",
        )
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc


def create_refresh_token() -> str:
    """Returns a secure random hex token (64 chars)."""
    import secrets
    return secrets.token_hex(32)
