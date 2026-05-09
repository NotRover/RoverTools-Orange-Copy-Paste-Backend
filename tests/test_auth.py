"""Auth endpoint tests: register, verify, login, refresh, password reset, JWKS."""

import pytest
from httpx import AsyncClient


# ── Registration ──────────────────────────────────────────────────────────────

async def test_register_success(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "new@example.com", "password": "Secret1234!", "display_name": "Alice"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "user_id" in body
    assert "message" in body


async def test_register_duplicate_email(client: AsyncClient):
    payload = {"email": "dup@example.com", "password": "Secret1234!"}
    await client.post("/api/v1/auth/register", json=payload)
    resp = await client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 409


async def test_register_short_password(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "short@example.com", "password": "abc"},
    )
    assert resp.status_code == 422


# ── Email verification ────────────────────────────────────────────────────────

async def test_verify_email_flow(client: AsyncClient, fake_redis):
    # Register
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": "verify@example.com", "password": "Verify1234!"},
    )
    assert reg.status_code == 201
    user_id = reg.json()["user_id"]

    # Harvest the token from fake Redis
    token = None
    async for key in fake_redis.scan_iter("email_verify:*"):
        stored = await fake_redis.get(key)
        if stored == user_id:
            token = key.split("email_verify:")[1]
            break
    assert token is not None, "verification token not found in Redis"

    resp = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert resp.status_code == 200
    assert "verified" in resp.json()["message"].lower()


async def test_verify_email_invalid_token(client: AsyncClient):
    resp = await client.post("/api/v1/auth/verify-email", json={"token": "bogus"})
    assert resp.status_code == 400


async def test_resend_verification_always_202(client: AsyncClient):
    # Works for both registered and unregistered emails
    resp = await client.post(
        "/api/v1/auth/resend-verification",
        json={"email": "nobody@example.com"},
    )
    assert resp.status_code == 202


# ── Login ─────────────────────────────────────────────────────────────────────

async def test_login_success(client: AsyncClient, auth_headers: dict):
    assert "Authorization" in auth_headers


async def test_login_wrong_password(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": auth_headers["_email"], "password": "WrongPass999!"},
    )
    assert resp.status_code == 401


async def test_login_returns_kdf_salt(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": auth_headers["_email"],
            "password": auth_headers["_password"],
            "platform": "windows",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "kdf_salt" in body
    assert "access_token" in body
    assert "refresh_token" in body
    assert "device_id" in body


# ── Token refresh ─────────────────────────────────────────────────────────────

async def test_refresh_token_rotation(client: AsyncClient, auth_headers: dict):
    resp = await client.post(
        "/api/v1/auth/refresh",
        json={
            "refresh_token": auth_headers["_refresh_token"],
            "device_id": auth_headers["_device_id"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    # Old refresh token must no longer work
    resp2 = await client.post(
        "/api/v1/auth/refresh",
        json={
            "refresh_token": auth_headers["_refresh_token"],
            "device_id": auth_headers["_device_id"],
        },
    )
    assert resp2.status_code == 401


# ── Authenticated endpoints ───────────────────────────────────────────────────

async def test_list_devices(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.get("/api/v1/auth/devices", headers=headers)
    assert resp.status_code == 200
    devices = resp.json()
    assert isinstance(devices, list)
    assert len(devices) >= 1
    assert devices[0]["is_current"] is True


async def test_protected_endpoint_no_token(client: AsyncClient):
    resp = await client.get("/api/v1/auth/devices")
    assert resp.status_code == 403


# ── Password reset ────────────────────────────────────────────────────────────

async def test_password_reset_flow(client: AsyncClient, fake_redis, auth_headers: dict):
    email = auth_headers["_email"]

    # Request reset — always returns 202
    req = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": email}
    )
    assert req.status_code == 202

    # Harvest reset token from fake Redis
    token = None
    async for key in fake_redis.scan_iter("pwd_reset:*"):
        token = key.split("pwd_reset:")[1]
        break
    assert token is not None

    new_password = "NewPass5678!secure"
    confirm = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": new_password},
    )
    assert confirm.status_code == 200

    # Old password no longer works
    bad = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": auth_headers["_password"]},
    )
    assert bad.status_code == 401

    # New password works
    ok = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": new_password, "platform": "windows"},
    )
    assert ok.status_code == 200


async def test_password_reset_unknown_email_still_202(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "ghost@example.com"},
    )
    assert resp.status_code == 202


async def test_password_reset_invalid_token(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": "invalid-token", "new_password": "NewPass5678!"},
    )
    assert resp.status_code == 400


# ── JWKS ──────────────────────────────────────────────────────────────────────

async def test_jwks_endpoint(client: AsyncClient):
    resp = await client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body
    assert len(body["keys"]) == 1
    key = body["keys"][0]
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["use"] == "sig"
    assert "n" in key and "e" in key and "kid" in key
    assert resp.headers.get("cache-control", "").startswith("public")


# ── Health check ──────────────────────────────────────────────────────────────

async def test_healthz(client: AsyncClient):
    resp = await client.get("/internal/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok"
    assert body["redis"] == "ok"
