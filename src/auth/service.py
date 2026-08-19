import base64
import os
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import Device, Profile
from src.config import settings


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _generate_kdf_salt() -> str:
    return base64.b64encode(os.urandom(16)).decode()


# ── Profile bootstrap ───────────────────────────────────────────────────────────


async def ensure_profile(
    db: AsyncSession,
    user_id: str,
    display_name: str | None,
    email: str | None = None,
    avatar_url: str | None = None,
) -> Profile:
    """Get-or-create the profile for a Supabase user. Generates the KDF salt on
    first call; the salt is stable thereafter (it seeds UMK derivation). The
    email claim is mirrored (lowercased) so invites can address this user, and
    the provider avatar URL so member lists can show a picture."""
    uid = uuid.UUID(user_id)
    profile = await db.scalar(select(Profile).where(Profile.id == uid))
    now = _now_ms()
    normalized_email = email.lower() if email else None
    # Fallback name from the email local-part: profiles created by clients that
    # pass no display_name would otherwise render as blank members everywhere.
    fallback_name = normalized_email.split("@")[0] if normalized_email else ""

    if profile is None:
        profile = Profile(
            id=uid,
            display_name=display_name or fallback_name,
            email=normalized_email,
            avatar_url=avatar_url,
            kdf_salt=_generate_kdf_salt(),
            blob_bytes_quota=settings.default_blob_quota_bytes,
            created_at=now,
            updated_at=now,
        )
        db.add(profile)
        await db.commit()
        await db.refresh(profile)
        return profile

    changed = False
    if display_name is not None and display_name != profile.display_name:
        profile.display_name = display_name
        changed = True
    elif not profile.display_name and fallback_name:
        # Heal profiles that were created blank before the fallback existed.
        profile.display_name = fallback_name
        changed = True
    if normalized_email and normalized_email != profile.email:
        profile.email = normalized_email
        changed = True
    # Providers rotate avatar URLs, so follow the claim whenever it moves. A
    # missing claim never clears a stored picture: email/password logins for an
    # account that also signs in with Google carry no avatar.
    if avatar_url and avatar_url != profile.avatar_url:
        profile.avatar_url = avatar_url
        changed = True
    if changed:
        profile.updated_at = now
        await db.commit()
        await db.refresh(profile)
    return profile


async def set_wrapped_umk(db: AsyncSession, user_id: str, wrapped_umk: str) -> None:
    """Store (or replace) the password-wrapped UMK envelope for the account.
    Set once on first setup; replaced when the account password changes."""
    profile = await db.scalar(select(Profile).where(Profile.id == uuid.UUID(user_id)))
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    profile.pw_wrapped_umk = wrapped_umk
    profile.updated_at = _now_ms()
    await db.commit()


# ── Device management ─────────────────────────────────────────────────────────


async def register_device(db: AsyncSession, user_id: str, req) -> Device:
    """Register this device, or return the row it already has.

    Idempotent on `(user_id, device_pubkey)`. That key is the device's real
    identity: the matching private key lives in its OS keychain and is what
    decrypts its `wrapped_umk`. Registering used to insert unconditionally, so
    every re-login minted another row and one laptop appeared in the device list
    many times over.

    `fingerprint` is deliberately *not* part of the match. Machines imaged from
    one base share a machine id, and matching on it would let a clone take over
    the original's row - and with it the wrap the original still needs. A
    fingerprint collision with a different pubkey is a different device.
    """
    now = _now_ms()
    existing = None
    if req.device_pubkey:
        existing = await db.scalar(
            select(Device).where(
                Device.user_id == uuid.UUID(user_id),
                Device.device_pubkey == req.device_pubkey,
                Device.revoked.is_(False),
            )
        )
    if existing is not None:
        existing.device_name = req.device_name
        existing.platform = req.platform
        existing.app_version = req.app_version
        existing.last_seen_at = now
        # Backfills rows registered before fingerprints existed.
        if getattr(req, "fingerprint", None):
            existing.fingerprint = req.fingerprint
        await db.commit()
        await db.refresh(existing)
        return existing

    device = Device(
        user_id=uuid.UUID(user_id),
        device_name=req.device_name,
        platform=req.platform,
        app_version=req.app_version,
        device_pubkey=req.device_pubkey,
        fingerprint=getattr(req, "fingerprint", None),
        created_at=now,
        last_seen_at=now,
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return device


async def revoke_device(db: AsyncSession, device_id: uuid.UUID, requesting_user_id: str) -> None:
    device = await db.scalar(select(Device).where(Device.id == device_id))
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if str(device.user_id) != requesting_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your device")
    device.revoked = True
    device.wrapped_umk = None
    await db.commit()


async def get_user_devices(db: AsyncSession, user_id: str) -> list[Device]:
    result = await db.scalars(
        select(Device).where(Device.user_id == uuid.UUID(user_id), Device.revoked.is_(False))
    )
    return list(result.all())


# ── Public key management ─────────────────────────────────────────────────────


async def store_public_keys(
    db: AsyncSession,
    user_id: str,
    device_id: str,
    identity_pubkey: str,
    device_pubkey: str,
) -> None:
    profile = await db.scalar(select(Profile).where(Profile.id == uuid.UUID(user_id)))
    device = await db.scalar(select(Device).where(Device.id == uuid.UUID(device_id)))
    if not profile or not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if device.user_id != profile.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your device")
    profile.identity_pubkey = identity_pubkey
    device.device_pubkey = device_pubkey
    await db.commit()


async def get_device_wrapped_umk(db: AsyncSession, device_id: str, user_id: str) -> str | None:
    """The UMK wrapped for this device, or None when absent or the device was
    revoked (revocation clears the wrap, cutting off silent restore)."""
    device = await db.scalar(select(Device).where(Device.id == uuid.UUID(device_id)))
    if not device or str(device.user_id) != user_id or device.revoked:
        return None
    return device.wrapped_umk


async def store_wrapped_umk(
    db: AsyncSession,
    target_device_id: uuid.UUID,
    requesting_user_id: str,
    wrapped_umk: str,
) -> None:
    target_device = await db.scalar(select(Device).where(Device.id == target_device_id))
    if not target_device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if str(target_device.user_id) != requesting_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your device")
    target_device.wrapped_umk = wrapped_umk
    await db.commit()
