import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class Space(Base):
    """A shared clipboard/notes space — the single sharing primitive.

    Replaces the old pool-group / Live Share split: every space is persistent,
    realtime, and encrypted client-side under a Space Key the server never sees.
    What flows into a space (send filters) and what happens to incoming entries
    (auto-copy) are client-side choices; the server only routes ciphertext.
    """

    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    invite_code: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    invite_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Owner's choice: may someone who joins later read entries from before they joined?
    share_history: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SpaceInvite(Base):
    """A persistent, addressed invitation into a space.

    The space's `invite_code` is a bearer secret — anyone holding it can join.
    An invite row is the *addressed* counterpart: it targets one email, survives
    the invitee being offline, and gives the inviter visibility into whether it
    was accepted. Accepting an invite joins the space directly by invite id; the
    emailed code path stays as the fallback for invitees without an account yet.
    """

    __tablename__ = "space_invites"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True
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


class SpaceMembership(Base):
    __tablename__ = "space_memberships"

    space_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")  # 'owner' | 'member'
    # This member's Space Key *keyring*, X25519-wrapped for their identity key:
    # a JSON array of wrapped keys, newest first. An array rather than one key so
    # a member who restarts after a rekey can still decrypt entries written under
    # earlier keys — previous keys live nowhere else. Opaque to the server.
    wrapped_space_keys: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Oldest entry this member may be served, resolved from the space's
    # `share_history` at join time. NULL = no floor (full history).
    history_from_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    joined_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
