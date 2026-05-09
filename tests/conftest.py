"""
Test fixtures.

Requires a running PostgreSQL reachable via TEST_DATABASE_URL
(default: postgresql+asyncpg://postgres:postgres@localhost:5432/clipboard_test).

Redis is replaced by fakeredis — no real Redis needed.
Celery task dispatch (.delay) is monkeypatched to a MagicMock in all tests.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.database import Base, get_db
from src.dependencies import get_redis
from src.main import app
from src.redis_client import get_redis_pool

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/clipboard_test",
)


# ── Session-level event loop ───────────────────────────────────────────────────
@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── RSA keypair (generated once per session) ───────────────────────────────────
@pytest.fixture(scope="session")
def _rsa_pem() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pub


@pytest.fixture(scope="session", autouse=True)
def patch_settings(_rsa_pem: tuple[str, str]):
    """Write ephemeral RSA keys to temp files and point settings at them."""
    from src.auth.jwt import get_jwks
    from src.config import settings

    priv_pem, pub_pem = _rsa_pem
    _tmpdir = TemporaryDirectory()
    tmp = Path(_tmpdir.name)
    (tmp / "private.pem").write_text(priv_pem)
    (tmp / "public.pem").write_text(pub_pem)
    settings.jwt_private_key_path = tmp / "private.pem"
    settings.jwt_public_key_path = tmp / "public.pem"
    # Invalidate any cached JWKS from a previous settings state
    get_jwks.cache_clear()
    yield
    _tmpdir.cleanup()


# ── Test database engine (session-scoped: create tables once, drop on teardown) ─
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
    """Per-test DB session.  Uses join_transaction_mode='create_savepoint' so that
    service-level session.commit() calls operate on savepoints, not real commits.
    The outer connection is rolled back at the end of each test."""
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


# ── Fake Redis (per-test, decode_responses matches production behaviour) ─────
@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    r = FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# ── FastAPI test client with dependency overrides ─────────────────────────────
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


# ── Suppress Celery task dispatch in every test ───────────────────────────────
@pytest.fixture(autouse=True)
def _no_celery(monkeypatch):
    for attr in [
        ("src.worker.tasks.email", "send_verification_email"),
        ("src.worker.tasks.email", "send_password_reset_email"),
        ("src.worker.tasks.email", "send_sharing_invite_email"),
    ]:
        import importlib

        mod = importlib.import_module(attr[0])
        task = getattr(mod, attr[1])
        monkeypatch.setattr(task, "delay", MagicMock())


# ── Convenience: register + verify + login, return auth headers ───────────────
@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient, fake_redis: FakeRedis) -> dict:
    email = f"test-{uuid.uuid4().hex[:8]}@example.com"
    password = "TestPass123!secure"

    # Register
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": "Test User"},
    )
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["user_id"]

    # Grab the verify token from fake Redis and confirm it
    async for raw_key in fake_redis.scan_iter("email_verify:*"):
        stored = await fake_redis.get(raw_key)
        if stored == user_id:
            token = raw_key.split("email_verify:")[1]
            resp = await client.post("/api/v1/auth/verify-email", json={"token": token})
            assert resp.status_code == 200, resp.text
            break

    # Login
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "platform": "windows"},
    )
    assert login.status_code == 200, login.text

    return {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "_refresh_token": login.json()["refresh_token"],
        "_device_id": login.json()["device_id"],
        "_email": email,
        "_password": password,
    }
