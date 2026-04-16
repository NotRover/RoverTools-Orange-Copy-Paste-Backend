from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.redis_client import get_redis_pool

router = APIRouter(prefix="/internal", tags=["admin"])


@router.get("/healthz")
async def healthz(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_pool),
):
    db_ok = False
    redis_ok = False

    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        pass

    healthy = db_ok and redis_ok
    return {
        "status": "ok" if healthy else "degraded",
        "db": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }
