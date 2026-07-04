"""Test fixtures.

Requires a running PostgreSQL reachable via TEST_DATABASE_URL
(default: postgresql+asyncpg://postgres:postgres@localhost:5432/clipboard_test).

Auth is Supabase-issued in production; here we mint HS256 tokens with the same
secret the app verifies against. Redis is replaced by fakeredis.
"""

import asyncio
import os
import time
import uuid
from collections.abc import AsyncGenerator

import jwt
import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.config import settings
from src.database import Base, get_db
from src.dependencies import get_redis
from src.main import app
from src.redis_client import get_redis_pool

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/clipboard_test",
)
_TEST_JWT_SECRET = "test-supabase-jwt-secret"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session", autouse=True)
def _configure_settings():
    settings.supabase_jwt_secret = _TEST_JWT_SECRET
    settings.supabase_jwt_algorithm = "HS256"
    settings.supabase_jwt_audience = "authenticated"
    yield


def make_token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "aud": "authenticated", "iat": now, "exp": now + 3600},
        _TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db(test_engine) -> AsyncGenerator[AsyncSession, None]:
    async with test_engine.connect() as conn:
        await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    r = FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def client(db: AsyncSession, fake_redis: FakeRedis) -> AsyncGenerator[AsyncClient, None]:
    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = lambda: fake_redis
    app.dependency_overrides[get_redis_pool] = lambda: fake_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient) -> dict:
    """Simulate a signed-in Supabase user: bootstrap a profile + register a device."""
    user_id = str(uuid.uuid4())
    token = make_token(user_id)
    base = {"Authorization": f"Bearer {token}"}

    boot = await client.post("/api/v1/auth/bootstrap", json={"display_name": "Test User"}, headers=base)
    assert boot.status_code == 200, boot.text

    dev = await client.post(
        "/api/v1/auth/devices",
        json={"device_name": "Test", "platform": "windows"},
        headers=base,
    )
    assert dev.status_code == 201, dev.text
    device_id = dev.json()["device_id"]

    return {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": device_id,
        "_user_id": user_id,
        "_kdf_salt": boot.json()["kdf_salt"],
    }
