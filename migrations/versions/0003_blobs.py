"""Blob metadata table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blobs",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("entry_id", UUID(as_uuid=True), sa.ForeignKey("sync_entries.id"), nullable=True),
        sa.Column("mime_type", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("checksum", sa.Text, nullable=False),
        sa.Column("confirmed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_blobs_user_id", "blobs", ["user_id"])
    op.create_index("idx_blobs_confirmed", "blobs", ["confirmed", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_blobs_confirmed", table_name="blobs")
    op.drop_index("idx_blobs_user_id", table_name="blobs")
    op.drop_table("blobs")
