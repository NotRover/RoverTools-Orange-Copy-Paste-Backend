"""
Internal/admin endpoints.

/internal/healthz  — public, used by load balancers
/internal/metrics  — Prometheus text, requires X-Admin-Key
/internal/stats    — JSON aggregate, requires X-Admin-Key
/internal/admin/*  — user management, requires X-Admin-Key

Set ADMIN_API_KEY in .env to enable admin endpoints.
If ADMIN_API_KEY is empty, all admin/metrics/stats endpoints return 503.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin import service
from src.admin.schemas import (
    QuotaUpdateRequest,
    StatsResponse,
    SuspendRequest,
    UserAdminDetail,
    UserListResponse,
)
from src.config import settings
from src.database import get_db
from src.dependencies import get_redis
from src.realtime import hub

router = APIRouter(prefix="/internal", tags=["admin"])


# ── Admin key dependency ───────────────────────────────────────────────────────

def require_admin_key(
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
) -> None:
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoints are disabled (ADMIN_API_KEY not set)",
        )
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin key",
        )


# ── Health (public — no admin key required) ────────────────────────────────────

@router.get("/healthz", include_in_schema=True)
async def healthz(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    db_ok = False
    redis_ok = False

    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        pass

    healthy = db_ok and redis_ok
    return {
        "status": "ok" if healthy else "degraded",
        "db": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }


# ── Prometheus metrics ─────────────────────────────────────────────────────────

@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def metrics(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> str:
    stats = await service.get_stats(db, redis, hub.connection_count())

    lines: list[str] = []

    def _gauge(name: str, help_text: str, value: int | None) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value if value is not None else 0}")

    _gauge("orange_users_total", "Total registered users", stats.users_total)
    _gauge("orange_users_verified_total", "Email-verified users", stats.users_verified)
    _gauge("orange_users_suspended_total", "Suspended users", stats.users_suspended)
    _gauge("orange_devices_total", "Total registered devices", stats.devices_total)
    _gauge("orange_devices_active_total", "Active (non-revoked) devices", stats.devices_active)
    _gauge("orange_sync_entries_total", "Total sync entries", stats.entries_total)
    _gauge("orange_sync_entries_deleted_total", "Tombstoned sync entries", stats.entries_deleted)
    _gauge("orange_blobs_total", "Total blob records", stats.blobs_total)
    _gauge("orange_blobs_confirmed_total", "Confirmed (uploaded) blobs", stats.blobs_confirmed)
    _gauge("orange_storage_bytes_used", "Total blob storage bytes used across all users", stats.storage_bytes_used)
    _gauge("orange_ws_connections_active", "Active WebSocket connections (this process)", stats.ws_connections_active)
    if stats.redis_memory_bytes is not None:
        _gauge("orange_redis_memory_bytes", "Redis used_memory bytes", stats.redis_memory_bytes)

    return "\n".join(lines) + "\n"


# ── JSON stats ─────────────────────────────────────────────────────────────────

@router.get(
    "/stats",
    response_model=StatsResponse,
    dependencies=[Depends(require_admin_key)],
)
async def stats(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StatsResponse:
    return await service.get_stats(db, redis, hub.connection_count())


# ── User management ────────────────────────────────────────────────────────────

@router.get(
    "/admin/users",
    response_model=UserListResponse,
    dependencies=[Depends(require_admin_key)],
)
async def list_users(
    db: AsyncSession = Depends(get_db),
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> UserListResponse:
    return await service.list_users(db, offset=offset, limit=limit, search=search)


@router.get(
    "/admin/users/{user_id}",
    response_model=UserAdminDetail,
    dependencies=[Depends(require_admin_key)],
)
async def get_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> UserAdminDetail:
    return await service.get_user(db, user_id)


@router.patch(
    "/admin/users/{user_id}/quota",
    status_code=204,
    dependencies=[Depends(require_admin_key)],
)
async def update_quota(
    user_id: uuid.UUID,
    body: QuotaUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    await service.update_quota(db, user_id, body.blob_bytes_quota)


@router.post(
    "/admin/users/{user_id}/suspend",
    status_code=204,
    dependencies=[Depends(require_admin_key)],
)
async def suspend_user(
    user_id: uuid.UUID,
    body: SuspendRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    await service.set_suspended(db, user_id, body.suspend)


@router.delete(
    "/admin/users/{user_id}",
    status_code=204,
    dependencies=[Depends(require_admin_key)],
)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    await service.delete_user(db, user_id)
