"""Initial auth tables: users and devices

Revision ID: 0001
Revises:
Create Date: 2026-04-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("kdf_salt", sa.String(64), nullable=False),
        sa.Column("identity_pubkey", sa.Text, nullable=True),
        sa.Column("email_verified", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("blob_bytes_used", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("blob_bytes_quota", sa.BigInteger, nullable=False, server_default="524288000"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
    )

    op.create_table(
        "devices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("app_version", sa.String(32), nullable=False, server_default=""),
        sa.Column("device_pubkey", sa.Text, nullable=True),
        sa.Column("wrapped_umk", sa.Text, nullable=True),
        sa.Column("refresh_token_hash", sa.Text, nullable=True),
        sa.Column("revoked", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.Column("last_seen_at", sa.BigInteger, nullable=False),
    )
    op.create_index("idx_devices_user_id", "devices", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_devices_user_id", table_name="devices")
    op.drop_table("devices")
    op.drop_table("users")
