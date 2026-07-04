"""Supabase JWT verification.

Supabase Auth signs access tokens with the project's JWT secret (HS256). We only
ever *verify* them here — the backend never issues its own tokens. The verified
`sub` claim is the Supabase user id, which is also the primary key of our
`profiles` table.
"""

import jwt
from fastapi import HTTPException, status

from src.config import settings


def decode_supabase_token(token: str) -> dict:
    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SUPABASE_JWT_SECRET is not configured",
        )
    try:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=[settings.supabase_jwt_algorithm],
            audience=settings.supabase_jwt_audience,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc
