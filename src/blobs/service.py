import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import Profile
from src.config import settings
from src.blobs import s3
from src.blobs.models import Blob
from src.spaces.models import SpaceMembership
from src.sync.models import SyncEntry
from src.blobs.schemas import (
    ConfirmUploadBody,
    DownloadUrlResponse,
    QuotaResponse,
    ReleaseUploadBody,
    RequestUploadBody,
    RequestUploadResponse,
)

_MAX_BLOB_BYTES = 5_242_880  # 5 MB per-entry hard cap


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


async def _used_bytes(db: AsyncSession, uid: uuid.UUID) -> int:
    """Storage in use, computed on demand from confirmed blobs (no denormalized counter)."""
    return (
        await db.scalar(
            select(func.coalesce(func.sum(Blob.size_bytes), 0)).where(
                Blob.user_id == uid, Blob.confirmed.is_(True)
            )
        )
    ) or 0


async def _quota_bytes(db: AsyncSession, uid: uuid.UUID) -> int:
    profile = await db.scalar(select(Profile).where(Profile.id == uid))
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return profile.blob_bytes_quota


async def request_upload(db: AsyncSession, user_id: str, body: RequestUploadBody) -> RequestUploadResponse:
    if body.size_bytes > _MAX_BLOB_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Blob exceeds 5 MB limit ({body.size_bytes} bytes)",
        )

    uid = uuid.UUID(user_id)
    if await _used_bytes(db, uid) + body.size_bytes > await _quota_bytes(db, uid):
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Storage quota exceeded")

    blob_key = f"{user_id}/{uuid.uuid4().hex}"
    presigned_url = s3.generate_presigned_put(blob_key, body.mime_type)

    db.add(
        Blob(
            key=blob_key,
            user_id=uid,
            mime_type=body.mime_type,
            size_bytes=body.size_bytes,
            checksum=body.checksum,
            confirmed=False,
            created_at=_now_ms(),
        )
    )
    await db.commit()

    return RequestUploadResponse(blob_key=blob_key, presigned_put_url=presigned_url, expires_in_seconds=300)


async def confirm_upload(db: AsyncSession, user_id: str, body: ConfirmUploadBody) -> None:
    uid = uuid.UUID(user_id)
    blob = await db.scalar(select(Blob).where(Blob.key == body.blob_key, Blob.user_id == uid))
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blob not found")
    if blob.confirmed:
        return  # idempotent
    blob.confirmed = True
    await db.commit()


async def release_upload(db: AsyncSession, user_id: str, body: ReleaseUploadBody) -> None:
    """Give back an upload whose entry never landed.

    A blob starts costing quota at confirm-upload, but the row that owns it is
    only created by the push that follows. When that push ends without a row -
    refused by the server, or dropped on an error path - the object is charged to
    an account that cannot reach it. The client calls this instead of leaving it
    for the 7-day unreferenced sweep, which exists for a queued push that may
    still arrive, not for one that never will.

    Refuses while a live entry references the blob: an entry that is doing its
    job must not lose its image because some other push decided to tidy up.
    Idempotent, and silent about blobs that are not the caller's - the same 404
    reasoning as ``get_download_url``.
    """
    uid = uuid.UUID(user_id)
    blob = await db.scalar(select(Blob).where(Blob.key == body.blob_key, Blob.user_id == uid))
    if blob is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blob not found")
    if not blob.confirmed:
        return  # already released, or never confirmed
    referenced = await db.scalar(
        select(SyncEntry.id)
        .where(SyncEntry.blob_key == body.blob_key, SyncEntry.deleted_at.is_(None))
        .limit(1)
    )
    if referenced is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A live entry still references this blob",
        )
    blob.confirmed = False
    await db.commit()


async def release_blob(db: AsyncSession, user_id: str | uuid.UUID, blob_key: str) -> bool:
    """Drop a blob's last reference, so the hourly reaper can delete it.

    Marking it unconfirmed rather than deleting it here does three things: the
    quota stops counting it immediately (``_used_bytes`` sums confirmed blobs
    only), the R2 object is removed by the existing orphan cleanup instead of
    inside a latency-sensitive push, and a mistake stays recoverable until that
    sweep runs.

    Returns True when a blob was released. Missing or already-released blobs
    are not an error - this is called on every image overwrite and delete.
    """
    uid = uuid.UUID(str(user_id))
    blob = await db.scalar(select(Blob).where(Blob.key == blob_key, Blob.user_id == uid))
    if blob is None or not blob.confirmed:
        return False
    blob.confirmed = False
    return True


async def get_download_url(db: AsyncSession, user_id: str, blob_key: str) -> DownloadUrlResponse:
    uid = uuid.UUID(user_id)
    blob = await db.scalar(select(Blob).where(Blob.key == blob_key, Blob.confirmed.is_(True)))
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blob not found")
    if blob.user_id != uid and not await _shares_space_with_blob(db, uid, blob_key):
        # 404, not 403 — don't confirm the key exists to non-members.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blob not found")
    return DownloadUrlResponse(presigned_get_url=s3.generate_presigned_get(blob_key), expires_in_seconds=3600)


async def _shares_space_with_blob(db: AsyncSession, uid: uuid.UUID, blob_key: str) -> bool:
    """True when some live sync entry carries this blob into a space the caller
    belongs to. This is what makes shared image entries readable by other
    members — without it, download URLs are owner-only and every shared image
    silently fails to materialize on the receiving side."""
    entry = await db.scalar(
        select(SyncEntry).where(SyncEntry.blob_key == blob_key, SyncEntry.deleted_at.is_(None))
    )
    if not entry or not entry.space_ids:
        return False
    membership = await db.scalar(
        select(SpaceMembership).where(
            SpaceMembership.user_id == uid,
            SpaceMembership.space_id.in_(entry.space_ids),
        )
    )
    return membership is not None


async def _entry_count(db: AsyncSession, uid: uuid.UUID) -> int:
    """Live rows against ``max_entries_per_user`` - tombstones are not rows the
    account is charged for, so they are excluded here exactly as they are in
    the push path that enforces the cap."""
    return (
        await db.scalar(
            select(func.count())
            .select_from(SyncEntry)
            .where(SyncEntry.user_id == uid, SyncEntry.deleted_at.is_(None))
        )
    ) or 0


async def get_quota(db: AsyncSession, user_id: str) -> QuotaResponse:
    uid = uuid.UUID(user_id)
    return QuotaResponse(
        used_bytes=await _used_bytes(db, uid),
        quota_bytes=await _quota_bytes(db, uid),
        entry_count=await _entry_count(db, uid),
        entry_limit=settings.max_entries_per_user,
        max_entry_bytes=settings.max_entry_bytes,
    )
