import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.settings.models import UserSettings
from src.settings.schemas import SettingsPutRequest, SettingsPutResponse


async def get_settings(db: AsyncSession, user_id: str) -> UserSettings | None:
    uid = uuid.UUID(user_id)
    return await db.scalar(select(UserSettings).where(UserSettings.user_id == uid))


async def put_settings(
    db: AsyncSession, user_id: str, req: SettingsPutRequest
) -> SettingsPutResponse:
    uid = uuid.UUID(user_id)
    existing = await db.scalar(select(UserSettings).where(UserSettings.user_id == uid))

    if existing and existing.updated_at > req.updated_at:
        # Server blob is newer — return it so client applies it
        return SettingsPutResponse(
            updated_at=existing.updated_at,
            winner="server",
            encrypted_blob=existing.encrypted_blob,
        )

    stmt = pg_insert(UserSettings).values(
        user_id=uid,
        encrypted_blob=req.encrypted_blob,
        updated_at=req.updated_at,
    ).on_conflict_do_update(
        index_elements=["user_id"],
        set_={"encrypted_blob": req.encrypted_blob, "updated_at": req.updated_at},
    )
    await db.execute(stmt)
    await db.commit()
    return SettingsPutResponse(
        updated_at=req.updated_at,
        winner="client",
        encrypted_blob=req.encrypted_blob,
    )
