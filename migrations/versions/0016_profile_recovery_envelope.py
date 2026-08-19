"""Add profiles.recovery_wrapped_umk (a second, password-independent envelope)

The account has exactly one envelope holding its UMK, and it is wrapped under a
key derived from the password. That makes a forgotten password unrecoverable on
any machine that has never signed in: the per-device wrap only exists where a
device already registered, and the server has no key of its own by design.

This column is the second envelope, wrapped under a key derived from a recovery
code the user holds. Same `kdf_salt`, different secret, and a distinct AAD
(`umk-recovery-v1` against the password envelope's `umk-envelope-v1`) so the two
can never be mistaken for one another.

Opaque to the server, exactly like `pw_wrapped_umk`: it holds a blob it cannot
open. There is deliberately no server-decryptable recovery path - one would end
the end-to-end guarantee.

Additive, nullable, non-destructive. Existing accounts start null, and those are
precisely the accounts the client asks to save a code at their next sign-in.

Revision ID: 0016
Revises: 0015
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("recovery_wrapped_umk", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("profiles", "recovery_wrapped_umk")
