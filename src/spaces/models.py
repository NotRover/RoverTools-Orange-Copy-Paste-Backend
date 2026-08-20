import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy import Index
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
    # Truncated hash of the newest Space Key, written by the owner's client when
    # it mints one. The server cannot check a wrapped keyring it is handed, and
    # any member may hand one over, so this is what a recipient verifies a ring
    # against before adopting it. Reveals nothing: it hashes 32 random bytes.
    key_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when a departure invalidates the Space Key; cleared when the owner
    # redistributes. The signal used to be "every keyring is null", which meant
    # clearing the owner's too - and their ring lives only in memory, so a
    # restart in between lost the previous keys for good.
    rekey_requested_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
    # The space's keyring, wrapped by the inviter for the invitee's identity key
    # before they are a member. Moved into the membership row on accept, which is
    # what lets a new member read the space immediately with nobody else online.
    # Opaque to the server, like every other wrap.
    wrapped_space_keys: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    # Whose identity key wrapped the keyring above, so the recipient knows which
    # counterparty to compute its shared secret against. Null means the owner,
    # which is every row written before members could hand keys over.
    wrapped_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Oldest entry this member may be served, resolved from the space's
    # `share_history` at join time. NULL = no floor (full history).
    history_from_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    joined_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SpaceComment(Base):
    """One comment written by a member on one entry shared into a space.

    Scoped to the space, not to the entry: the same clipboard item can sit in
    two spaces, and a remark meant for one team should not surface in the other.
    The entry is addressed the way every other space route addresses it, by
    `(client_id, entry_type)`, rather than by a foreign key — a `sync_entries`
    row is per-account, so there is no single row to point at.

    Encrypted the way entries are: the body is sealed under a random per-comment
    key, and that key is wrapped under the Space Key. The server stores both and
    can read neither. Mentions are inside the ciphertext, so who was tagged is
    invisible here too.
    """

    __tablename__ = "space_comments"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)  # 'clipboard' | 'note'
    author_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)

    # E2E encrypted — ciphertext and an opaque wrapped key, never plaintext.
    encrypted_body: Mapped[str] = mapped_column(Text, nullable=False)
    # The per-comment content key, wrapped under the Space Key current at write
    # time. A member who joined after a rekey still holds the older key in their
    # keyring, so every comment stays readable across rotations.
    wrapped_key: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


# Threads are read per entry and counted per space, and both go through the
# space id first, so one composite index serves them together.
Index("ix_space_comments_thread", SpaceComment.space_id, SpaceComment.client_id, SpaceComment.entry_type)
