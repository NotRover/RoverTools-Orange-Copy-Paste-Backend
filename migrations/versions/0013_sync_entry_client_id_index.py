"""Index sync_entries on (client_id, entry_type)

Supports the authorship check on push: before inserting a new row, the service
asks whether another account already holds this `client_id` in a space the
pusher is writing into. Without an index that is a sequential scan of every
entry in the table, on the hot path of every first push of every entry.

Not unique - the same `client_id` legitimately exists under several accounts
(one person with two accounts and the same local history). Uniqueness is what
the service decides, per space, not what the schema asserts.

Revision ID: 0013
Revises: 0012
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_sync_entries_client_id_type",
        "sync_entries",
        ["client_id", "entry_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_sync_entries_client_id_type", table_name="sync_entries")
