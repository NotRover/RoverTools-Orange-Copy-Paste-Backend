"""Profile avatar URL mirror

``profiles.avatar_url`` — mirror of the identity provider's avatar URL (Google,
read from the Supabase ``user_metadata`` claim at bootstrap), so group and
session member payloads can carry a picture for each member.

Only the URL is stored. No image bytes reach the server, and accounts without a
provider avatar (email/password sign-ups) simply leave this null and fall back to
generated initials in the client.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("avatar_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("profiles", "avatar_url")
