"""Admin service — aggregate queries and user management operations."""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.schemas import (
    StatsResponse,
    UserAdminDetail,
    UserAdminSummary,
    UserListResponse,
)
from src.auth.models import Device, User
from src.blobs.models import Blob
from src.sync.models import SyncEntry


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats(db: AsyncSession, redis: Redis, ws_connections: int) -> StatsResponse:
    users_total = (await db.scalar(select(func.count()).select_from(User))) or 0
    users_verified = (
        await db.scalar(select(func.count()).select_from(User).where(User.email_verified.is_(True)))
    ) or 0
    users_suspended = (
        await db.scalar(
            select(func.count()).select_from(User).where(User.suspended_at.isnot(None))
        )
    ) or 0

    devices_total = (await db.scalar(select(func.count()).select_from(Device))) or 0
    devices_active = (
        await db.scalar(
            select(func.count()).select_from(Device).where(Device.revoked.is_(False))
        )
    ) or 0

    entries_total = (await db.scalar(select(func.count()).select_from(SyncEntry))) or 0
    entries_deleted = (
        await db.scalar(
            select(func.count()).select_from(SyncEntry).where(SyncEntry.deleted_at.isnot(None))
        )
    ) or 0

    blobs_total = (await db.scalar(select(func.count()).select_from(Blob))) or 0
    blobs_confirmed = (
        await db.scalar(
            select(func.count()).select_from(Blob).where(Blob.confirmed.is_(True))
        )
    ) or 0

    storage_bytes = (
        await db.scalar(select(func.coalesce(func.sum(User.blob_bytes_used), 0)))
    ) or 0

    # Redis memory usage (best-effort)
    redis_memory: int | None = None
    try:
        info = await redis.info("memory")
        redis_memory = info.get("used_memory")
    except Exception:
        pass

    return StatsResponse(
        users_total=users_total,
        users_verified=users_verified,
        users_suspended=users_suspended,
        devices_total=devices_total,
        devices_active=devices_active,
        entries_total=entries_total,
        entries_deleted=entries_deleted,
        blobs_total=blobs_total,
        blobs_confirmed=blobs_confirmed,
        storage_bytes_used=storage_bytes,
        ws_connections_active=ws_connections,
        redis_memory_bytes=redis_memory,
    )


# ── User management ───────────────────────────────────────────────────────────

async def list_users(
    db: AsyncSession,
    offset: int = 0,
    limit: int = 50,
    search: str | None = None,
) -> UserListResponse:
    base = select(User)
    if search:
        pattern = f"%{search}%"
        base = base.where(User.email.ilike(pattern) | User.display_name.ilike(pattern))

    total = (
        await db.scalar(
            select(func.count()).select_from(base.subquery())
        )
    ) or 0

    users = (
        await db.scalars(base.order_by(User.created_at.desc()).offset(offset).limit(limit))
    ).all()

    summaries = []
    for u in users:
        device_count = (
            await db.scalar(
                select(func.count()).select_from(Device).where(Device.user_id == u.id)
            )
        ) or 0
        entry_count = (
            await db.scalar(
                select(func.count()).select_from(SyncEntry).where(SyncEntry.user_id == u.id)
            )
        ) or 0
        summaries.append(
            UserAdminSummary(
                id=u.id,
                email=u.email,
                display_name=u.display_name,
                email_verified=u.email_verified,
                suspended_at=u.suspended_at,
                blob_bytes_used=u.blob_bytes_used,
                blob_bytes_quota=u.blob_bytes_quota,
                device_count=device_count,
                entry_count=entry_count,
                created_at=u.created_at,
            )
        )

    return UserListResponse(users=summaries, total=total, offset=offset, limit=limit)


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> UserAdminDetail:
    u = await db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    device_count = (
        await db.scalar(
            select(func.count()).select_from(Device).where(Device.user_id == u.id)
        )
    ) or 0
    entry_count = (
        await db.scalar(
            select(func.count()).select_from(SyncEntry).where(SyncEntry.user_id == u.id)
        )
    ) or 0

    return UserAdminDetail(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        email_verified=u.email_verified,
        suspended_at=u.suspended_at,
        identity_pubkey=u.identity_pubkey,
        blob_bytes_used=u.blob_bytes_used,
        blob_bytes_quota=u.blob_bytes_quota,
        device_count=device_count,
        entry_count=entry_count,
        created_at=u.created_at,
        updated_at=u.updated_at,
    )


async def update_quota(db: AsyncSession, user_id: uuid.UUID, quota_bytes: int) -> None:
    u = await db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    u.blob_bytes_quota = quota_bytes
    await db.commit()


async def set_suspended(db: AsyncSession, user_id: uuid.UUID, suspend: bool) -> None:
    u = await db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    u.suspended_at = _now_ms() if suspend else None
    await db.commit()


async def delete_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    u = await db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await db.delete(u)
    await db.commit()
