import base64
import binascii
import hashlib
import hmac
import logging
import os
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import Device, Profile
from src.config import settings

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _generate_kdf_salt() -> str:
    return base64.b64encode(os.urandom(16)).decode()


# ── Profile bootstrap ───────────────────────────────────────────────────────────

# Longest display name stored, matching the column. A provider's `full_name` claim
# is not bounded by anything of ours.
DISPLAY_NAME_MAX = 128

# Hosts a provider avatar may be served from. The URL is handed to every member
# of a space and rendered by their client, so an arbitrary one would be a way to
# make other people's apps fetch a URL of the uploader's choosing (a tracking
# pixel at minimum). Google is the only provider that sets one today.
_AVATAR_HOSTS = ("lh3.googleusercontent.com",)
_AVATAR_HOST_SUFFIXES = (".googleusercontent.com",)


def clean_display_name(name: object) -> str | None:
    """A claim-supplied name as something safe to store, or None."""
    if not isinstance(name, str):
        return None
    name = name.strip()[:DISPLAY_NAME_MAX]
    return name or None


def clean_avatar_url(url: object) -> str | None:
    """The avatar URL when it is https on an allow-listed host, else None."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return None
    if host in _AVATAR_HOSTS or host.endswith(_AVATAR_HOST_SUFFIXES):
        return url
    return None


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


# ── UMK proof ─────────────────────────────────────────────────────────────────


UMK_PROOF_BYTES = 32


def _proof_required() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="umk_proof_required")


def hash_umk_proof(header: str | None) -> str | None:
    """hex sha256 of the decoded `X-Umk-Proof`, or None when the header is absent.

    400 `invalid_umk_proof` when present but not base64 of exactly 32 bytes: a
    malformed proof is a client bug, not a wrong answer.
    """
    if not header:
        return None
    try:
        raw = base64.b64decode(header, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_umk_proof") from exc
    if len(raw) != UMK_PROOF_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_umk_proof")
    return hashlib.sha256(raw).hexdigest()


def enforce_umk_proof(profile: Profile, header: str | None) -> None:
    """Apply the UMK proof rule to one key-material write. Mutates, does not commit.

    - Stored hash set: the header is required and must hash to it (constant-time
      compare), else 403 `umk_proof_required`.
    - Stored hash NULL (an account no proof-carrying client has touched yet): the
      request proceeds, and a proof it carries is stored - trust on first use.
    """
    presented = hash_umk_proof(header)
    if profile.umk_proof_hash is None:
        if presented is not None:
            profile.umk_proof_hash = presented
        return
    if presented is None or not hmac.compare_digest(presented, profile.umk_proof_hash):
        raise _proof_required()


def is_recovery_session(claims: dict) -> bool:
    """True when the token came from a Supabase recovery link (`amr` method `recovery`)."""
    amr = claims.get("amr")
    if not isinstance(amr, list):
        return False
    for entry in amr:
        method = entry.get("method") if isinstance(entry, dict) else entry
        if method == "recovery":
            return True
    return False


async def _profile_or_404(db: AsyncSession, user_id: str) -> Profile:
    profile = await db.scalar(select(Profile).where(Profile.id == uuid.UUID(user_id)))
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return profile


async def set_wrapped_umk(
    db: AsyncSession,
    user_id: str,
    wrapped_umk: str,
    proof_header: str | None,
    *,
    reset: bool = False,
    claims: dict | None = None,
) -> None:
    """Store (or replace) the password-wrapped UMK envelope for the account.
    Set once on first setup; replaced when the account password changes.

    `reset` is the account-reset path: the user lost the old key and starts over
    with a new UMK, so no proof of the old one can exist. Allowed only from a
    Supabase recovery-link session (`amr` method `recovery`), and only with a
    proof of the *new* UMK, which replaces the stored hash. The replaced envelope
    is kept in `pw_wrapped_umk_prev`. A `reset` from any other session is judged
    like an ordinary write.
    """
    profile = await _profile_or_404(db, user_id)
    if reset and is_recovery_session(claims or {}):
        presented = hash_umk_proof(proof_header)
        if presented is None:
            raise _proof_required()
        if profile.pw_wrapped_umk is not None:
            profile.pw_wrapped_umk_prev = profile.pw_wrapped_umk
        profile.umk_proof_hash = presented
        logger.info("account reset from a recovery session for profile %s", profile.id)
    else:
        enforce_umk_proof(profile, proof_header)
    profile.pw_wrapped_umk = wrapped_umk
    profile.updated_at = _now_ms()
    await db.commit()


async def clear_recovery_wrapped_umk(db: AsyncSession, user_id: str, proof_header: str | None) -> None:
    """Drop the recovery envelope.

    Needed because an envelope can outlive the key it holds: an account that
    starts over gets a brand-new UMK, and the old envelope would then hand a
    recovering client a key that decrypts nothing. Clearing it is also what makes
    the client ask for a fresh code at the next sign-in.
    """
    profile = await _profile_or_404(db, user_id)
    enforce_umk_proof(profile, proof_header)
    profile.recovery_wrapped_umk = None
    profile.updated_at = _now_ms()
    await db.commit()


async def set_recovery_wrapped_umk(
    db: AsyncSession, user_id: str, recovery_wrapped_umk: str, proof_header: str | None
) -> None:
    """Store (or replace) the recovery-code envelope for the account.

    Replacing is how regenerating a code works: the previous code stops opening
    anything the moment this lands, because the blob it could open is gone. Only
    one recovery code is live at a time, on purpose - a code the user believes is
    revoked must not still work.
    """
    profile = await _profile_or_404(db, user_id)
    enforce_umk_proof(profile, proof_header)
    profile.recovery_wrapped_umk = recovery_wrapped_umk
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


async def revoke_device(
    db: AsyncSession, device_id: uuid.UUID, requesting_user_id: str, proof_header: str | None
) -> None:
    """Mark the device revoked and drop its UMK wrap. Needs the UMK proof.

    The router then drops the device cache, publishes `device:revoked` and closes
    the device's sockets. The device's Supabase session is NOT ended: GoTrue's
    admin logout is per user, not per session, and no session id is recorded per
    device. A revoked device can hold a valid access token until it expires, but
    every device-scoped route and the socket refuse it, and it has no wrap left to
    restore the UMK from.
    """
    device = await db.scalar(select(Device).where(Device.id == device_id))
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if str(device.user_id) != requesting_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your device")
    profile = await _profile_or_404(db, requesting_user_id)
    enforce_umk_proof(profile, proof_header)
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
    # The identity keypair is derived from the UMK, so every device of an account
    # derives the same one and an honest client sends the same value forever. A
    # *different* value therefore means the caller does not hold the UMK - it holds
    # a bearer token and is trying to become the account's key. Space Keys are
    # wrapped to whatever sits in this column, so accepting that would hand over
    # every space the account is in from then on. First write wins; a change is
    # refused rather than merged.
    if profile.identity_pubkey and profile.identity_pubkey != identity_pubkey:
        logger.warning("Refused identity pubkey change for profile %s", profile.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Identity key already registered for this account",
        )
    profile.identity_pubkey = identity_pubkey
    # Device keys are per device and legitimately re-registered, so this one stays
    # writable: it only ever unwraps that device's own copy of the UMK.
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
    proof_header: str | None,
) -> None:
    """Wrap the UMK for one of the caller's devices. Needs the UMK proof, and
    refuses a revoked target (409 `device_revoked`): a wrap written there would
    re-open silent restore for a device the owner cut off."""
    target_device = await db.scalar(select(Device).where(Device.id == target_device_id))
    if not target_device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if str(target_device.user_id) != requesting_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your device")
    if target_device.revoked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="device_revoked")
    profile = await _profile_or_404(db, requesting_user_id)
    enforce_umk_proof(profile, proof_header)
    target_device.wrapped_umk = wrapped_umk
    await db.commit()
