"""Space Key handover: any member can distribute, and a ring can be verified

Four additive columns, one per defect or capability. Nothing is dropped, and
every existing row reads correctly with all four null.

`space_memberships.wrapped_by` — who wrapped this member's keyring. Until now the
only writer was the space owner, so the recipient could assume the counterparty
for its X25519 shared secret. Once any member may hand a key over, the recipient
has to be told whose public key to use. Null keeps meaning "the owner", so rows
written before this migration still open.

`spaces.key_fingerprint` — a truncated hash of the newest Space Key, written by
the owner when it mints one. The server stores whatever wrapped keyring it is
handed and cannot check it; with more than one possible writer, a member could
hand a newcomer a key that is not this space's and the unwrap would succeed
anyway. This is what a recipient checks the ring against before adopting it. A
hash of 32 random bytes tells the server nothing about the key.

`spaces.rekey_requested_at` — the rekey signal, moved out of band. A member
leaving used to clear *every* keyring, the owner's included, and the owner's ring
lives only in memory: if their app restarted before redistributing, the previous
keys were gone and everything ever shared in the space became unreadable for
everyone, permanently. Now the removal clears the other members' wraps and sets
this instead, so the owner's own wrap survives to be recovered from.

`space_invites.wrapped_space_keys` — the keyring pre-wrapped for an invitee, set
by the inviter when the invite is created and moved into the membership row on
accept. It is what makes a new member able to read the space the moment they
join, with nobody else online.

Revision ID: 0017
Revises: 0016
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "space_memberships",
        sa.Column("wrapped_by", PGUUID(as_uuid=True), nullable=True),
    )
    op.add_column("spaces", sa.Column("key_fingerprint", sa.Text(), nullable=True))
    op.add_column("spaces", sa.Column("rekey_requested_at", sa.BigInteger(), nullable=True))
    op.add_column("space_invites", sa.Column("wrapped_space_keys", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("space_invites", "wrapped_space_keys")
    op.drop_column("spaces", "rekey_requested_at")
    op.drop_column("spaces", "key_fingerprint")
    op.drop_column("space_memberships", "wrapped_by")
