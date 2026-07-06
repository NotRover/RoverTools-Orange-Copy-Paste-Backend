"""Add profiles.pw_wrapped_umk (envelope-wrapped UMK)

Stores the random User Master Key wrapped under the password-derived key so the
encryption key is decoupled from the password. Nullable: NULL means the account
has not established a UMK yet (first setup writes it via PUT /auth/umk).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("pw_wrapped_umk", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("profiles", "pw_wrapped_umk")
