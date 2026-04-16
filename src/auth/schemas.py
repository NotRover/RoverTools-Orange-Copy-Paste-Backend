import uuid

from pydantic import BaseModel, EmailStr, Field


# ── Request bodies ─────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    device_name: str = Field(default="", max_length=128)
    platform: str = Field(default="unknown", max_length=32)
    app_version: str = Field(default="", max_length=32)
    device_pubkey: str | None = None  # base64 X25519 public key


class RefreshRequest(BaseModel):
    refresh_token: str
    device_id: uuid.UUID


class PasswordResetRequestBody(BaseModel):
    email: EmailStr


class PasswordResetConfirmBody(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class RegisterPublicKeysRequest(BaseModel):
    identity_pubkey: str   # base64 X25519
    device_pubkey: str     # base64 X25519


class WrapUmkRequest(BaseModel):
    wrapped_umk: str       # base64 AES-256-GCM(shared_secret, UMK)


# ── Response bodies ────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    email_verified: bool

    model_config = {"from_attributes": True}


class DeviceOut(BaseModel):
    id: uuid.UUID
    device_name: str
    platform: str
    app_version: str
    last_seen_at: int
    is_current: bool = False

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    device_id: uuid.UUID
    kdf_salt: str
    user: UserOut


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str


class RegisterResponse(BaseModel):
    user_id: uuid.UUID
    message: str = "Registration successful. Please verify your email."
