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


# ── Device registration ─────────────────────────────────────────────────────────


class RegisterDeviceRequest(BaseModel):
    device_name: str = Field(default="", max_length=128)
    platform: str = Field(default="unknown", max_length=32)
    app_version: str = Field(default="", max_length=32)
    device_pubkey: str | None = None  # base64 X25519 public key


class RegisterDeviceResponse(BaseModel):
    device_id: uuid.UUID


class DeviceOut(BaseModel):
    id: uuid.UUID
    device_name: str
    platform: str
    app_version: str
    last_seen_at: int

    model_config = {"from_attributes": True}


# ── Public key management ────────────────────────────────────────────────────────


class RegisterPublicKeysRequest(BaseModel):
    identity_pubkey: str  # base64 X25519
    device_pubkey: str  # base64 X25519


class WrapUmkRequest(BaseModel):
    wrapped_umk: str  # base64 AES-256-GCM(shared_secret, UMK)
