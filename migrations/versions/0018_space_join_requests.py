"""Join approval: a code gets you a knock at the door, not a membership

`spaces.invite_code` is a multi-use bearer capability - eight characters, one per
space, no rotate route - and redeeming it granted membership outright. A forwarded
mail or a screenshot in a group chat was therefore a silent join, and the owner
found out by noticing a name in the member list. The code was doing two jobs at
once, introduction and authorisation. This splits them.

`space_join_requests` - redeeming a code now writes a row here instead of a
membership. Somebody already inside approves it, and the approval carries the
wrapped Space Key, so the requester goes from pending straight to readable in one
step. That is strictly faster than what it replaces: a code-joiner already had to
wait for somebody's app to wrap a key for them, they just waited *inside* the
space with nothing to read.

A request is requester-initiated, so it shares almost nothing with an invite: no
inviter, no invitee_email, no email delivery, no expiry. Overloading
`space_invites` would leave half its columns null and put a discriminator in
every query.

The unique index on (space_id, user_id) is the abuse cap. A leaked code can now
raise requests where before it walked straight in, and one row per person per
space is what stops those stacking. Declined rows are kept for the same reason:
deleting one would let the same code produce a fresh knock every time.

`spaces.members_can_approve` - the one new control, the owner's to set. False
means the owner alone approves; true extends it to any member. It grants exactly
that and nothing else: renaming, deleting and removing members stay the owner's,
because a removal forces a rekey of the whole space.

Additive only. Existing spaces default to owner-only approval, and every space
that predates this migration reads correctly with no join-request rows at all.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "spaces",
        sa.Column(
            "members_can_approve",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "space_join_requests",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "space_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", PGUUID(as_uuid=True), nullable=False),
        # pending | approved | declined
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        # The approver's ring, wrapped for the requester's identity key. Moved
        # into the membership row on approval and cleared here, the way an
        # invite's pre-wrap is.
        sa.Column("wrapped_space_keys", sa.Text(), nullable=True),
        sa.Column("wrapped_by", PGUUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("decided_at", sa.BigInteger(), nullable=True),
        sa.Column("decided_by", PGUUID(as_uuid=True), nullable=True),
    )
    # One row per person per space - the cap that stops a leaked code stacking
    # knocks, and what a declined row relies on to stay in the way.
    op.create_index(
        "ix_join_requests_space_user",
        "space_join_requests",
        ["space_id", "user_id"],
        unique=True,
    )
    # The approver's list: pending rows for one space.
    op.create_index(
        "ix_join_requests_space_status",
        "space_join_requests",
        ["space_id", "status"],
    )
    # The requester's own view across every space they have knocked on.
    op.create_index("ix_join_requests_user", "space_join_requests", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_join_requests_user", table_name="space_join_requests")
    op.drop_index("ix_join_requests_space_status", table_name="space_join_requests")
    op.drop_index("ix_join_requests_space_user", table_name="space_join_requests")
    op.drop_table("space_join_requests")
    op.drop_column("spaces", "members_can_approve")
