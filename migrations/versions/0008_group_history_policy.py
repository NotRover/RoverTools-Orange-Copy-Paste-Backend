"""Group history-access policy

Adds the owner-chosen policy controlling whether someone joining a pool group can
read entries pushed *before* they joined:

- ``groups.share_history`` — the owner's choice for the group. True (default)
  keeps current behaviour: joiners see the group's whole history.
- ``group_memberships.history_from_ts`` — resolved per member at join time.
  NULL means "no floor" (full history); a timestamp means the member is only
  served entries whose ``server_ts`` is at or after it.

The floor is enforced server-side in ``pull_entries``, not cryptographically: a
member holds the group key either way. That is consistent with the server already
being the authority on routing and per-user scoping, and it keeps the key model
simple (one key per group, no versioning). Rotating the key per join — so history
is unreadable rather than merely unserved — would need per-entry key versions and
can be layered on later without changing this column's meaning.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "groups",
        sa.Column("share_history", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "group_memberships",
        sa.Column("history_from_ts", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("group_memberships", "history_from_ts")
    op.drop_column("groups", "share_history")
