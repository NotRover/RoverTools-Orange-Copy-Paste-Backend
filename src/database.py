from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import settings

# Supavisor's transaction pooler (port 6543) may hand each transaction a
# different backend connection, which invalidates asyncpg's prepared-statement
# cache — reused statements then fail with "prepared statement does not exist".
# Disabling both caches is the supported workaround. Session mode (port 5432 on
# the pooler host) behaves like a normal connection and needs no special care.
_TRANSACTION_POOLER_PORT = ":6543"


def _connect_args(url: str) -> dict[str, int]:
    if _TRANSACTION_POOLER_PORT in url:
        return {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    return {}


engine = create_async_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=settings.app_env == "development",
    connect_args=_connect_args(settings.database_url),
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
