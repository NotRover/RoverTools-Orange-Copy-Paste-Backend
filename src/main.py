import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.admin.router import router as admin_router
from src.well_known import router as well_known_router
from src.auth.router import router as auth_router
from src.blobs.router import router as blobs_router
from src.config import settings
from src.groups.router import router as groups_router
from src.limiter import limiter
from src.middleware import SecurityHeadersMiddleware
from src.realtime.pubsub import start_listener
from src.realtime.router import router as realtime_router
from src.redis_client import close_redis_pool, get_redis_pool
from src.settings.router import router as settings_router
from src.sharing.router import router as sharing_router
from src.sync.router import router as sync_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis_pool()
    pubsub_task = asyncio.create_task(start_listener(settings.redis_url))
    yield
    pubsub_task.cancel()
    try:
        await pubsub_task
    except asyncio.CancelledError:
        pass
    await close_redis_pool()


app = FastAPI(
    title="Orange Clipboard API",
    version="1.0.0",
    description="Smart Clipboard backend — cloud sync, realtime sharing, E2E encryption",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# ── Rate limiting ─────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
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

# ── API routes ────────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api/v1")
app.include_router(sync_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")
app.include_router(blobs_router, prefix="/api/v1")
app.include_router(groups_router, prefix="/api/v1")
app.include_router(sharing_router, prefix="/api/v1")

# ── WebSocket ─────────────────────────────────────────────────────────────────
app.include_router(realtime_router)

# ── Well-known (JWKS, etc.) ───────────────────────────────────────────────────
app.include_router(well_known_router)

# ── Internal / admin ─────────────────────────────────────────────────────────
app.include_router(admin_router)
