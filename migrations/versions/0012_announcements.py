"""Announcements: server-authored messages to users

The one table here that holds plaintext, and only because it holds the
*service's* words rather than the user's - a maintenance window, a quota note,
a message to one account. Nothing in it is derived from an entry, a note, or a
space name, so there is nothing the server would have had to decrypt to write it.

``user_id`` null means everyone. One table rather than two, because the read
query asks the same question either way ("what is this account owed") and a
broadcast is just the row that skipped the address.

Revision ID: 0012
Revises: 0011
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # No FK to profiles: an announcement is a record of what was said, and
        # deleting an account should not rewrite that. Unaddressed rows (null)
        # are broadcasts.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default="announcement"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("data", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
    )
    # The read path is always "mine or broadcast, newer than my watermark", so
    # both columns in the filter get an index.
    op.create_index("ix_announcements_user_id", "announcements", ["user_id"])
    op.create_index("ix_announcements_created_at", "announcements", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_announcements_created_at", table_name="announcements")
    op.drop_index("ix_announcements_user_id", table_name="announcements")
    op.drop_table("announcements")
