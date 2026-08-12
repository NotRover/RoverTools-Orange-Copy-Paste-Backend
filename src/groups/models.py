import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
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
    # Owner's choice: may someone who joins later read entries from before they joined?
    share_history: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class GroupInvite(Base):
    """A persistent, addressed invitation into a group.

    The group's `invite_code` is a bearer secret — anyone holding it can join.
    An invite row is the *addressed* counterpart: it targets one email, survives
    the invitee being offline, and gives the inviter visibility into whether it
    was accepted. Accepting an invite joins the group directly by invite id; the
    emailed code path stays as the fallback for invitees without an account yet.
    """

    __tablename__ = "group_invites"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inviter_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    invitee_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)  # lowercased
    # Resolved lazily: at creation when the email matches a known profile, or at
    # accept time. NULL means the invitee has no profile yet (or hasn't logged in
    # since profiles started mirroring emails).
    invitee_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # 'pending' | 'accepted' | 'declined' | 'revoked'
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class GroupMembership(Base):
    __tablename__ = "group_memberships"

    group_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")  # 'owner'|'admin'|'member'
    wrapped_group_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    share_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="clipboard")
    # Oldest entry this member may be served, resolved from the group's
    # `share_history` at join time. NULL = no floor (full history).
    history_from_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    joined_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
