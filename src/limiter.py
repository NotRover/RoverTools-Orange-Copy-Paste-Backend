"""Global rate limiter instance (slowapi).

Import `limiter` in routers that need rate-limited endpoints and decorate the route with
`@limiter.limit("N/period")`. The route must take a `request: Request` parameter. The
limiter is attached to app.state in main.py so slowapi can find it.

Keying: slowapi calls the decorated endpoint after FastAPI has resolved its dependencies,
so an authenticated route has already run `get_current_user_id` (or `get_current_claims`),
which stores the *verified* `sub` on `request.state.user_id`. Such a request is keyed by
user, so one account shares one bucket across devices and networks. Anything else (an
unauthenticated route, or a request that never reached the dependency) is keyed by client
IP. The IP is `request.client.host`, which uvicorn rewrites from `X-Forwarded-For` only
when it runs with `--proxy-headers` and the peer is in `--forwarded-allow-ips` (see the
Dockerfile), so a client cannot pick its own bucket by sending the header.

An unverified token is never used as a key: a forged `sub` would let one caller drain
another user's bucket.
"""

from slowapi import Limiter
from starlette.requests import Request


def client_ip(request: Request) -> str:
    """The peer address, already proxy-resolved by uvicorn when it trusts the proxy."""
    return request.client.host if request.client else "unknown"


def rate_limit_key(request: Request) -> str:
    """Per-user key when a verified token is on the request, else per-IP."""
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    return f"ip:{client_ip(request)}"


limiter = Limiter(key_func=rate_limit_key, default_limits=[])
