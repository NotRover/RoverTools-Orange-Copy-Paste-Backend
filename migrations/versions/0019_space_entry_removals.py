"""Withdrawing an entry from a space leaves a record a catching-up device can read

Taking an entry out of a space strips the space id from `sync_entries.space_ids`,
and pull's space arm matches on that array. So from the moment of removal the row
is not "changed" and not "deleted" to a member - it is absent, indistinguishable
from an entry that never existed. The `space:entry_removed` event was the only
signal that it had gone.

An event reaches whoever is connected at the time. A member whose laptop was shut
came back, pulled, matched nothing, and kept a local copy of withdrawn content
indefinitely - with a healthy Redis, a successful publish, and nothing failing
anywhere. Only the author saw their own withdrawal take effect, because their
`user_id` arm in pull still matches the row.

Every other mutation in this system is durable. An addition or an edit is a row.
Deleting an entry outright is a tombstone that pull returns like any other
change. Removal-from-a-space was the one that existed only as a message in
flight. `space_entry_removals` is what makes it a fact on disk, and pull returns
it alongside entries against the same cursor.

One row per (space, entry), enforced by a unique index which is also the upsert
target: re-sharing and re-removing updates in place and bumps `server_ts` instead
of accumulating history. Only the latest removal matters, and the entry row
carries a newer `server_ts` than any removal preceding it, so a client that
applies removals before entries lands on the right final state in either order.

Additive and reversible. No existing row is touched and no column changes type,
so a deployment that has not yet shipped the reading code is unaffected: the
table simply fills up, and a client that does not know the new pull field ignores
it.

Records are pruned by the maintenance loop after a retention window. That window
is a real limit, not a formality: a device offline for longer than it returns to
find no record and keeps its copy, and a full resync does not repair that,
because the client's merge is additive and never deletes an entry merely for
going unmentioned. See `_REMOVAL_RETENTION_MS` in `src/background.py`.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "space_entry_removals",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("space_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("author_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("removed_by", PGUUID(as_uuid=True), nullable=False),
        sa.Column("server_ts", sa.BigInteger(), nullable=False),
    )
    # One removal fact per entry per space. Unique because it is the upsert
    # conflict target, not merely to prevent duplicates: a re-share/re-remove
    # cycle has to update the existing row and move its timestamp forward.
    op.create_index(
        "ix_space_entry_removals_entry",
        "space_entry_removals",
        ["space_id", "client_id", "entry_type"],
        unique=True,
    )
    # The pull query: removals in one of my spaces, newer than my cursor.
    op.create_index(
        "ix_space_entry_removals_pull",
        "space_entry_removals",
        ["space_id", "server_ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_space_entry_removals_pull", table_name="space_entry_removals")
    op.drop_index("ix_space_entry_removals_entry", table_name="space_entry_removals")
    op.drop_table("space_entry_removals")
