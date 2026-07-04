"""Migrate to Supabase Auth + Postgres model

- users -> profiles (Supabase owns identity: drop email/password/verified/suspended)
- profiles.id now equals Supabase auth.users.id (app-level link, no cross-schema FK)
- devices: drop refresh_token_hash (Supabase owns sessions)
- groups.max_members: NULL now means unlimited (was 0)
- blobs: drop unused entry_id
- default per-user blob quota lowered to 50 MB

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_QUOTA_50MB = "52428800"
_QUOTA_500MB = "524288000"


def upgrade() -> None:
    # ── users -> profiles ────────────────────────────────────────────────────
    op.rename_table("users", "profiles")
    op.drop_column("profiles", "email")
    op.drop_column("profiles", "password_hash")
    op.drop_column("profiles", "email_verified")
    op.drop_column("profiles", "suspended_at")
    op.drop_column("profiles", "blob_bytes_used")
    op.alter_column("profiles", "blob_bytes_quota", server_default=_QUOTA_50MB)

    # ── devices ──────────────────────────────────────────────────────────────
    op.drop_column("devices", "refresh_token_hash")

    # ── groups: NULL = unlimited (migrate existing 0 sentinels) ──────────────
    op.alter_column("groups", "max_members", existing_type=sa.Integer(), nullable=True, server_default=None)
    op.execute("UPDATE groups SET max_members = NULL WHERE max_members = 0")

    # ── blobs ────────────────────────────────────────────────────────────────
    op.drop_column("blobs", "entry_id")


def downgrade() -> None:
    op.add_column("blobs", sa.Column("entry_id", UUID(as_uuid=True), nullable=True))

    op.execute("UPDATE groups SET max_members = 0 WHERE max_members IS NULL")
    op.alter_column("groups", "max_members", existing_type=sa.Integer(), nullable=False, server_default="0")

    op.add_column("devices", sa.Column("refresh_token_hash", sa.Text(), nullable=True))

    op.alter_column("profiles", "blob_bytes_quota", server_default=_QUOTA_500MB)
    op.add_column("profiles", sa.Column("blob_bytes_used", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("profiles", sa.Column("suspended_at", sa.BigInteger(), nullable=True))
    op.add_column("profiles", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("profiles", sa.Column("password_hash", sa.Text(), nullable=False, server_default=""))
    op.add_column("profiles", sa.Column("email", sa.String(320), nullable=True))
    op.rename_table("profiles", "users")
