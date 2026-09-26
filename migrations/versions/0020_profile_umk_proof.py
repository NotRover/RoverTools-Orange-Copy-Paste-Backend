"""Add profiles.umk_proof_hash and profiles.pw_wrapped_umk_prev

A bearer token used to be enough to replace the account's UMK envelopes or to
wrap the UMK for a new device. A stolen access token could therefore swap the
password envelope for one under a key the attacker chose, or cut the owner's
devices off. The routes that change key material now take `X-Umk-Proof`: 32
bytes the client derives from the UMK itself, so only a caller that has unlocked
the account can produce it. The server keeps only `sha256(proof)` and compares
against that; the proof is not a key and opens nothing here.

`umk_proof_hash` is set on first use (trust on first use), so existing accounts
start NULL and are filled in by the first proof-carrying request.

`pw_wrapped_umk_prev` keeps the envelope an account reset replaced, so a reset
started from a recovery-link session is not the one-way loss of the old key.

Additive, nullable, non-destructive.

Revision ID: 0020
Revises: 0019
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("umk_proof_hash", sa.Text(), nullable=True))
    op.add_column("profiles", sa.Column("pw_wrapped_umk_prev", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("profiles", "pw_wrapped_umk_prev")
    op.drop_column("profiles", "umk_proof_hash")
