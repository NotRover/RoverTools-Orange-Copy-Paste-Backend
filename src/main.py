import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src import background, realtime
from src.admin.router import admin_router, probe_router
from src.auth.router import router as auth_router
from src.blobs.router import router as blobs_router
from src.config import settings
from src.announcements.router import router as announcements_router
from src.spaces.invites import router as invites_router
from src.spaces.router import router as spaces_router
from src.limiter import limiter
from src.middleware import SecurityHeadersMiddleware
from src.redis_client import close_redis_pool, get_redis_pool
from src.settings.router import router as settings_router
from src.sync.router import router as sync_router
from src.web.router import router as web_router
from src.version import API_PREFIX, SERVICE_VERSION


# ── OpenAPI tag descriptions ────────────────────────────────────────────────────
OPENAPI_TAGS = [
    {"name": "auth", "description": "Profile bootstrap, device registration, and E2E public-key storage. "
     "Identity itself (signup/login/refresh/verify/reset) is handled by Supabase Auth."},
    {"name": "sync", "description": "Encrypted clipboard/note entry push & pull with last-write-wins and a "
     "per-device cursor. Deletes are tombstones (push with `deleted_at`)."},
    {"name": "settings", "description": "Encrypted per-user settings blob with last-write-wins."},
    {"name": "blobs", "description": "Presigned S3/R2 upload & download URLs for large attachments, with quota."},
    {"name": "spaces", "description": "Shared spaces: realtime encrypted clipboard/note sharing between users, "
     "with invite codes and per-member Space Key distribution."},
    {"name": "invites", "description": "Addressed space invites: persistent, per-email invitations with accept/decline/"
     "revoke and live `invite:*` notifications — the in-app counterpart to bearer invite codes."},
    {"name": "announcements", "description": "Server-authored messages to users - the one thing here that is not "
     "ciphertext, because it is the service's own words. Read by the client's notification centre."},
    {"name": "realtime", "description": "WebSocket `/ws` fan-out of sync/presence/sharing events. Not part of the "
     "OpenAPI HTTP schema; see the API reference doc for the event contract."},
    {"name": "ops", "description": "Unversioned infrastructure probes: `/internal/healthz` (public) and "
     "`/internal/metrics` (Prometheus, admin key)."},
    {"name": "admin", "description": "Versioned management API under `/internal/v1` (requires `X-Admin-Key`): "
     "stats and user administration."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis_pool()
    stop = asyncio.Event()
    pubsub_task = asyncio.create_task(realtime.start_listener(settings.redis_url))
    maintenance_task = asyncio.create_task(background.run_maintenance(stop))
    try:
        yield
    finally:
        stop.set()
        for task in (pubsub_task, maintenance_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await close_redis_pool()


app = FastAPI(
    title="Orange Clipboard API",
    version=SERVICE_VERSION,
    description=(
        "Smart Clipboard backend — cloud sync, realtime sharing, E2E encryption "
        "(Supabase Auth + Postgres).\n\n"
        "**Versioning:** product endpoints live under `/api/v1` (client) and `/internal/v1` "
        "(admin). Infra probes `/internal/healthz` and `/internal/metrics` are intentionally "
        "unversioned. Every response carries an `X-API-Version` header."
    ),
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
    # `None` removes the route entirely, so these 404 unless DOCS_ENABLED=true.
    docs_url="/api/docs" if settings.docs_enabled else None,
    redoc_url="/api/redoc" if settings.docs_enabled else None,
    openapi_url="/api/openapi.json" if settings.docs_enabled else None,
)


async def _handle_rate_limit(request: Request, exc: Exception) -> Response:
    if isinstance(exc, RateLimitExceeded):
        return _rate_limit_exceeded_handler(request, exc)
    return Response(status_code=429)


# ── Rate limiting ─────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _handle_rate_limit)
app.add_middleware(SlowAPIMiddleware)

# ── Security headers ──────────────────────────────────────────────────────────
app.add_middleware(SecurityHeadersMiddleware)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Versioned product API (/api/v1) ─────────────────────────────────────────────
app.include_router(auth_router, prefix=API_PREFIX)
app.include_router(sync_router, prefix=API_PREFIX)
app.include_router(settings_router, prefix=API_PREFIX)
app.include_router(blobs_router, prefix=API_PREFIX)
app.include_router(spaces_router, prefix=API_PREFIX)
app.include_router(invites_router, prefix=API_PREFIX)
app.include_router(announcements_router, prefix=API_PREFIX)

# ── WebSocket ─────────────────────────────────────────────────────────────────
app.include_router(realtime.router)

# ── Human-facing pages (unversioned: people paste these links) ──────────────────
app.include_router(web_router)

# ── Internal: unversioned probes + versioned admin API ──────────────────────────
app.include_router(probe_router)
app.include_router(admin_router)
