"""Comments on entries shared into a space

A member can leave a remark on any entry in a space they belong to. Scoped to
the space rather than the entry: the same clipboard item can sit in two spaces,
and a remark meant for one team should not surface in the other.

The entry is addressed by `(client_id, entry_type)` — the same pair every other
space route uses — rather than by a foreign key, because a `sync_entries` row is
per-account and there is no single row to point at.

Body and key are ciphertext, so mentions are invisible here too. Rows die with
their space via ON DELETE CASCADE; there is no tombstone, because comments are
never stored on the client.

Revision ID: 0014
Revises: 0013
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "space_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "space_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("encrypted_body", sa.Text(), nullable=False),
        sa.Column("wrapped_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_space_comments_space_id", "space_comments", ["space_id"])
    op.create_index("ix_space_comments_author_id", "space_comments", ["author_id"])
    # Reading a thread and counting a space both go through the space id first.
    op.create_index(
        "ix_space_comments_thread",
        "space_comments",
        ["space_id", "client_id", "entry_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_space_comments_thread", table_name="space_comments")
    op.drop_index("ix_space_comments_author_id", table_name="space_comments")
    op.drop_index("ix_space_comments_space_id", table_name="space_comments")
    op.drop_table("space_comments")
