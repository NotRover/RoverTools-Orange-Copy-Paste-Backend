from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.blobs import service
from src.blobs.schemas import (
    ConfirmUploadBody,
    DownloadUrlResponse,
    QuotaResponse,
    ReleaseUploadBody,
    RequestUploadBody,
    RequestUploadResponse,
)
from src.database import get_db
from src.dependencies import get_current_user_id

router = APIRouter(prefix="/blobs", tags=["blobs"])


@router.post("/request-upload", response_model=RequestUploadResponse)
async def request_upload(
    body: RequestUploadBody,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Request a presigned upload URL for a new blob, subject to quota.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    return await service.request_upload(db, user_id, body)


@router.post("/confirm-upload", status_code=204)
async def confirm_upload(
    body: ConfirmUploadBody,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Confirm a completed blob upload and record it against the user's quota.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    await service.confirm_upload(db, user_id, body)


@router.post("/release", status_code=204)
async def release_upload(
    body: ReleaseUploadBody,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Give back an upload whose entry never reached the server.

    409 while a live entry still references the blob. Declared before the
    catch-all download route so `/release` is not read as a blob key.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    await service.release_upload(db, user_id, body)


@router.get("/{blob_key:path}/download-url", response_model=DownloadUrlResponse)
async def download_url(
    blob_key: str,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Return a presigned download URL for a blob the user may access.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    return await service.get_download_url(db, user_id, blob_key)


@router.get("/quota", response_model=QuotaResponse)
async def quota(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Return the current user's blob storage quota and usage.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, _ = current
    return await service.get_quota(db, user_id)
