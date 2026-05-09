from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.blobs import service
from src.blobs.schemas import (
    ConfirmUploadBody,
    DownloadUrlResponse,
    QuotaResponse,
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
    user_id, _ = current
    return await service.request_upload(db, user_id, body)


@router.post("/confirm-upload", status_code=204)
async def confirm_upload(
    body: ConfirmUploadBody,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    await service.confirm_upload(db, user_id, body)


@router.get("/{blob_key:path}/download-url", response_model=DownloadUrlResponse)
async def download_url(
    blob_key: str,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    return await service.get_download_url(db, user_id, blob_key)


@router.get("/quota", response_model=QuotaResponse)
async def quota(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, _ = current
    return await service.get_quota(db, user_id)
