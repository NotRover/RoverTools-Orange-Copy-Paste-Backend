import uuid

from sqlalchemy import BigInteger, Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    kdf_salt: Mapped[str] = mapped_column(String(64), nullable=False)  # base64 Argon2id salt
    identity_pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)  # base64 X25519
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspended_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    blob_bytes_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    blob_bytes_quota: Mapped[int] = mapped_column(BigInteger, nullable=False, default=524_288_000)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    devices: Mapped[list["Device"]] = relationship("Device", back_populates="user", cascade="all, delete-orphan")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    platform: Mapped[str] = mapped_column(String(32), nullable=False)  # windows | linux | macos
    app_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    device_pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)  # base64 X25519
    wrapped_umk: Mapped[str | None] = mapped_column(Text, nullable=True)   # AES-GCM(shared_secret, UMK)
    refresh_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="devices")
