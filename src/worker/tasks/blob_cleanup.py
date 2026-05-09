import asyncio
import logging
from datetime import UTC, datetime, timedelta

from src.worker.app import app

logger = logging.getLogger(__name__)

_ORPHAN_TTL_HOURS = 1  # blobs unconfirmed longer than this are considered orphans


@app.task(name="src.worker.tasks.blob_cleanup.cleanup_orphan_blobs")
def cleanup_orphan_blobs() -> dict:
    """Delete unconfirmed blobs older than 1 hour from both S3 and the DB."""
    return asyncio.run(_cleanup())


async def _cleanup() -> dict:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.blobs.models import Blob
    from src.blobs.s3 import delete_object
    from src.config import settings

    cutoff = int((datetime.now(UTC) - timedelta(hours=_ORPHAN_TTL_HOURS)).timestamp() * 1000)
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    deleted_count = 0
    async with Session() as db:
        orphans = await db.scalars(select(Blob).where(Blob.confirmed.is_(False), Blob.created_at < cutoff))
        for blob in orphans.all():
            try:
                delete_object(blob.key)
            except Exception:
                logger.exception("Failed to delete S3 object %s", blob.key)
                continue
            await db.delete(blob)
            deleted_count += 1
        await db.commit()

    await engine.dispose()
    logger.info("Orphan blob cleanup: deleted %d blobs", deleted_count)
    return {"deleted": deleted_count}
