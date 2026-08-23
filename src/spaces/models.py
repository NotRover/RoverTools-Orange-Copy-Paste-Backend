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

    `invite_code` introduces a space; it no longer authorises entry to one.
    Redeeming it raises a `SpaceJoinRequest` that somebody inside approves.
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
    # Owner's choice: may any member approve a join request, or only the owner?
    # The whole of the approval policy - deliberately one boolean rather than a
    # third role, because approving is not renaming, deleting, or removing
    # members, and a removal forces a rekey of the entire space.
    members_can_approve: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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


class SpaceJoinRequest(Base):
    """Somebody redeemed this space's invite code and is waiting to be let in.

    The counterpart to `SpaceInvite`, from the other direction. An invite is
    addressed - the owner names an email, which *is* the approval, so that path
    never lands here. A request is anonymous: the code says nothing about who is
    holding it, so a member decides.

    Approval carries the Space Key. Whoever approves is by definition online and
    holding the keyring at that instant - they are the one clicking - so
    `wrapped_space_keys` is written in the same action and moves into the
    membership row, exactly as an invite's pre-wrap does. The requester goes from
    pending to readable in one step, which is faster than the membership this
    replaces: that granted access instantly and then left the joiner unable to
    read anything until somebody else's app happened to be running.

    Declined rows are kept, not deleted. Together with the unique index on
    (space_id, user_id) they are what stops somebody who still holds the code
    from knocking again, and they are the only record the owner has that a
    stranger tried.
    """

    __tablename__ = "space_join_requests"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No per-column index on either of these: the composite unique index below
    # serves space_id lookups by prefix, and the requester's own view goes
    # through ix_join_requests_user. Both are declared at the foot of this file
    # so they match the migration name for name.
    space_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # 'pending' | 'approved' | 'declined'
    # The approver's keyring, wrapped for the requester's identity key. Cleared
    # on approval once it has moved into the membership row - a spent request has
    # no reason to keep key material around.
    wrapped_space_keys: Mapped[str | None] = mapped_column(Text, nullable=True)
    wrapped_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decided_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


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


class SpaceEntryRemoval(Base):
    """A durable record that one entry left one space.

    The only mutation in the system that used to leave no trace. An addition or
    an edit is a row; deleting an entry outright is a tombstone (`deleted_at`)
    that pull returns like any other change. Taking an entry *out of a space*
    strips the space id from `sync_entries.space_ids` - and pull's space arm
    matches on that array, so from that moment the row is not "changed" or
    "deleted" to a member, it is simply absent, indistinguishable from an entry
    that never existed.

    The `space:entry_removed` event was the only signal, and an event reaches
    whoever happens to be connected. A member whose device was closed came back,
    pulled, matched nothing, and kept its copy of withdrawn content forever -
    with a healthy Redis and nothing failing anywhere. This table is what a
    catching-up device reads instead.

    One row per (space, entry): re-sharing and re-removing the same entry
    updates the row and bumps `server_ts` rather than accumulating history.
    Only the latest removal matters, and the entry row itself carries a newer
    `server_ts` than any removal that preceded it, so a client applying
    removals before entries lands on the right final state either way.

    Not a tombstone for the entry. The author keeps their own copy - what was
    withdrawn is the *sharing*, and the wrapped key for this space is dropped
    from the entry in the same transaction that writes this row.
    """

    __tablename__ = "space_entry_removals"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # The client-side entry id, keyed with entry_type exactly as `sync_entries`
    # is - this identifies the entry to a client, which never sees the server id.
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)  # 'clipboard' | 'note'
    # Who shared it, and who took it down. Both travel to the client because the
    # placeholder it shows depends on whether the author withdrew their own post
    # or somebody moderated it - the same distinction `space:entry_removed`
    # carries as `author_id` / `removed_by`.
    author_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    removed_by: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Same clock and same meaning as `sync_entries.server_ts`, because a device
    # pulls both streams against one cursor.
    server_ts: Mapped[int] = mapped_column(BigInteger, nullable=False)


# One removal fact per entry per space, so a re-share/re-remove cycle updates in
# place instead of growing the table. Also the conflict target for that upsert.
Index(
    "ix_space_entry_removals_entry",
    SpaceEntryRemoval.space_id,
    SpaceEntryRemoval.client_id,
    SpaceEntryRemoval.entry_type,
    unique=True,
)
# The pull query: removals in one of my spaces, newer than my cursor.
Index("ix_space_entry_removals_pull", SpaceEntryRemoval.space_id, SpaceEntryRemoval.server_ts)


# Threads are read per entry and counted per space, and both go through the
# space id first, so one composite index serves them together.
Index("ix_space_comments_thread", SpaceComment.space_id, SpaceComment.client_id, SpaceComment.entry_type)

# One knock per person per space. This is the abuse cap: a leaked code can raise
# requests where it used to walk straight in, and the uniqueness is what stops
# those stacking - and what makes a declined row stay in the way.
Index(
    "ix_join_requests_space_user",
    SpaceJoinRequest.space_id,
    SpaceJoinRequest.user_id,
    unique=True,
)
# The approver's list: pending rows for one space.
Index("ix_join_requests_space_status", SpaceJoinRequest.space_id, SpaceJoinRequest.status)
# The requester's own view, across every space they have knocked on.
Index("ix_join_requests_user", SpaceJoinRequest.user_id)
