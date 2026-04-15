from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.admin.router import router as admin_router
from src.auth.router import router as auth_router
from src.blobs.router import router as blobs_router
from src.config import settings
from src.groups.router import router as groups_router
from src.realtime.router import router as realtime_router
from src.redis_client import close_redis_pool, get_redis_pool
from src.sync.router import router as sync_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up Redis connection pool
    await get_redis_pool()
    yield
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(auth_router, prefix="/api/v1")
app.include_router(sync_router, prefix="/api/v1")
app.include_router(blobs_router, prefix="/api/v1")
app.include_router(groups_router, prefix="/api/v1")

# WebSocket
app.include_router(realtime_router)

# Internal/admin
app.include_router(admin_router)
