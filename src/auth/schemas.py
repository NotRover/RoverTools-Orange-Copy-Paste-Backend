import uuid

from pydantic import BaseModel, Field


# ── Bootstrap ──────────────────────────────────────────────────────────────────


class BootstrapRequest(BaseModel):
    # Optional: mirror the display name from Supabase user metadata into the profile.
    display_name: str | None = Field(default=None, max_length=128)


class BootstrapResponse(BaseModel):
    user_id: uuid.UUID
    kdf_salt: str
    display_name: str
    # Provider avatar URL (Google), or None for accounts without one.
    avatar_url: str | None = None
    # Random UMK wrapped under the password-derived key (base64 AES-GCM envelope).
    # None on a brand-new account: the client then generates a UMK, wraps it, and
    # stores it via PUT /auth/umk. On return, the client unwraps this to recover
    # the key; a GCM auth failure means the wrong password was entered.
    wrapped_umk: str | None = None
    # Whether the account has a recovery envelope, and the blob itself. None
    # means no recovery code has been saved - the client asks the user to save
    # one at sign-in, because without it a forgotten password is unrecoverable
    # on any machine that has not registered.
    recovery_wrapped_umk: str | None = None


class SetWrappedUmkRequest(BaseModel):
    # base64 AES-GCM envelope: the random UMK wrapped under the password-derived key.
    wrapped_umk: str = Field(max_length=512)


class SetRecoveryUmkRequest(BaseModel):
    # base64 AES-GCM envelope: the same UMK wrapped under the recovery-code key.
    recovery_wrapped_umk: str = Field(max_length=512)


# ── Device registration ─────────────────────────────────────────────────────────


class RegisterDeviceRequest(BaseModel):
    device_name: str = Field(default="", max_length=128)
    platform: str = Field(default="unknown", max_length=32)
    app_version: str = Field(default="", max_length=32)
    device_pubkey: str | None = None  # base64 X25519 public key
    # Salted hash of a machine id (see Device.fingerprint). Optional: an older
    # client sends none, and a machine with no readable id sends one derived
    # from a locally generated seed instead.
    fingerprint: str | None = Field(default=None, max_length=64)


class RegisterDeviceResponse(BaseModel):
    device_id: uuid.UUID


class DeviceOut(BaseModel):
    id: uuid.UUID
    device_name: str
    platform: str
    app_version: str
    last_seen_at: int
    # Lets the client tell its own machine's registrations apart from other
    # devices in the list. Null on rows registered before fingerprints existed.
    fingerprint: str | None = None
    # Presence snapshot at list time (Redis presence key exists). Live updates
    # still flow over WS `device:online`/`device:offline`; this seeds the UI so
    # devices don't all render offline until the next event happens to arrive.
    online: bool = False

    model_config = {"from_attributes": True}


# ── Public key management ────────────────────────────────────────────────────────


class RegisterPublicKeysRequest(BaseModel):
    identity_pubkey: str  # base64 X25519
    device_pubkey: str  # base64 X25519


class WrapUmkRequest(BaseModel):
    wrapped_umk: str  # base64 AES-256-GCM(shared_secret, UMK)


class DeviceWrappedUmkResponse(BaseModel):
    wrapped_umk: str
