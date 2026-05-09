import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import User
from src.blobs import s3
from src.blobs.models import Blob
from src.blobs.schemas import (
    ConfirmUploadBody,
    DownloadUrlResponse,
    QuotaResponse,
    RequestUploadBody,
    RequestUploadResponse,
)

_MAX_BLOB_BYTES = 5_242_880  # 5 MB


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


async def request_upload(
    db: AsyncSession,
    user_id: str,
    body: RequestUploadBody,
) -> RequestUploadResponse:
    if body.size_bytes > _MAX_BLOB_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Blob exceeds 5 MB limit ({body.size_bytes} bytes)",
        )

    uid = uuid.UUID(user_id)
    user = await db.scalar(select(User).where(User.id == uid))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.blob_bytes_used + body.size_bytes > user.blob_bytes_quota:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Storage quota exceeded",
        )

    blob_key = f"{user_id}/{uuid.uuid4().hex}"
    presigned_url = s3.generate_presigned_put(blob_key, body.mime_type)

    blob = Blob(
        key=blob_key,
        user_id=uid,
        mime_type=body.mime_type,
        size_bytes=body.size_bytes,
        checksum=body.checksum,
        confirmed=False,
        created_at=_now_ms(),
    )
    db.add(blob)
    await db.commit()

    return RequestUploadResponse(
        blob_key=blob_key,
        presigned_put_url=presigned_url,
        expires_in_seconds=300,
    )


async def confirm_upload(db: AsyncSession, user_id: str, body: ConfirmUploadBody) -> None:
    uid = uuid.UUID(user_id)
    blob = await db.scalar(select(Blob).where(Blob.key == body.blob_key, Blob.user_id == uid))
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blob not found")
    if blob.confirmed:
        return  # idempotent

    blob.confirmed = True
    # Update user quota usage
    user = await db.scalar(select(User).where(User.id == uid))
    if user:
        user.blob_bytes_used = user.blob_bytes_used + blob.size_bytes
    await db.commit()


async def get_download_url(db: AsyncSession, user_id: str, blob_key: str) -> DownloadUrlResponse:
    uid = uuid.UUID(user_id)
    blob = await db.scalar(select(Blob).where(Blob.key == blob_key, Blob.user_id == uid, Blob.confirmed.is_(True)))
    if not blob:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blob not found")

    url = s3.generate_presigned_get(blob_key)
    return DownloadUrlResponse(presigned_get_url=url, expires_in_seconds=3600)


async def get_quota(db: AsyncSession, user_id: str) -> QuotaResponse:
    uid = uuid.UUID(user_id)
    user = await db.scalar(select(User).where(User.id == uid))
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return QuotaResponse(used_bytes=user.blob_bytes_used, quota_bytes=user.blob_bytes_quota)
