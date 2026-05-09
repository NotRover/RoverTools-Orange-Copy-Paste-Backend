"""Groups and group memberships tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("group_type", sa.String(16), nullable=False, server_default="pool"),
        sa.Column("invite_code", sa.Text, nullable=True, unique=True),
        sa.Column("invite_expires_at", sa.BigInteger, nullable=True),
        sa.Column("max_members", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_groups_owner_id", "groups", ["owner_id"])

    op.create_table(
        "group_memberships",
        sa.Column("group_id", UUID(as_uuid=True), sa.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("wrapped_group_key", sa.Text, nullable=True),
        sa.Column("share_scope", sa.String(16), nullable=False, server_default="clipboard"),
        sa.Column("joined_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_gm_user", "group_memberships", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_gm_user", table_name="group_memberships")
    op.drop_table("group_memberships")
    op.drop_index("idx_groups_owner_id", table_name="groups")
    op.drop_table("groups")
