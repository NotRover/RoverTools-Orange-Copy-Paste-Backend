"""Give devices a stable identity so re-logins stop minting duplicate rows

`register_device` inserted unconditionally, so every sign-in that could not
reuse a device id from client-side state added another row. One laptop ended up
listed many times, and the old rows kept the wrapped UMK that install could no
longer reach.

Two changes. The partial unique index makes `(user_id, device_pubkey)` the
device's identity - the key whose private half sits in that machine's keychain
and decrypts its `wrapped_umk` - so registration can be an upsert. It covers
only live rows with a key: `device_pubkey` is nullable for pre-E2E rows, and a
revoked row must not block the same machine registering again.

`fingerprint` is a salted hash of a machine id, computed client-side, and is a
grouping hint only. It is deliberately not part of the unique key: machines
imaged from one base share a machine id, and matching on it would let a clone
claim the original's row.

Additive and non-destructive. Existing rows keep a null fingerprint and are
unaffected by the index unless they already carry a key, in which case any
genuine duplicates must be resolved before this applies - see the guard below.

Revision ID: 0015
Revises: 0014
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("fingerprint", sa.String(length=64), nullable=True))

    # Rows that predate the upsert can already hold the same key twice for one
    # user - the duplicates this migration exists to stop. Revoke all but the
    # most recently seen of each set so the unique index below can be created.
    # Revoking rather than deleting: the row may still be referenced, and a
    # revoked device is already a state the product understands.
    op.execute(
        """
        UPDATE devices d
           SET revoked = TRUE, wrapped_umk = NULL
         WHERE d.revoked IS FALSE
           AND d.device_pubkey IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM devices o
                WHERE o.user_id = d.user_id
                  AND o.device_pubkey = d.device_pubkey
                  AND o.revoked IS FALSE
                  AND (o.last_seen_at, o.id) > (d.last_seen_at, d.id)
           )
        """
    )

    op.create_index(
        "uq_devices_user_pubkey_live",
        "devices",
        ["user_id", "device_pubkey"],
        unique=True,
        postgresql_where=sa.text("device_pubkey IS NOT NULL AND revoked IS FALSE"),
    )


def downgrade() -> None:
    op.drop_index("uq_devices_user_pubkey_live", table_name="devices")
    op.drop_column("devices", "fingerprint")
