"""Addressed invites + profile email mirror

Two additions supporting persistent in-app invitations:

- ``profiles.email`` — lowercased mirror of the Supabase email claim, captured
  at bootstrap. Supabase remains the identity authority; this copy exists so an
  invite addressed to an email can be resolved to a user (for the live
  ``invite:received`` push and sent-invite status) without an Admin API call.

- ``group_invites`` — the addressed counterpart to a group's bearer invite
  code: one row per (group, invitee email), surviving offline invitees and
  giving inviters visibility into acceptance. Status lifecycle:
  pending → accepted | declined | revoked; expiry is judged against
  ``expires_at`` at read time (no sweeper).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("email", sa.String(length=320), nullable=True))
    op.create_index("ix_profiles_email", "profiles", ["email"])

    op.create_table(
        "group_invites",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("inviter_id", UUID(as_uuid=True), nullable=False),
        sa.Column("invitee_email", sa.String(length=320), nullable=False),
        sa.Column("invitee_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_group_invites_group_id", "group_invites", ["group_id"])
    op.create_index("ix_group_invites_inviter_id", "group_invites", ["inviter_id"])
    op.create_index("ix_group_invites_invitee_email", "group_invites", ["invitee_email"])
    op.create_index("ix_group_invites_invitee_user_id", "group_invites", ["invitee_user_id"])


def downgrade() -> None:
    op.drop_table("group_invites")
    op.drop_index("ix_profiles_email", table_name="profiles")
    op.drop_column("profiles", "email")
