import uuid

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    group_type: Mapped[str] = mapped_column(String(16), nullable=False, default="pool")  # 'pool' | 'live_share'
    invite_code: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    invite_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = unlimited; 5 for live_share
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class GroupMembership(Base):
    __tablename__ = "group_memberships"

    group_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")  # 'owner'|'admin'|'member'
    wrapped_group_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    share_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="clipboard")
    joined_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
