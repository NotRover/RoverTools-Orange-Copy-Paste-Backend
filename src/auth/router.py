"""Auth-adjacent endpoints.

Registration, email verification, login, token refresh, and password reset are
all handled by Supabase Auth — the client talks to Supabase directly for those.
This router owns only what the app itself must store: the profile (KDF salt +
identity key), device registration, and E2E key wrapping.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import schemas, service
from src.database import get_db
from src.dependencies import get_current_user_id, get_current_user_only

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Bootstrap ──────────────────────────────────────────────────────────────────


@router.post("/bootstrap", response_model=schemas.BootstrapResponse)
async def bootstrap(
    body: schemas.BootstrapRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    """Idempotently ensure a profile exists and return the KDF salt the client
    needs to derive its UMK. Called right after Supabase login."""
    profile = await service.ensure_profile(db, user_id, body.display_name)
    return schemas.BootstrapResponse(
        user_id=profile.id,
        kdf_salt=profile.kdf_salt,
        display_name=profile.display_name,
    )


# ── Device management ─────────────────────────────────────────────────────────


@router.post("/devices", response_model=schemas.RegisterDeviceResponse, status_code=201)
async def register_device(
    body: schemas.RegisterDeviceRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    device = await service.register_device(db, user_id, body)
    return schemas.RegisterDeviceResponse(device_id=device.id)


@router.get("/devices", response_model=list[schemas.DeviceOut])
async def list_devices(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    devices = await service.get_user_devices(db, user_id)
    return [schemas.DeviceOut.model_validate(d) for d in devices]


@router.delete("/devices/{device_id}", status_code=204)
async def revoke_device(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    await service.revoke_device(db, device_id, user_id)


# ── Public key management ─────────────────────────────────────────────────────


@router.post("/keys/register", status_code=204)
async def register_keys(
    body: schemas.RegisterPublicKeysRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    user_id, device_id = current
    await service.store_public_keys(db, user_id, device_id, body.identity_pubkey, body.device_pubkey)


@router.post("/devices/{device_id}/key-wrap", status_code=204)
async def wrap_device_umk(
    device_id: uuid.UUID,
    body: schemas.WrapUmkRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    await service.store_wrapped_umk(db, device_id, user_id, body.wrapped_umk)
