import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import HTTPException, status
from jose import JWTError, jwt

from src.config import settings


def _now_ts() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


@lru_cache(maxsize=1)
def get_jwks() -> dict:
    """Return the RS256 public key as a JWKS document.

    Cached at process level — keys don't change at runtime.
    """
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_public_key,
    )

    pem = settings.jwt_public_key
    pub_key = load_pem_public_key(pem.encode())
    pub_numbers = pub_key.public_numbers()  # type: ignore[union-attr]

    def _b64url_int(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    # kid = first 16 hex chars of SHA-256 of DER-encoded public key (stable fingerprint)
    der = pub_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    kid = hashlib.sha256(der).hexdigest()[:16]

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64url_int(pub_numbers.n),
                "e": _b64url_int(pub_numbers.e),
            }
        ]
    }


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
