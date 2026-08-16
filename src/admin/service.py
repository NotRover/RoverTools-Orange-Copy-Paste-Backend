"""Admin service — aggregate stats and user management.

Account identity (email, verification, ban state) lives in Supabase Auth, so
those operations delegate to the Supabase Admin API. This DB owns profiles,
devices, entries, blobs, and per-user quota.
"""

import uuid
from typing import Awaitable, cast

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src import supabase_admin
from src.admin.schemas import StatsResponse, UserAdminDetail, UserAdminSummary, UserListResponse
from src.auth.models import Device, Profile
from src.blobs.models import Blob
from src.config import settings
from src.sync.models import SyncEntry


def _supabase_configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_service_role_key)


# ── Stats ─────────────────────────────────────────────────────────────────────


async def _online_device_count(redis: Redis) -> int:
    """Cluster-wide online devices, summed from Redis presence sets."""
    total = 0
    try:
        async for key in redis.scan_iter(match="user:*:devices", count=200):
            total += await cast("Awaitable[int]", redis.scard(key))
    except Exception:
        return 0
    return total


async def get_stats(db: AsyncSession, redis: Redis) -> StatsResponse:
    users_total = (await db.scalar(select(func.count()).select_from(Profile))) or 0
    devices_total = (await db.scalar(select(func.count()).select_from(Device))) or 0
    devices_active = (await db.scalar(select(func.count()).select_from(Device).where(Device.revoked.is_(False)))) or 0
    entries_total = (await db.scalar(select(func.count()).select_from(SyncEntry))) or 0
    entries_deleted = (
        await db.scalar(select(func.count()).select_from(SyncEntry).where(SyncEntry.deleted_at.isnot(None)))
    ) or 0
    blobs_total = (await db.scalar(select(func.count()).select_from(Blob))) or 0
    blobs_confirmed = (await db.scalar(select(func.count()).select_from(Blob).where(Blob.confirmed.is_(True)))) or 0
    storage_bytes = (
        await db.scalar(select(func.coalesce(func.sum(Blob.size_bytes), 0)).where(Blob.confirmed.is_(True)))
    ) or 0

    redis_memory: int | None = None
    try:
        info = await redis.info("memory")
        redis_memory = info.get("used_memory")
    except Exception:
        pass

    return StatsResponse(
        users_total=users_total,
        devices_total=devices_total,
        devices_active=devices_active,
        devices_online=await _online_device_count(redis),
        entries_total=entries_total,
        entries_deleted=entries_deleted,
        blobs_total=blobs_total,
        blobs_confirmed=blobs_confirmed,
        storage_bytes_used=storage_bytes,
        redis_memory_bytes=redis_memory,
    )


# ── User management ───────────────────────────────────────────────────────────


async def _counts(db: AsyncSession, uid: uuid.UUID) -> tuple[int, int, int]:
    device_count = (await db.scalar(select(func.count()).select_from(Device).where(Device.user_id == uid))) or 0
    entry_count = (await db.scalar(select(func.count()).select_from(SyncEntry).where(SyncEntry.user_id == uid))) or 0
    used = (
        await db.scalar(
            select(func.coalesce(func.sum(Blob.size_bytes), 0)).where(Blob.user_id == uid, Blob.confirmed.is_(True))
        )
    ) or 0
    return device_count, entry_count, used


async def list_users(db: AsyncSession, offset: int = 0, limit: int = 50, search: str | None = None) -> UserListResponse:
    base = select(Profile)
    if search:
        base = base.where(Profile.display_name.ilike(f"%{search}%"))

    total = (await db.scalar(select(func.count()).select_from(base.subquery()))) or 0
    profiles = (await db.scalars(base.order_by(Profile.created_at.desc()).offset(offset).limit(limit))).all()

    summaries: list[UserAdminSummary] = []
    for p in profiles:
        device_count, entry_count, used = await _counts(db, p.id)
        summaries.append(
            UserAdminSummary(
                id=p.id,
                display_name=p.display_name,
                blob_bytes_used=used,
                blob_bytes_quota=p.blob_bytes_quota,
                device_count=device_count,
                entry_count=entry_count,
                created_at=p.created_at,
            )
        )
    return UserListResponse(users=summaries, total=total, offset=offset, limit=limit)


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> UserAdminDetail:
    p = await db.scalar(select(Profile).where(Profile.id == user_id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    device_count, entry_count, used = await _counts(db, p.id)

    # Enrich with Supabase account state when the admin API is configured.
    email: str | None = None
    email_verified: bool | None = None
    banned: bool | None = None
    if _supabase_configured():
        su = await supabase_admin.get_user(str(user_id))
        if su:
            email = su.get("email")
            email_verified = bool(su.get("email_confirmed_at"))
            banned = bool(su.get("banned_until"))

    return UserAdminDetail(
        id=p.id,
        display_name=p.display_name,
        blob_bytes_used=used,
        blob_bytes_quota=p.blob_bytes_quota,
        device_count=device_count,
        entry_count=entry_count,
        created_at=p.created_at,
        identity_pubkey=p.identity_pubkey,
        updated_at=p.updated_at,
        email=email,
        email_verified=email_verified,
        banned=banned,
    )


async def update_quota(db: AsyncSession, user_id: uuid.UUID, quota_bytes: int) -> None:
    p = await db.scalar(select(Profile).where(Profile.id == user_id))
    if not p:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    p.blob_bytes_quota = quota_bytes
    await db.commit()


async def set_suspended(user_id: uuid.UUID, suspend: bool) -> None:
    """Account suspension is owned by Supabase Auth — ban/unban the user there."""
    await supabase_admin.set_banned(str(user_id), suspend)


async def delete_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Delete app-side data (profile cascades to devices) and the Supabase user."""
    # blobs.user_id has no ON DELETE, so the rows have to go first - and their
    # R2 objects with them, or a deleted account leaves its images in the
    # bucket forever. Unconfirming hands both jobs to the hourly reaper.
    await db.execute(update(Blob).where(Blob.user_id == user_id).values(confirmed=False))
    p = await db.scalar(select(Profile).where(Profile.id == user_id))
    if p:
        await db.delete(p)
    await db.commit()
    await supabase_admin.delete_user(str(user_id))
