"""Internal endpoints — infrastructure probes and admin management.

Two routers with deliberately different versioning (see ``src/version.py``):

* ``probe_router`` — **unversioned** infra endpoints under ``/internal``:
    - ``GET /internal/healthz``  liveness/readiness (public, no auth)
    - ``GET /internal/metrics``  Prometheus text (requires ``X-Admin-Key``)
  These paths are hardcoded by load balancers and metric scrapers, so they stay
  stable across API version bumps.

* ``admin_router`` — **versioned** management API under ``/internal/v1``:
    - ``GET  /internal/v1/stats``            JSON aggregate
    - ``GET  /internal/v1/admin/users``      list users
    - ``GET  /internal/v1/admin/users/{id}`` user detail
    - ``PATCH /internal/v1/admin/users/{id}/quota``
    - ``POST  /internal/v1/admin/users/{id}/suspend``
    - ``DELETE /internal/v1/admin/users/{id}``
    - ``GET  /internal/v1/admin/email``       mail provider and whether it is configured
    - ``POST /internal/v1/admin/email/test``  send one test message and report the error
    - ``POST  /internal/v1/admin/announcements``       post a message to a user or everyone
    - ``DELETE /internal/v1/admin/announcements/{id}``
  All require the ``X-Admin-Key`` header.

Set ``ADMIN_API_KEY`` in the environment to enable the admin/metrics/stats
endpoints; when it is unset they all return ``503 Service Unavailable``.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin import service
from src.announcements import router as announcements_router
from src.announcements.schemas import AnnouncementCreateResponse
from src.admin.schemas import (
    EmailConfigResponse,
    EmailTestRequest,
    EmailTestResponse,
    HealthResponse,
    QuotaUpdateRequest,
    StatsResponse,
    SuspendRequest,
    UserAdminDetail,
    UserListResponse,
)
from src import email
from src.config import settings
from src.database import get_db
from src.dependencies import get_redis
from src.version import INTERNAL_VERSIONED_PREFIX

logger = logging.getLogger(__name__)

# Unversioned infrastructure probes.
probe_router = APIRouter(prefix="/internal", tags=["ops"])
# Versioned admin/management surface.
admin_router = APIRouter(prefix=INTERNAL_VERSIONED_PREFIX, tags=["admin"])


# ── Admin key dependency ───────────────────────────────────────────────────────


def require_admin_key(x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None) -> None:
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoints are disabled (ADMIN_API_KEY not set)",
        )
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin key")


# ── Health (public — no admin key required) ────────────────────────────────────


@probe_router.get("/healthz", response_model=HealthResponse)
async def healthz(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)) -> HealthResponse:
    """Liveness/readiness probe.

    Public (no auth). Reports ``ok`` when both Postgres and Redis are reachable,
    otherwise ``degraded`` with per-dependency status. Intended for load-balancer
    health checks and orchestrator readiness gates.
    """
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
    return HealthResponse(
        status="ok" if healthy else "degraded",
        db="ok" if db_ok else "error",
        redis="ok" if redis_ok else "error",
    )


# ── Prometheus metrics ─────────────────────────────────────────────────────────


@probe_router.get(
    "/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_admin_key)],
    include_in_schema=False,
)
async def metrics(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)) -> str:
    """Prometheus exposition-format metrics (text/plain).

    Requires: ``X-Admin-Key``. Excluded from the OpenAPI schema because the body
    is Prometheus text, not JSON. Scrape at ``/internal/metrics``.
    """
    stats = await service.get_stats(db, redis)
    lines: list[str] = []

    def _gauge(name: str, help_text: str, value: int | None) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value if value is not None else 0}")

    _gauge("orange_users_total", "Total profiles", stats.users_total)
    _gauge("orange_devices_total", "Total registered devices", stats.devices_total)
    _gauge("orange_devices_active_total", "Active (non-revoked) devices", stats.devices_active)
    _gauge("orange_devices_online", "Devices currently connected (Redis presence)", stats.devices_online)
    _gauge("orange_sync_entries_total", "Total sync entries", stats.entries_total)
    _gauge("orange_sync_entries_deleted_total", "Tombstoned sync entries", stats.entries_deleted)
    _gauge("orange_blobs_total", "Total blob records", stats.blobs_total)
    _gauge("orange_blobs_confirmed_total", "Confirmed (uploaded) blobs", stats.blobs_confirmed)
    _gauge("orange_storage_bytes_used", "Confirmed blob storage bytes across all users", stats.storage_bytes_used)
    if stats.redis_memory_bytes is not None:
        _gauge("orange_redis_memory_bytes", "Redis used_memory bytes", stats.redis_memory_bytes)

    return "\n".join(lines) + "\n"


# ── JSON stats ─────────────────────────────────────────────────────────────────


@admin_router.get("/stats", response_model=StatsResponse, dependencies=[Depends(require_admin_key)])
async def stats(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)) -> StatsResponse:
    """Aggregate service statistics as JSON.

    Requires: ``X-Admin-Key``. Same underlying data as ``/internal/metrics`` but
    structured for dashboards and ad-hoc inspection.
    """
    return await service.get_stats(db, redis)


# ── User management ────────────────────────────────────────────────────────────


@admin_router.get("/admin/users", response_model=UserListResponse, dependencies=[Depends(require_admin_key)])
async def list_users(
    db: AsyncSession = Depends(get_db),
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> UserListResponse:
    """List user profiles with pagination and optional display-name search.

    Requires: ``X-Admin-Key``.
    """
    return await service.list_users(db, offset=offset, limit=limit, search=search)


@admin_router.get("/admin/users/{user_id}", response_model=UserAdminDetail, dependencies=[Depends(require_admin_key)])
async def get_user(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> UserAdminDetail:
    """Fetch one user's profile, usage counts, and (if Supabase admin is
    configured) account state (email, verification, ban status).

    Requires: ``X-Admin-Key``.
    """
    return await service.get_user(db, user_id)


@admin_router.patch("/admin/users/{user_id}/quota", status_code=204, dependencies=[Depends(require_admin_key)])
async def update_quota(user_id: uuid.UUID, body: QuotaUpdateRequest, db: AsyncSession = Depends(get_db)) -> None:
    """Override a user's blob storage quota (bytes).

    Requires: ``X-Admin-Key``.
    """
    await service.update_quota(db, user_id, body.blob_bytes_quota)


@admin_router.post("/admin/users/{user_id}/suspend", status_code=204, dependencies=[Depends(require_admin_key)])
async def suspend_user(user_id: uuid.UUID, body: SuspendRequest) -> None:
    """Suspend or un-suspend an account (delegates to the Supabase Admin API).

    Requires: ``X-Admin-Key``.
    """
    await service.set_suspended(user_id, body.suspend)


@admin_router.delete("/admin/users/{user_id}", status_code=204, dependencies=[Depends(require_admin_key)])
async def delete_user(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    """Delete a user's app-side data (profile → cascades to devices/entries) and
    the Supabase account.

    Requires: ``X-Admin-Key``.
    """
    await service.delete_user(db, user_id)


# ── Email ──────────────────────────────────────────────────────────────────────
#
# Invite delivery is best-effort and failures are swallowed (see
# `email.send_sharing_invite`), so from the outside a misconfigured provider
# looks exactly like a working one. These two routes are the way to tell.


@admin_router.get("/admin/email", response_model=EmailConfigResponse, dependencies=[Depends(require_admin_key)])
async def email_config() -> EmailConfigResponse:
    """Report the mail provider this deployment would use and whether its
    credentials are present. No secret is returned.

    Requires: ``X-Admin-Key``.
    """
    return EmailConfigResponse(**email.describe_config())


@admin_router.post("/admin/email/test", response_model=EmailTestResponse, dependencies=[Depends(require_admin_key)])
async def email_test(body: EmailTestRequest) -> EmailTestResponse:
    """Send a test message to one address and report what happened. Runs in a
    threadpool because both send paths are synchronous.

    Requires: ``X-Admin-Key``.
    """
    provider = settings.email_provider.lower()
    try:
        await run_in_threadpool(email.send_test_email, body.to)
    except Exception as exc:
        logger.warning("Admin email test to %s failed: %s", body.to, exc)
        return EmailTestResponse(sent=False, provider=provider, error=f"{type(exc).__name__}: {exc}")
    return EmailTestResponse(sent=True, provider=provider)


# ── Announcements ──────────────────────────────────────────────────────────────
#
# The handlers live in ``src/announcements/router.py`` next to the rest of that
# domain; only the mounting is here, because posting an announcement is an admin
# act and belongs behind the admin key.

admin_router.add_api_route(
    "/admin/announcements",
    announcements_router.create_announcement,
    methods=["POST"],
    response_model=AnnouncementCreateResponse,
    dependencies=[Depends(require_admin_key)],
    tags=["admin"],
)
admin_router.add_api_route(
    "/admin/announcements/{announcement_id}",
    announcements_router.delete_announcement,
    methods=["DELETE"],
    dependencies=[Depends(require_admin_key)],
    tags=["admin"],
)
