import uuid

from sqlalchemy import BigInteger, Boolean, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class SyncEntry(Base):
    __tablename__ = "sync_entries"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)  # 'clipboard' | 'note'
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 'text'|'image'|'html'|'file'

    # E2E encrypted payloads — server stores ciphertext only
    encrypted_content: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Sync bookkeeping
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    server_ts: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Group membership — server-visible for routing; names are inside encrypted_metadata
    group_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default="{}")

    # Blob-backed content (images / files)
    blob_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SyncCursor(Base):
    __tablename__ = "sync_cursors"

    device_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    last_server_ts: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
