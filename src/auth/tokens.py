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

import threading
import time
from functools import lru_cache

import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient
from jwt.types import Options
from starlette.concurrency import run_in_threadpool

from src.config import settings

# The only algorithms Supabase signs with. Anything else — notably "none" — is
# rejected before a key is ever selected.
_ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")
_SYMMETRIC_ALGORITHM = "HS256"
_ACCEPTED_ALGORITHMS = (*_ASYMMETRIC_ALGORITHMS, _SYMMETRIC_ALGORITHM)

_JWKS_TIMEOUT_SECONDS = 5
_JWKS_CACHE_SECONDS = 300
# Longest gap between two forced JWKS refreshes. A token whose `kid` is not in the
# cached set forces a refetch, which is how a rotated key is picked up; without a
# cooldown every such token was a fetch, so a stream of random `kid`s made this
# service hammer Supabase and park a thread on each request. Within the window an
# unknown `kid` is judged against the set we already hold.
_JWKS_REFRESH_COOLDOWN_SECONDS = 60

_refresh_lock = threading.Lock()
_last_forced_refresh = 0.0


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
    )


def _key_set_unavailable() -> HTTPException:
    """We could not reach the JWKS endpoint, which says nothing about the token.

    The distinction matters more than it looks. A 401 is a verdict on the
    caller's credential, and the desktop client acts on it: a 401 during its
    silent session restore used to end the session outright, with no retry, so
    one failed DNS lookup here signed a user out and made them type a password.
    503 is the honest answer - the client retries it, and `Retry-After` tells it
    roughly when.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Cannot verify tokens right now",
        headers={"Retry-After": "2"},
    )


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    """Process-wide JWKS client.

    Cached because the client holds the fetched key set; a fresh instance per
    request would re-download the JWKS on every call. Every call into it runs in
    the threadpool (`jwt`'s client is synchronous), the fetch timeout bounds how
    long one miss can hold a worker, and `lifespan` bounds how stale a rotated key
    can get.
    """
    base = settings.supabase_url.rstrip("/")
    return PyJWKClient(
        f"{base}/auth/v1/.well-known/jwks.json",
        cache_keys=True,
        lifespan=_JWKS_CACHE_SECONDS,
        timeout=_JWKS_TIMEOUT_SECONDS,
    )


def _claim_refresh_slot() -> bool:
    """True when this caller may force a JWKS refetch now; at most one per window."""
    global _last_forced_refresh
    with _refresh_lock:
        now = time.monotonic()
        if _last_forced_refresh and now - _last_forced_refresh < _JWKS_REFRESH_COOLDOWN_SECONDS:
            return False
        _last_forced_refresh = now
        return True


def _reset_refresh_cooldown() -> None:
    """For tests: forget when the last forced refresh happened."""
    global _last_forced_refresh
    with _refresh_lock:
        _last_forced_refresh = 0.0


def _match(keys: list, kid: str | None):
    for key in keys:
        if kid and key.key_id == kid:
            return key.key
    return None


def _asymmetric_key(token: str):
    """Resolve the public key for `token` from the JWKS. Blocking: run it in the threadpool.

    The cached set is consulted first (it is fetched when empty or older than
    `lifespan`). Only a `kid` missing from it forces a refetch, and only once per
    `_JWKS_REFRESH_COOLDOWN_SECONDS` across the process.
    """
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc
    client = _jwks_client()
    try:
        key = _match(client.get_signing_keys(), kid)
        if key is None and _claim_refresh_slot():
            key = _match(client.get_signing_keys(refresh=True), kid)
    except jwt.PyJWKClientConnectionError as exc:
        # We never reached the JWKS endpoint. Nothing was judged, so do not
        # answer with a verdict on the credential.
        raise _key_set_unavailable() from exc
    except jwt.PyJWTError as exc:
        # We reached it and it held nothing usable. A verdict, so 401.
        raise _unauthorized() from exc
    if key is None:
        # We did reach it and the `kid` is not in the set - a forged or
        # long-rotated token. 401, and deliberately not 5xx: a random `kid`
        # must not be a way to drive server-error responses.
        raise _unauthorized()
    return key


def _algorithm(token: str) -> str:
    try:
        algorithm = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc
    if algorithm not in _ACCEPTED_ALGORITHMS:
        raise _unauthorized()
    return algorithm


def _symmetric_key() -> str:
    """The HS256 secret.

    Picking the key from the token's own header is safe here because the two
    branches draw on unrelated key material: the HS256 branch uses the shared
    secret, never a public key. The classic algorithm-confusion attack - signing
    HS256 with the RSA/EC *public key* as the HMAC secret - therefore has nothing
    to forge against. With no secret configured an HS256 token is simply a token
    this deployment does not accept: 401, not a 500 anyone can trigger.
    """
    if not settings.supabase_jwt_secret:
        raise _unauthorized()
    return settings.supabase_jwt_secret


def _require_supabase_url() -> None:
    if not settings.supabase_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Received an asymmetric token but SUPABASE_URL is not configured",
        )


def _verify(token: str, key, algorithm: str) -> dict:
    required = ["exp", "sub"]
    issuer: str | None = None
    if settings.supabase_url:
        # GoTrue stamps `iss` with the project's auth URL. Checking it stops a
        # token minted by a different project that happens to share a key (the
        # legacy HS256 secret is the realistic case) from being accepted here.
        issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1"
        required.append("iss")
    options: Options = {"require": required}
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            audience=settings.supabase_jwt_audience,
            issuer=issuer,
            options=options,
            # Clock skew between Supabase and this host. Without it a token
            # minted a moment ago fails `iat` validation with
            # ImmatureSignatureError, which lands in the 401 below. Only the
            # client's session restore is exposed - it presents a token that is
            # milliseconds old - and being signed out over a second of drift is
            # not a trade worth making against a token that lives an hour.
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc


def decode_supabase_token(token: str) -> dict:
    """Verify `token` synchronously. May block on a JWKS fetch.

    Request paths use `verify_supabase_token`, which keeps that fetch off the event
    loop. This form stays for callers that are already off it (and for tests).
    """
    algorithm = _algorithm(token)
    if algorithm == _SYMMETRIC_ALGORITHM:
        return _verify(token, _symmetric_key(), algorithm)
    _require_supabase_url()
    return _verify(token, _asymmetric_key(token), algorithm)


async def verify_supabase_token(token: str) -> dict:
    """Verify `token` without blocking the event loop.

    The JWKS lookup is the only part that can touch the network, so only it goes
    to the threadpool; an HS256 token is checked in line.
    """
    algorithm = _algorithm(token)
    if algorithm == _SYMMETRIC_ALGORITHM:
        return _verify(token, _symmetric_key(), algorithm)
    _require_supabase_url()
    key = await run_in_threadpool(_asymmetric_key, token)
    return _verify(token, key, algorithm)
