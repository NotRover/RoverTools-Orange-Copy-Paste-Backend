import uuid

from sqlalchemy import BigInteger, Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class Profile(Base):
    """App-side profile for a Supabase Auth user.

    `id` equals the Supabase `auth.users.id` (the JWT `sub`); we link at the
    application level (no cross-schema FK to the Supabase-managed auth table).
    Email, password, and verification state live in Supabase — not here. This
    table holds only what the app owns: E2E key material and quota.
    """

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    kdf_salt: Mapped[str] = mapped_column(String(64), nullable=False)  # base64 Argon2id salt (UMK derivation)
    identity_pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)  # base64 X25519
    blob_bytes_quota: Mapped[int] = mapped_column(BigInteger, nullable=False, default=52_428_800)  # 50 MB
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    devices: Mapped[list["Device"]] = relationship("Device", back_populates="profile", cascade="all, delete-orphan")


class Device(Base):
    """A registered install. Session/refresh lifecycle is owned by Supabase Auth;
    this row carries E2E key material (device pubkey, wrapped UMK) and presence
    metadata, plus a soft `revoked` flag for the device-management UX.
    """

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    platform: Mapped[str] = mapped_column(String(32), nullable=False)  # windows | linux | macos
    app_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    device_pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)  # base64 X25519
    wrapped_umk: Mapped[str | None] = mapped_column(Text, nullable=True)  # AES-GCM(shared_secret, UMK)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    profile: Mapped["Profile"] = relationship("Profile", back_populates="devices")
