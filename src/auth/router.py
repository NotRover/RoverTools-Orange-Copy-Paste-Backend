"""Auth-adjacent endpoints.

Registration, email verification, login, token refresh, and password reset are
all handled by Supabase Auth — the client talks to Supabase directly for those.
This router owns only what the app itself must store: the profile (KDF salt +
identity key), device registration, and E2E key wrapping.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src import realtime as rt
from src.auth import schemas, service
from src.database import get_db
from src.dependencies import get_current_claims, get_current_user_id, get_current_user_only, get_redis

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Bootstrap ──────────────────────────────────────────────────────────────────


@router.post("/bootstrap", response_model=schemas.BootstrapResponse)
async def bootstrap(
    body: schemas.BootstrapRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict = Depends(get_current_claims),
):
    """Idempotently ensure a profile exists and return the KDF salt plus the
    wrapped-UMK envelope (if set) the client needs to unlock its data. Called
    right after Supabase login. Mirrors the token's email claim into the profile
    so addressed invites can be resolved to this user, and the provider avatar
    URL so member lists can show a picture."""
    metadata = claims.get("user_metadata") or {}
    # GoTrue copies the Google profile picture into user_metadata; the key is
    # "avatar_url" for the OAuth flow and "picture" on the raw OIDC claim.
    avatar_url = metadata.get("avatar_url") or metadata.get("picture")
    # The provider's name is a better identity than anything a client can guess,
    # so it stands in when the client sends none. Reading it here rather than
    # client-side means every client benefits without shipping an update.
    claim_name = metadata.get("full_name") or metadata.get("name")
    display_name = body.display_name or (claim_name if isinstance(claim_name, str) else None)
    profile = await service.ensure_profile(
        db,
        claims["sub"],
        display_name,
        claims.get("email"),
        avatar_url if isinstance(avatar_url, str) else None,
    )
    return schemas.BootstrapResponse(
        user_id=profile.id,
        kdf_salt=profile.kdf_salt,
        display_name=profile.display_name,
        avatar_url=profile.avatar_url,
        wrapped_umk=profile.pw_wrapped_umk,
        recovery_wrapped_umk=profile.recovery_wrapped_umk,
    )


@router.put("/umk", status_code=204)
async def set_wrapped_umk(
    body: schemas.SetWrappedUmkRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    """Store the password-wrapped UMK envelope for the current account.

    Set once on first setup; replaced when the account password changes. The
    server only ever holds the wrapped blob — never the key.

    Requires: Bearer token (Supabase JWT).
    """
    await service.set_wrapped_umk(db, user_id, body.wrapped_umk)


@router.put("/umk/recovery", status_code=204)
async def set_recovery_wrapped_umk(
    body: schemas.SetRecoveryUmkRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    """Store the recovery-code-wrapped UMK envelope for the current account.

    The second envelope holding the same key, wrapped under a secret the user
    keeps rather than one they remember - so a forgotten password is survivable
    on a machine that has never signed in. Replacing it revokes the previous
    recovery code, which is what regenerating one does.

    Opaque to the server, same as PUT /umk: it stores a blob it cannot open.

    Requires: Bearer token (Supabase JWT).
    """
    await service.set_recovery_wrapped_umk(db, user_id, body.recovery_wrapped_umk)


@router.delete("/umk/recovery", status_code=204)
async def clear_recovery_wrapped_umk(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    """Drop the recovery envelope for the current account.

    An envelope can outlive the key inside it: an account that starts over with a
    fresh UMK leaves one that would hand a recovering client a dead key. Clearing
    it is also how the client is told to ask for a new code.

    Idempotent - clearing an account that has none is still a 204.

    Requires: Bearer token (Supabase JWT).
    """
    await service.clear_recovery_wrapped_umk(db, user_id)


# ── Device management ─────────────────────────────────────────────────────────


@router.post("/devices", response_model=schemas.RegisterDeviceResponse, status_code=201)
async def register_device(
    body: schemas.RegisterDeviceRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    """Register a new device for the current user and return its device id.

    Requires: Bearer token (Supabase JWT).
    """
    device = await service.register_device(db, user_id, body)
    return schemas.RegisterDeviceResponse(device_id=device.id)


@router.get("/devices", response_model=list[schemas.DeviceOut])
async def list_devices(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    user_id: str = Depends(get_current_user_only),
):
    """List all registered devices for the current user, with a presence
    snapshot (`online`) resolved from Redis at request time.

    Requires: Bearer token (Supabase JWT).
    """
    devices = await service.get_user_devices(db, user_id)
    out = []
    for d in devices:
        item = schemas.DeviceOut.model_validate(d)
        item.online = bool(await redis.exists(rt.presence_key(user_id, str(d.id))))
        out.append(item)
    return out


@router.delete("/devices/{device_id}", status_code=204)
async def revoke_device(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    """Revoke (delete) one of the current user's devices.

    Requires: Bearer token (Supabase JWT).
    """
    await service.revoke_device(db, device_id, user_id)


# ── Public key management ─────────────────────────────────────────────────────


@router.post("/keys/register", status_code=204)
async def register_keys(
    body: schemas.RegisterPublicKeysRequest,
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Store the identity and device public keys for the calling device.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, device_id = current
    await service.store_public_keys(db, user_id, device_id, body.identity_pubkey, body.device_pubkey)


@router.get("/umk/device", response_model=schemas.DeviceWrappedUmkResponse)
async def get_device_wrapped_umk(
    db: AsyncSession = Depends(get_db),
    current: tuple[str, str] = Depends(get_current_user_id),
):
    """Return the UMK wrapped for the calling device — the silent session
    restore path. 404 when no wrap is stored or the device was revoked, which
    is what makes revocation actually cut a device off: its keychain state
    alone can no longer recover the master key.

    That 404 carries `X-Wrap-Absent: 1`, and the header is part of the contract.
    A client that gets this answer signs the user out and asks for a password,
    so it must not act on a 404 that came from somewhere else — a proxy, a
    rewritten path, or a deployment predating this route. The header is the only
    thing that separates "this device is cut off" from "nobody answered the
    question", and the status alone cannot.

    Requires: Bearer token + X-Device-Id header.
    """
    user_id, device_id = current
    wrapped = await service.get_device_wrapped_umk(db, device_id, user_id)
    if not wrapped:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No device key wrap",
            headers={"X-Wrap-Absent": "1"},
        )
    return schemas.DeviceWrappedUmkResponse(wrapped_umk=wrapped)


@router.post("/devices/{device_id}/key-wrap", status_code=204)
async def wrap_device_umk(
    device_id: uuid.UUID,
    body: schemas.WrapUmkRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_only),
):
    """Store the User Master Key wrapped for the target device's public key.

    Requires: Bearer token (Supabase JWT).
    """
    await service.store_wrapped_umk(db, device_id, user_id, body.wrapped_umk)
