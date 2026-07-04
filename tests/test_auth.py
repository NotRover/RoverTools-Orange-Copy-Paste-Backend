"""Auth-adjacent endpoint tests: bootstrap, device registration, JWT gating."""

import uuid

from httpx import AsyncClient

from tests.conftest import make_token


# ── Bootstrap ──────────────────────────────────────────────────────────────────


async def test_bootstrap_creates_profile_and_returns_salt(client: AsyncClient):
    token = make_token(str(uuid.uuid4()))
    resp = await client.post(
        "/api/v1/auth/bootstrap",
        json={"display_name": "Alice"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kdf_salt"]
    assert body["display_name"] == "Alice"


async def test_bootstrap_is_idempotent_and_salt_stable(client: AsyncClient):
    token = make_token(str(uuid.uuid4()))
    headers = {"Authorization": f"Bearer {token}"}
    first = await client.post("/api/v1/auth/bootstrap", json={}, headers=headers)
    second = await client.post("/api/v1/auth/bootstrap", json={}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["kdf_salt"] == second.json()["kdf_salt"]


# ── Devices ─────────────────────────────────────────────────────────────────────


async def test_register_and_list_devices(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.get("/api/v1/auth/devices", headers={"Authorization": auth_headers["Authorization"]})
    assert resp.status_code == 200
    devices = resp.json()
    assert isinstance(devices, list)
    assert any(str(d["id"]) == headers["X-Device-Id"] for d in devices)


# ── JWT gating ───────────────────────────────────────────────────────────────


async def test_protected_endpoint_no_token(client: AsyncClient):
    resp = await client.get("/api/v1/auth/devices")
    assert resp.status_code == 403  # HTTPBearer rejects missing credentials


async def test_protected_endpoint_bad_token(client: AsyncClient):
    resp = await client.get("/api/v1/auth/devices", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


async def test_missing_device_header_is_rejected(client: AsyncClient, auth_headers: dict):
    # sync endpoints require X-Device-Id
    resp = await client.get("/api/v1/sync/status", headers={"Authorization": auth_headers["Authorization"]})
    assert resp.status_code == 400


# ── Health check ──────────────────────────────────────────────────────────────


async def test_healthz(client: AsyncClient):
    resp = await client.get("/internal/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok"
    assert body["redis"] == "ok"
