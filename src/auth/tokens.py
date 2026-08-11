"""Supabase JWT verification.

Supabase Auth issues access tokens; we only ever *verify* them here — the backend
never signs its own. The verified `sub` claim is the Supabase user id, which is
also the primary key of our `profiles` table.

Two signing schemes are supported, selected per-token from the JWT header:

* **Asymmetric (ES256/RS256)** — the default for every Supabase project created
  from 2025-10-01 onward. The public key is fetched from the project's JWKS
  endpoint and cached in-process, so Supabase-side key rotation needs no
  redeploy. Requires `SUPABASE_URL`.
* **Symmetric (HS256)** — the legacy shared `SUPABASE_JWT_SECRET`, used by older
  projects. Only accepted when that secret is configured.

Supporting both means one deployment works against a legacy project, a migrated
project mid-rotation (whose JWKS carries the old secret alongside the new EC
key), and a brand-new asymmetric-only project.
"""

from functools import lru_cache

import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient

from src.config import settings

# The only algorithms Supabase signs with. Anything else — notably "none" — is
# rejected before a key is ever selected.
_ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")
_SYMMETRIC_ALGORITHM = "HS256"
_ACCEPTED_ALGORITHMS = (*_ASYMMETRIC_ALGORITHMS, _SYMMETRIC_ALGORITHM)

_JWKS_TIMEOUT_SECONDS = 5
_JWKS_CACHE_SECONDS = 300


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
    )


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    """Process-wide JWKS client.

    Cached because the client holds the fetched key set; a fresh instance per
    request would re-download the JWKS on every call. The short fetch timeout
    bounds how long a cache miss can block the event loop (`jwt`'s JWKS client
    is synchronous), and `lifespan` bounds how stale a rotated key can get.
    """
    base = settings.supabase_url.rstrip("/")
    return PyJWKClient(
        f"{base}/auth/v1/.well-known/jwks.json",
        cache_keys=True,
        lifespan=_JWKS_CACHE_SECONDS,
        timeout=_JWKS_TIMEOUT_SECONDS,
    )


def _signing_key(token: str, algorithm: str):
    """Resolve the verification key for `token`, given its (allowlisted) `algorithm`.

    Picking the key from the token's own header is safe here because the two
    branches draw on unrelated key material: the HS256 branch uses the shared
    secret, never a public key. The classic algorithm-confusion attack — signing
    HS256 with the RSA/EC *public key* as the HMAC secret — therefore has nothing
    to forge against, and HS256 is refused outright when no secret is set.
    """
    if algorithm == _SYMMETRIC_ALGORITHM:
        if not settings.supabase_jwt_secret:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Received an HS256 token but SUPABASE_JWT_SECRET is not configured"
                ),
            )
        return settings.supabase_jwt_secret

    if not settings.supabase_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Received an asymmetric token but SUPABASE_URL is not configured",
        )
    try:
        return _jwks_client().get_signing_key_from_jwt(token).key
    except jwt.PyJWTError as exc:
        # Covers both an unknown `kid` and a JWKS fetch failure. Reported as 401
        # rather than 5xx so a forged token with a random `kid` can't be used to
        # drive server-error responses.
        raise _unauthorized() from exc


def decode_supabase_token(token: str) -> dict:
    try:
        algorithm = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc

    if algorithm not in _ACCEPTED_ALGORITHMS:
        raise _unauthorized()

    key = _signing_key(token, algorithm)
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            audience=settings.supabase_jwt_audience,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc
