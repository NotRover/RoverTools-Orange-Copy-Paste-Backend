import base64
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, text
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

# Namespace for the two-key per-user advisory lock taken around quota decisions.
_QUOTA_LOCK_NAMESPACE = 0xB10B
# How long an unconfirmed upload holds its bytes against the quota: the life of
# its presigned PUT URL, after which nothing new can be written through it.
_RESERVATION_MS = s3.PRESIGNED_PUT_TTL * 1000


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


async def _lock_quota(db: AsyncSession, uid: uuid.UUID) -> None:
    """Serialise one account's quota decisions until the transaction ends, so two
    concurrent requests cannot both fit into the same free bytes."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:uid))"),
        {"ns": _QUOTA_LOCK_NAMESPACE, "uid": str(uid)},
    )


async def _used_bytes(db: AsyncSession, uid: uuid.UUID, *, with_reservations: bool = False) -> int:
    """Storage in use, computed on demand from confirmed blobs (no denormalized counter).

    `with_reservations` adds uploads that were handed a URL, are not confirmed
    yet, and could still be written. Without it one account could ask for any
    number of URLs against the same free bytes and then confirm them all.
    """
    counted = Blob.confirmed.is_(True)
    if with_reservations:
        counted = or_(
            counted,
            and_(Blob.confirmed.is_(False), Blob.created_at > _now_ms() - _RESERVATION_MS),
        )
    return (
        await db.scalar(
            select(func.coalesce(func.sum(Blob.size_bytes), 0)).where(Blob.user_id == uid, counted)
        )
    ) or 0


async def _quota_bytes(db: AsyncSession, uid: uuid.UUID) -> int:
    profile = await db.scalar(select(Profile).where(Profile.id == uid))
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return profile.blob_bytes_quota


async def request_upload(db: AsyncSession, user_id: str, body: RequestUploadBody) -> RequestUploadResponse:
    # Size (1 byte to 5 MB) and type are enforced on the body schema.
    uid = uuid.UUID(user_id)
    await _lock_quota(db, uid)
    if await _used_bytes(db, uid, with_reservations=True) + body.size_bytes > await _quota_bytes(db, uid):
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Storage quota exceeded")

    blob_key = f"{user_id}/{uuid.uuid4().hex}"
    presigned_url = s3.generate_presigned_put(blob_key, body.mime_type, body.size_bytes)

    db.add(
        Blob(
            key=blob_key,
            user_id=uid,
            mime_type=body.mime_type,
            size_bytes=body.size_bytes,
            checksum=body.checksum.lower(),
            confirmed=False,
            created_at=_now_ms(),
        )
    )
    await db.commit()

    return RequestUploadResponse(
        blob_key=blob_key, presigned_put_url=presigned_url, expires_in_seconds=s3.PRESIGNED_PUT_TTL
    )


async def confirm_upload(db: AsyncSession, user_id: str, body: ConfirmUploadBody) -> None:
    """Start charging for an upload, once what landed matches what was declared.

    The quota was checked against the size declared at request time and the PUT
    URL is signed for that length, but the store is asked rather than trusted:
    an object of any other size - or, when the upload carried one, any other
    SHA-256 - is deleted and refused with 409. The quota is checked again here,
    because the reservation request-upload counted may have lapsed since.
    """
    uid = uuid.UUID(user_id)
    await _lock_quota(db, uid)
    blob = await db.scalar(select(Blob).where(Blob.key == body.blob_key, Blob.user_id == uid))
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blob not found")
    if blob.confirmed:
        return  # idempotent

    head = s3.head_object(blob.key)
    if head is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload not found in storage")
    if head.size_bytes != blob.size_bytes or (
        head.checksum_sha256 is not None and head.checksum_sha256 != _hex_to_b64(blob.checksum)
    ):
        s3.delete_object(blob.key)
        await db.delete(blob)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload does not match what was declared")

    if await _used_bytes(db, uid) + blob.size_bytes > await _quota_bytes(db, uid):
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Storage quota exceeded")
    blob.confirmed = True
    await db.commit()


def _hex_to_b64(checksum_hex: str) -> str | None:
    """The request's hex SHA-256 in the base64 form the store reports."""
    try:
        return base64.b64encode(bytes.fromhex(checksum_hex)).decode()
    except ValueError:
        return None


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
    if blob.user_id != uid and not await _shares_space_with_blob(db, uid, blob):
        # 404, not 403 — don't confirm the key exists to non-members.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blob not found")
    return DownloadUrlResponse(presigned_get_url=s3.generate_presigned_get(blob_key), expires_in_seconds=3600)


async def _shares_space_with_blob(db: AsyncSession, uid: uuid.UUID, blob: Blob) -> bool:
    """True when a live sync entry *written by the blob's owner* carries this
    blob into a space the caller belongs to. This is what makes shared image
    entries readable by other members — without it, download URLs are
    owner-only and every shared image silently fails to materialize on the
    receiving side.

    Only the owner's own rows count. Otherwise a member could push a row naming
    another account's key into a space of their own and read the blob through
    it. Push refuses such a row; this is the half that does not depend on every
    row already stored having gone through that check."""
    space_lists = (
        await db.scalars(
            select(SyncEntry.space_ids).where(
                SyncEntry.blob_key == blob.key,
                SyncEntry.user_id == blob.user_id,
                SyncEntry.deleted_at.is_(None),
            )
        )
    ).all()
    spaces = {sid for ids in space_lists for sid in (ids or [])}
    if not spaces:
        return False
    membership = await db.scalar(
        select(SpaceMembership.space_id)
        .where(SpaceMembership.user_id == uid, SpaceMembership.space_id.in_(spaces))
        .limit(1)
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
