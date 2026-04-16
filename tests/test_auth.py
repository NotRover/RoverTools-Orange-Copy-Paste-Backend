"""
Auth endpoint smoke tests.
Requires a running PostgreSQL + Redis (use docker-compose for CI).
"""
import pytest


@pytest.mark.asyncio
async def test_healthz(client):
    resp = await client.get("/internal/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
