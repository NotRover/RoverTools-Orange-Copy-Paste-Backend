"""Add suspended_at to users for admin account suspension

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("suspended_at", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "suspended_at")
