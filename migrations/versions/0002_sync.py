"""Sync tables: sync_entries, sync_cursors, user_settings

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_id", sa.Text, nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=True),
        sa.Column("encrypted_content", sa.Text, nullable=False),
        sa.Column("encrypted_metadata", sa.Text, nullable=True),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
        sa.Column("server_ts", sa.BigInteger, nullable=False),
        sa.Column("deleted_at", sa.BigInteger, nullable=True),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("group_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"),
        sa.Column("blob_key", sa.Text, nullable=True),
        sa.Column("blob_size", sa.BigInteger, nullable=True),
        sa.UniqueConstraint("user_id", "client_id", "entry_type", name="uniq_client_entry"),
    )
    op.create_index("idx_sync_entries_user_ts", "sync_entries", ["user_id", "server_ts"])
    op.create_index("idx_sync_entries_device", "sync_entries", ["device_id"])
    op.create_index(
        "idx_sync_entries_groups",
        "sync_entries",
        ["group_ids"],
        postgresql_using="gin",
    )

    op.create_table(
        "sync_cursors",
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_server_ts", sa.BigInteger, nullable=False, server_default="0"),
    )

    op.create_table(
        "user_settings",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("encrypted_blob", sa.Text, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_settings")
    op.drop_table("sync_cursors")
    op.drop_index("idx_sync_entries_groups", table_name="sync_entries")
    op.drop_index("idx_sync_entries_device", table_name="sync_entries")
    op.drop_index("idx_sync_entries_user_ts", table_name="sync_entries")
    op.drop_table("sync_entries")
