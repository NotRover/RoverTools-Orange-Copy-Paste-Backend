"""Device scoping, the UMK proof, the admin key, avatar hygiene and the socket handshake.

The WebSocket tests run through Starlette's TestClient, which drives the app on
its own event loop. They replace the database and Redis touch points with fakes
rather than share the async test session across loops.
"""

import base64
import hashlib
import json
import time
import uuid

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import HTTPException, status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src import realtime
from src.auth import service
from src.auth.models import Profile
from src.config import settings
from src.dependencies import device_cache_key
from src.main import app
from tests.conftest import make_token

PROOF = base64.b64encode(b"p" * 32).decode()
OTHER_PROOF = base64.b64encode(b"q" * 32).decode()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _user(client: AsyncClient, **claims) -> tuple[str, str, str]:
    """A bootstrapped user with one device: (user_id, token, device_id)."""
    user_id = str(uuid.uuid4())
    token = make_token(user_id, **claims)
    assert (await client.post("/api/v1/auth/bootstrap", json={}, headers=_auth(token))).status_code == 200
    dev = await client.post("/api/v1/auth/devices", json={"platform": "windows"}, headers=_auth(token))
    assert dev.status_code == 201
    return user_id, token, dev.json()["device_id"]


async def _proof_hash(db: AsyncSession, user_id: str) -> str | None:
    profile = await db.scalar(select(Profile).where(Profile.id == uuid.UUID(user_id)))
    assert profile is not None
    await db.refresh(profile)
    return profile.umk_proof_hash


# ── Device scoping ───────────────────────────────────────────────────────────


async def test_a_non_uuid_device_id_is_400(client: AsyncClient):
    _, token, _ = await _user(client)
    resp = await client.get("/api/v1/auth/umk/device", headers={**_auth(token), "X-Device-Id": "not-a-uuid"})
    assert resp.status_code == 400


async def test_an_unknown_device_is_403(client: AsyncClient):
    _, token, _ = await _user(client)
    resp = await client.get(
        "/api/v1/auth/umk/device", headers={**_auth(token), "X-Device-Id": str(uuid.uuid4())}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "device_unknown"


async def test_another_users_device_is_403(client: AsyncClient):
    _, token, _ = await _user(client)
    _, _, their_device = await _user(client)
    resp = await client.get("/api/v1/auth/umk/device", headers={**_auth(token), "X-Device-Id": their_device})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "device_unknown"


async def test_a_revoked_device_is_401_and_its_cache_entry_is_dropped(
    client: AsyncClient, fake_redis: FakeRedis, monkeypatch
):
    published: list[tuple[str, str, dict]] = []

    async def capture(redis, channel, event, payload, origin_device=None):
        published.append((channel, event, payload))

    monkeypatch.setattr(realtime, "publish", capture)

    user_id, token, device_id = await _user(client)
    headers = {**_auth(token), "X-Device-Id": device_id}
    # A live device passes (404 is "no wrap stored", past the device check) and is cached.
    assert (await client.get("/api/v1/auth/umk/device", headers=headers)).status_code == 404
    assert await fake_redis.get(device_cache_key(user_id, device_id))

    assert (await client.delete(f"/api/v1/auth/devices/{device_id}", headers=_auth(token))).status_code == 204
    assert not await fake_redis.get(device_cache_key(user_id, device_id))
    assert (f"user:{user_id}", "device:revoked", {"device_id": device_id}) in published

    resp = await client.get("/api/v1/auth/umk/device", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "device_revoked"


async def test_a_wrap_for_a_revoked_device_is_refused(client: AsyncClient):
    _, token, device_id = await _user(client)
    assert (await client.delete(f"/api/v1/auth/devices/{device_id}", headers=_auth(token))).status_code == 204
    resp = await client.post(
        f"/api/v1/auth/devices/{device_id}/key-wrap", json={"wrapped_umk": "w"}, headers=_auth(token)
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "device_revoked"


# ── UMK proof ────────────────────────────────────────────────────────────────


async def test_first_proof_is_trusted_then_required(client: AsyncClient, db: AsyncSession):
    user_id, token, _ = await _user(client)

    first = await client.put(
        "/api/v1/auth/umk", json={"wrapped_umk": "env-1"}, headers={**_auth(token), "X-Umk-Proof": PROOF}
    )
    assert first.status_code == 204
    assert await _proof_hash(db, user_id) == hashlib.sha256(b"p" * 32).hexdigest()

    missing = await client.put("/api/v1/auth/umk", json={"wrapped_umk": "env-2"}, headers=_auth(token))
    assert missing.status_code == 403
    assert missing.json()["detail"] == "umk_proof_required"

    wrong = await client.put(
        "/api/v1/auth/umk", json={"wrapped_umk": "env-2"}, headers={**_auth(token), "X-Umk-Proof": OTHER_PROOF}
    )
    assert wrong.status_code == 403

    right = await client.put(
        "/api/v1/auth/umk", json={"wrapped_umk": "env-2"}, headers={**_auth(token), "X-Umk-Proof": PROOF}
    )
    assert right.status_code == 204


async def test_proof_guards_recovery_wrap_and_revoke_once_set(client: AsyncClient):
    _, token, device_id = await _user(client)
    await client.put(
        "/api/v1/auth/umk", json={"wrapped_umk": "env"}, headers={**_auth(token), "X-Umk-Proof": PROOF}
    )
    no_proof = _auth(token)
    with_proof = {**_auth(token), "X-Umk-Proof": PROOF}

    calls = [
        ("put", "/api/v1/auth/umk/recovery", {"recovery_wrapped_umk": "r"}),
        ("delete", "/api/v1/auth/umk/recovery", None),
        ("post", f"/api/v1/auth/devices/{device_id}/key-wrap", {"wrapped_umk": "w"}),
        ("delete", f"/api/v1/auth/devices/{device_id}", None),
    ]
    for method, url, body in calls:
        kwargs = {"json": body} if body is not None else {}
        refused = await getattr(client, method)(url, headers=no_proof, **kwargs)
        assert refused.status_code == 403, (method, url)
        allowed = await getattr(client, method)(url, headers=with_proof, **kwargs)
        assert allowed.status_code == 204, (method, url, allowed.text)


async def test_legacy_account_without_proof_proceeds_and_records_one(client: AsyncClient, db: AsyncSession):
    user_id, token, _ = await _user(client)
    bare = await client.put(
        "/api/v1/auth/umk/recovery", json={"recovery_wrapped_umk": "r"}, headers=_auth(token)
    )
    assert bare.status_code == 204
    assert await _proof_hash(db, user_id) is None

    carried = await client.put(
        "/api/v1/auth/umk/recovery",
        json={"recovery_wrapped_umk": "r2"},
        headers={**_auth(token), "X-Umk-Proof": PROOF},
    )
    assert carried.status_code == 204
    assert await _proof_hash(db, user_id) == hashlib.sha256(b"p" * 32).hexdigest()


async def test_a_malformed_proof_is_400(client: AsyncClient):
    _, token, _ = await _user(client)
    for bad in ("not base64!", base64.b64encode(b"short").decode()):
        resp = await client.put(
            "/api/v1/auth/umk", json={"wrapped_umk": "e"}, headers={**_auth(token), "X-Umk-Proof": bad}
        )
        assert resp.status_code == 400


async def test_reset_from_a_recovery_session_replaces_the_proof(client: AsyncClient, db: AsyncSession):
    user_id, token, _ = await _user(client)
    await client.put(
        "/api/v1/auth/umk", json={"wrapped_umk": "old-env"}, headers={**_auth(token), "X-Umk-Proof": PROOF}
    )
    recovery = make_token(user_id, amr=[{"method": "recovery", "timestamp": int(time.time())}])
    reset = {"wrapped_umk": "new-env", "reset": True}

    # No header at all is refused even from a recovery session.
    assert (await client.put("/api/v1/auth/umk", json=reset, headers=_auth(recovery))).status_code == 403

    ok = await client.put("/api/v1/auth/umk", json=reset, headers={**_auth(recovery), "X-Umk-Proof": OTHER_PROOF})
    assert ok.status_code == 204

    profile = await db.scalar(select(Profile).where(Profile.id == uuid.UUID(user_id)))
    assert profile is not None
    await db.refresh(profile)
    assert profile.pw_wrapped_umk == "new-env"
    assert profile.pw_wrapped_umk_prev == "old-env"
    assert profile.umk_proof_hash == hashlib.sha256(b"q" * 32).hexdigest()

    # The old proof is dead, the new one works.
    stale = await client.put(
        "/api/v1/auth/umk", json={"wrapped_umk": "x"}, headers={**_auth(token), "X-Umk-Proof": PROOF}
    )
    assert stale.status_code == 403


async def test_reset_without_a_recovery_session_needs_the_old_proof(client: AsyncClient):
    user_id, token, _ = await _user(client)
    await client.put(
        "/api/v1/auth/umk", json={"wrapped_umk": "old"}, headers={**_auth(token), "X-Umk-Proof": PROOF}
    )
    password_session = make_token(user_id, amr=[{"method": "password", "timestamp": int(time.time())}])
    resp = await client.put(
        "/api/v1/auth/umk",
        json={"wrapped_umk": "new", "reset": True},
        headers={**_auth(password_session), "X-Umk-Proof": OTHER_PROOF},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "umk_proof_required"


# ── Admin key ────────────────────────────────────────────────────────────────


async def test_admin_key_is_checked_and_failures_lock_out(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "correct-admin-key")
    url = "/internal/v1/admin/email"
    assert (await client.get(url, headers={"X-Admin-Key": "correct-admin-key"})).status_code == 200
    assert (await client.get(url)).status_code == 403
    for _ in range(9):
        assert (await client.get(url, headers={"X-Admin-Key": "wrong"})).status_code == 403
    # Ten failures from this address: even the right key is refused for a while.
    assert (await client.get(url, headers={"X-Admin-Key": "correct-admin-key"})).status_code == 429


async def test_admin_routes_are_rate_limited(client: AsyncClient, monkeypatch, rate_limits_on):
    monkeypatch.setattr(settings, "admin_api_key", "correct-admin-key")
    headers = {"X-Admin-Key": "correct-admin-key"}
    codes = [(await client.get("/internal/v1/admin/email", headers=headers)).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60
    assert codes[60] == 429


async def test_join_page_is_rate_limited(client: AsyncClient, rate_limits_on):
    codes = [(await client.get("/join/ABCD2345")).status_code for _ in range(61)]
    assert codes[-1] == 429


# ── Bootstrap input hygiene ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://lh3.googleusercontent.com/a/abc=s96-c", True),
        ("https://lh5.googleusercontent.com/a/abc", True),
        ("http://lh3.googleusercontent.com/a/abc", False),
        ("https://evil.example/a.png", False),
        ("https://googleusercontent.com.evil.example/a.png", False),
        ("https://user:pw@lh3.googleusercontent.com/a", False),
        ("javascript:alert(1)", False),
        (42, False),
    ],
)
def test_avatar_url_allowlist(url, kept):
    assert (service.clean_avatar_url(url) == url) is kept


async def test_bootstrap_drops_a_foreign_avatar_and_caps_the_name(client: AsyncClient):
    token = make_token(
        str(uuid.uuid4()),
        user_metadata={"avatar_url": "https://tracker.example/p.gif", "full_name": "N" * 500},
    )
    body = (await client.post("/api/v1/auth/bootstrap", json={}, headers=_auth(token))).json()
    assert body["avatar_url"] is None
    assert len(body["display_name"]) == 128


# ── WebSocket handshake ──────────────────────────────────────────────────────


@pytest.fixture
def ws_app(monkeypatch):
    """The socket endpoint with its database and Redis touch points faked."""
    known: dict[str, str] = {}  # device_id -> user_id

    async def check_device(db, redis, user_id, device_id):
        if known.get(str(device_id)) != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="device_unknown")

    async def resolve_channels(user_id):
        return [f"user:{user_id}", realtime.BROADCAST_CHANNEL]

    async def pool():
        return FakeRedis(decode_responses=True)

    class _NoSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(realtime, "check_device", check_device)
    monkeypatch.setattr(realtime, "_resolve_channels", resolve_channels)
    monkeypatch.setattr(realtime, "get_redis_pool", pool)
    monkeypatch.setattr(realtime, "AsyncSessionLocal", _NoSession)
    return TestClient(app), known


def _register(known: dict, user_id: str | None = None) -> tuple[str, str]:
    user_id = user_id or str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    known[device_id] = user_id
    return user_id, device_id


def test_ws_first_message_auth_ok(ws_app):
    tc, known = ws_app
    user_id, device_id = _register(known)
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": make_token(user_id), "device_id": device_id})
        assert ws.receive_json() == {"type": "auth_ok"}
        # Non-object messages are ignored, not fatal.
        ws.send_text("[1, 2, 3]")
        ws.send_text("not json")
        ws.send_json({"event": "pong"})


@pytest.mark.parametrize(
    "message",
    [
        {"type": "auth", "token": "not-a-jwt"},
        {"type": "hello"},
        [1, 2],
    ],
)
def test_ws_bad_handshake_closes_4401(ws_app, message):
    tc, known = ws_app
    _, device_id = _register(known)
    with tc.websocket_connect("/ws") as ws:
        if isinstance(message, dict) and "token" in message:
            message = {**message, "device_id": device_id}
        ws.send_json(message)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == realtime.AUTH_FAILED


def test_ws_someone_elses_device_closes_4401(ws_app):
    tc, known = ws_app
    _, device_id = _register(known)
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": make_token(str(uuid.uuid4())), "device_id": device_id})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == realtime.AUTH_FAILED


def test_ws_silence_times_out_with_4401(ws_app, monkeypatch):
    monkeypatch.setattr(realtime, "AUTH_TIMEOUT", 0.2)
    tc, _ = ws_app
    with tc.websocket_connect("/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == realtime.AUTH_FAILED


def test_ws_query_token_is_not_accepted(ws_app, monkeypatch):
    monkeypatch.setattr(realtime, "AUTH_TIMEOUT", 0.2)
    tc, known = ws_app
    user_id, device_id = _register(known)
    with tc.websocket_connect(f"/ws?token={make_token(user_id)}&device_id={device_id}") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == realtime.AUTH_FAILED


def test_ws_closes_when_the_token_expires(ws_app):
    tc, known = ws_app
    user_id, device_id = _register(known)
    token = make_token(user_id, exp=int(time.time()) + 1)
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token, "device_id": device_id})
        assert ws.receive_json() == {"type": "auth_ok"}
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
    assert closed.value.code == realtime.AUTH_FAILED


async def test_socket_slots_are_capped_per_user(monkeypatch):
    monkeypatch.setattr(realtime, "MAX_SOCKETS_PER_USER", 2)
    user_id = str(uuid.uuid4())
    sockets = [object(), object(), object()]
    assert await realtime._reserve_slot(user_id, sockets[0])  # type: ignore[arg-type]
    assert await realtime._reserve_slot(user_id, sockets[1])  # type: ignore[arg-type]
    assert not await realtime._reserve_slot(user_id, sockets[2])  # type: ignore[arg-type]
    await realtime._release_slot(user_id, sockets[0])  # type: ignore[arg-type]
    assert await realtime._reserve_slot(user_id, sockets[2])  # type: ignore[arg-type]
    for ws in sockets:
        await realtime._release_slot(user_id, ws)  # type: ignore[arg-type]
    assert user_id not in realtime._user_sockets


async def test_revocation_closes_only_that_devices_sockets():
    class _Sock:
        def __init__(self):
            self.closed_with: int | None = None

        async def close(self, code: int = 1000):
            self.closed_with = code

    user_id = str(uuid.uuid4())
    revoked, kept = _Sock(), _Sock()
    await realtime._register(revoked, "dev-revoked", [f"user:{user_id}"])  # type: ignore[arg-type]
    await realtime._register(kept, "dev-kept", [f"user:{user_id}"])  # type: ignore[arg-type]
    try:
        await realtime._close_device_sockets(user_id, "dev-revoked")
        assert revoked.closed_with == realtime.AUTH_FAILED
        assert kept.closed_with is None
    finally:
        await realtime._unregister(revoked)  # type: ignore[arg-type]
        await realtime._unregister(kept)  # type: ignore[arg-type]


# ── Per-socket fan-out trimming ───────────────────────────────────────────────


def test_sync_entry_fanout_is_trimmed_to_each_sockets_spaces():
    """The socket-side twin of pull's `entry_view`: the author's sockets get the
    row whole, everyone else sees only the spaces they are in and those wraps."""
    from src import realtime as rt

    author, reader = str(uuid.uuid4()), str(uuid.uuid4())
    shared, private = str(uuid.uuid4()), str(uuid.uuid4())
    event = {
        "event": "sync:entry",
        "payload": {
            "user_id": author,
            "client_id": str(uuid.uuid4()),
            "space_ids": [shared, private],
            "wrapped_keys": json.dumps({"personal": "p", shared: "s", private: "q"}),
        },
    }
    author_ws, reader_ws = object(), object()
    rt._ws_channels[author_ws] = {f"user:{author}", f"space:{shared}", f"space:{private}"}  # type: ignore[index]
    rt._ws_channels[reader_ws] = {f"user:{reader}", f"space:{shared}"}  # type: ignore[index]
    try:
        whole = json.loads(rt._trim_entry_for(author_ws, event))  # type: ignore[arg-type]
        assert whole["payload"]["space_ids"] == [shared, private]
        assert "personal" in json.loads(whole["payload"]["wrapped_keys"])

        seen = json.loads(rt._trim_entry_for(reader_ws, event))  # type: ignore[arg-type]
        assert seen["payload"]["space_ids"] == [shared]
        assert json.loads(seen["payload"]["wrapped_keys"]) == {shared: "s"}
    finally:
        rt._ws_channels.pop(author_ws, None)  # type: ignore[arg-type]
        rt._ws_channels.pop(reader_ws, None)  # type: ignore[arg-type]
