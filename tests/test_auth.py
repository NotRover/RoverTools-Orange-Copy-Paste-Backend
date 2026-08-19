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


async def test_recovery_envelope_round_trips_and_starts_absent(client: AsyncClient):
    """The client keys the forced save-your-code panel on this being null."""
    token = make_token(str(uuid.uuid4()))
    headers = {"Authorization": f"Bearer {token}"}
    first = await client.post("/api/v1/auth/bootstrap", json={}, headers=headers)
    assert first.json()["recovery_wrapped_umk"] is None

    stored = await client.put(
        "/api/v1/auth/umk/recovery",
        json={"recovery_wrapped_umk": "recovery-envelope-blob"},
        headers=headers,
    )
    assert stored.status_code == 204

    again = await client.post("/api/v1/auth/bootstrap", json={}, headers=headers)
    assert again.json()["recovery_wrapped_umk"] == "recovery-envelope-blob"


async def test_regenerating_replaces_the_previous_recovery_envelope(client: AsyncClient):
    """One code is live at a time - a regenerated code must revoke the old one."""
    token = make_token(str(uuid.uuid4()))
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/auth/bootstrap", json={}, headers=headers)
    for blob in ("first", "second"):
        resp = await client.put(
            "/api/v1/auth/umk/recovery",
            json={"recovery_wrapped_umk": blob},
            headers=headers,
        )
        assert resp.status_code == 204
    boot = await client.post("/api/v1/auth/bootstrap", json={}, headers=headers)
    assert boot.json()["recovery_wrapped_umk"] == "second"


async def test_recovery_envelope_requires_a_token(client: AsyncClient):
    resp = await client.put(
        "/api/v1/auth/umk/recovery", json={"recovery_wrapped_umk": "blob"}
    )
    assert resp.status_code in (401, 403)


# ── Devices ─────────────────────────────────────────────────────────────────────


async def test_register_and_list_devices(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.get("/api/v1/auth/devices", headers={"Authorization": auth_headers["Authorization"]})
    assert resp.status_code == 200
    devices = resp.json()
    assert isinstance(devices, list)
    assert any(str(d["id"]) == headers["X-Device-Id"] for d in devices)


async def test_registering_the_same_pubkey_twice_reuses_one_row(client: AsyncClient):
    """Re-login must not mint a second row for a device that already exists.

    The device keypair is the identity: the private half is in that machine's
    keychain and is what decrypts its wrapped UMK. Registering used to insert
    unconditionally, so one laptop accumulated a row per sign-in.
    """
    token = make_token(str(uuid.uuid4()))
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/auth/bootstrap", json={}, headers=headers)

    body = {"device_name": "Desk", "platform": "windows", "device_pubkey": "pubkey-aaa"}
    first = await client.post("/api/v1/auth/devices", json=body, headers=headers)
    second = await client.post(
        "/api/v1/auth/devices",
        json={**body, "device_name": "Desk renamed", "app_version": "9.9.9"},
        headers=headers,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["device_id"] == second.json()["device_id"]

    listed = await client.get("/api/v1/auth/devices", headers=headers)
    rows = [d for d in listed.json() if d["device_name"].startswith("Desk")]
    assert len(rows) == 1
    # The second call is an update, not a no-op.
    assert rows[0]["device_name"] == "Desk renamed"
    assert rows[0]["app_version"] == "9.9.9"


async def test_same_fingerprint_different_pubkey_is_a_separate_device(client: AsyncClient):
    """Machines imaged from one base share a machine id, so they share a
    fingerprint. They must still get their own rows - matching on the
    fingerprint would let a clone claim the original's wrapped UMK."""
    token = make_token(str(uuid.uuid4()))
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/auth/bootstrap", json={}, headers=headers)

    shared = "f" * 64
    one = await client.post(
        "/api/v1/auth/devices",
        json={"device_name": "Clone A", "device_pubkey": "pubkey-a", "fingerprint": shared},
        headers=headers,
    )
    two = await client.post(
        "/api/v1/auth/devices",
        json={"device_name": "Clone B", "device_pubkey": "pubkey-b", "fingerprint": shared},
        headers=headers,
    )
    assert one.json()["device_id"] != two.json()["device_id"]

    listed = await client.get("/api/v1/auth/devices", headers=headers)
    clones = [d for d in listed.json() if d["device_name"].startswith("Clone")]
    assert len(clones) == 2
    assert {d["fingerprint"] for d in clones} == {shared}


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
