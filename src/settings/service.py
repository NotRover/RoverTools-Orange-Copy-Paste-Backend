import uuid

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.settings.models import UserSettings
from src.settings.schemas import SettingsPutRequest, SettingsPutResponse


async def get_settings(db: AsyncSession, user_id: str) -> UserSettings | None:
    uid = uuid.UUID(user_id)
    return await db.scalar(select(UserSettings).where(UserSettings.user_id == uid))


async def put_settings(db: AsyncSession, user_id: str, req: SettingsPutRequest) -> SettingsPutResponse:
    """Write the blob if the stored one allows it, else answer with the stored one.

    With no blob stored, the request is always written. Over a stored blob:

    - without `base_updated_at`, last-write-wins: the request replaces a blob whose
      `updated_at` is not newer than its own;
    - with it, a compare-and-swap: the request replaces only the blob it was merged
      from (stored `updated_at == base_updated_at`), and only forward (its own
      `updated_at` greater than the stored one).

    The condition is the upsert's own `WHERE`, so the check and the write are one
    statement under the row lock. Of two PUTs merged from the same blob, one writes;
    the other checks the condition against the winner's row, fails it, and gets that
    row back as `winner="server"`.
    """
    uid = uuid.UUID(user_id)
    write_if: ColumnElement[bool]
    if req.base_updated_at is None:
        write_if = UserSettings.updated_at <= req.updated_at
    else:
        write_if = and_(
            UserSettings.updated_at == req.base_updated_at,
            UserSettings.updated_at < req.updated_at,
        )

    written = await db.scalar(
        pg_insert(UserSettings)
        .values(
            user_id=uid,
            encrypted_blob=req.encrypted_blob,
            updated_at=req.updated_at,
        )
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={"encrypted_blob": req.encrypted_blob, "updated_at": req.updated_at},
            where=write_if,
        )
        .returning(UserSettings.user_id)
    )
    if written is not None:
        await db.commit()
        return SettingsPutResponse(
            updated_at=req.updated_at,
            winner="client",
            encrypted_blob=req.encrypted_blob,
        )

    # A skipped `DO UPDATE` still locks the row, so until the commit this reads the
    # exact row the condition was checked against. Columns, not the entity: a session
    # that already holds a `UserSettings` would hand back its stale copy.
    blob, stored_at = (
        await db.execute(
            select(UserSettings.encrypted_blob, UserSettings.updated_at).where(UserSettings.user_id == uid)
        )
    ).one()
    await db.commit()
    return SettingsPutResponse(updated_at=stored_at, winner="server", encrypted_blob=blob)
