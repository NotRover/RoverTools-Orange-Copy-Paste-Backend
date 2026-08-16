"""Spaces: fold pool groups and Live Share into one sharing primitive

The two sharing primitives (``groups.group_type`` = ``pool`` / ``live_share``)
become one: a **space** — persistent, realtime, encrypted client-side. What
changes at the schema level:

- ``groups`` → ``spaces``: ``group_type`` and ``max_members`` are gone (every
  space behaves the same; no member cap), everything else carries over.
- ``group_memberships`` → ``space_memberships``: ``share_scope`` is gone (it was
  never enforced server-side; send filtering is now a client-side, per-space
  choice stored in the encrypted settings blob). ``wrapped_group_key`` becomes
  ``wrapped_space_keys`` — a JSON *array* of X25519-wrapped Space Keys, newest
  first, so a member who restarts after a rekey can still decrypt entries
  written under earlier keys. Opaque to the server.
- ``group_invites`` → ``space_invites`` (``group_id`` → ``space_id``).
- ``sync_entries.group_ids`` → ``space_ids``, and a new ``wrapped_keys`` column:
  the per-entry content-key (CEK) envelope — a JSON map of wrapped CEK copies,
  ``"personal"`` (under the owner's UMK) plus one per space id. Content is
  encrypted once under the CEK, which is what lets a single ciphertext fan out
  to several spaces at once.

**Destructive by design**: shared data predating this migration is dropped
(old tables are dropped, not renamed), and ``sync_entries`` is truncated because
pre-CEK ciphertext is undecryptable under the new envelope. Agreed with the
project owner — there is no production data to preserve.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Drop the old sharing tables (memberships/invites cascade from groups) ──
    op.drop_table("group_invites")
    op.drop_table("group_memberships")
    op.drop_table("groups")

    # ── Spaces ────────────────────────────────────────────────────────────────
    op.create_table(
        "spaces",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("invite_code", sa.Text, nullable=True, unique=True),
        sa.Column("invite_expires_at", sa.BigInteger, nullable=True),
        sa.Column("share_history", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_spaces_owner_id", "spaces", ["owner_id"])

    op.create_table(
        "space_memberships",
        sa.Column("space_id", UUID(as_uuid=True), sa.ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("wrapped_space_keys", sa.Text, nullable=True),
        sa.Column("history_from_ts", sa.BigInteger, nullable=True),
        sa.Column("joined_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_sm_user", "space_memberships", ["user_id"])

    op.create_table(
        "space_invites",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("space_id", UUID(as_uuid=True), sa.ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inviter_id", UUID(as_uuid=True), nullable=False),
        sa.Column("invitee_email", sa.String(320), nullable=False),
        sa.Column("invitee_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_space_invites_space_id", "space_invites", ["space_id"])
    op.create_index("ix_space_invites_inviter_id", "space_invites", ["inviter_id"])
    op.create_index("ix_space_invites_invitee_email", "space_invites", ["invitee_email"])
    op.create_index("ix_space_invites_invitee_user_id", "space_invites", ["invitee_user_id"])

    # ── Sync entries: routing array rename + CEK envelope ─────────────────────
    # Pre-CEK ciphertext cannot be read under the new envelope; clear the table
    # rather than keep rows no client will ever decrypt again.
    op.execute("TRUNCATE TABLE sync_entries")
    op.drop_index("idx_sync_entries_groups", table_name="sync_entries")
    op.alter_column("sync_entries", "group_ids", new_column_name="space_ids")
    op.create_index("idx_sync_entries_spaces", "sync_entries", ["space_ids"], postgresql_using="gin")
    op.add_column(
        "sync_entries",
        sa.Column("wrapped_keys", sa.Text, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    # The upgrade is destructive; downgrade restores the old shape, not the data.
    op.drop_column("sync_entries", "wrapped_keys")
    op.drop_index("idx_sync_entries_spaces", table_name="sync_entries")
    op.alter_column("sync_entries", "space_ids", new_column_name="group_ids")
    op.create_index("idx_sync_entries_groups", "sync_entries", ["group_ids"], postgresql_using="gin")

    op.drop_index("ix_space_invites_invitee_user_id", table_name="space_invites")
    op.drop_index("ix_space_invites_invitee_email", table_name="space_invites")
    op.drop_index("ix_space_invites_inviter_id", table_name="space_invites")
    op.drop_index("ix_space_invites_space_id", table_name="space_invites")
    op.drop_table("space_invites")
    op.drop_index("idx_sm_user", table_name="space_memberships")
    op.drop_table("space_memberships")
    op.drop_index("idx_spaces_owner_id", table_name="spaces")
    op.drop_table("spaces")

    op.create_table(
        "groups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("profiles.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("group_type", sa.String(16), nullable=False, server_default="pool"),
        sa.Column("invite_code", sa.Text, nullable=True, unique=True),
        sa.Column("invite_expires_at", sa.BigInteger, nullable=True),
        sa.Column("max_members", sa.Integer, nullable=True),
        sa.Column("share_history", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "group_memberships",
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("wrapped_group_key", sa.Text, nullable=True),
        sa.Column("share_scope", sa.String(16), nullable=False, server_default="clipboard"),
        sa.Column("history_from_ts", sa.BigInteger, nullable=True),
        sa.Column("joined_at", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "group_invites",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inviter_id", UUID(as_uuid=True), nullable=False),
        sa.Column("invitee_email", sa.String(320), nullable=False),
        sa.Column("invitee_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
    )
