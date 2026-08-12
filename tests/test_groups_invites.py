"""Groups, short invite codes, addressed invites, and shared-blob access."""

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import make_token


async def make_user(client: AsyncClient, email: str | None = None) -> dict:
    """Bootstrap a fresh Supabase-style user + device; returns auth headers."""
    user_id = str(uuid.uuid4())
    token = make_token(user_id, email)
    base = {"Authorization": f"Bearer {token}"}

    boot = await client.post("/api/v1/auth/bootstrap", json={"display_name": email or "User"}, headers=base)
    assert boot.status_code == 200, boot.text

    dev = await client.post(
        "/api/v1/auth/devices", json={"device_name": "Test", "platform": "windows"}, headers=base
    )
    assert dev.status_code == 201, dev.text

    return {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": dev.json()["device_id"],
        "_user_id": user_id,
    }


def clean(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if not k.startswith("_")}


async def create_group(client: AsyncClient, headers: dict, name: str = "Test group") -> dict:
    resp = await client.post(
        "/api/v1/groups", json={"name": name, "group_type": "pool"}, headers=clean(headers)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Short invite codes ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_code_is_short_format(client: AsyncClient):
    owner = await make_user(client)
    created = await create_group(client, owner)
    code = created["invite_code"]
    assert len(code) == 8
    assert code.isalnum()
    assert code == code.upper()


@pytest.mark.asyncio
async def test_join_normalizes_code_case_and_dashes(client: AsyncClient):
    owner = await make_user(client)
    created = await create_group(client, owner)
    code = created["invite_code"]
    sloppy = f"{code[:4]}-{code[4:]}".lower()

    joiner = await make_user(client)
    resp = await client.post("/api/v1/groups/join", json={"invite_code": sloppy}, headers=clean(joiner))
    assert resp.status_code == 200, resp.text
    assert resp.json()["group_id"] == created["group_id"]


# ── Addressed invites ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_accept_flow(client: AsyncClient):
    owner = await make_user(client, "owner@example.com")
    invitee = await make_user(client, "invitee@example.com")
    created = await create_group(client, owner)

    sent = await client.post(
        f"/api/v1/groups/{created['group_id']}/invites",
        json={"email": "Invitee@Example.com"},
        headers=clean(owner),
    )
    assert sent.status_code == 201, sent.text
    invite = sent.json()
    assert invite["status"] == "pending"
    assert invite["invitee_email"] == "invitee@example.com"

    box = await client.get("/api/v1/invites", headers=clean(invitee))
    assert box.status_code == 200
    received = box.json()["received"]
    assert len(received) == 1
    assert received[0]["id"] == invite["id"]
    assert received[0]["group_name"] == "Test group"

    accepted = await client.post(f"/api/v1/invites/{invite['id']}/accept", headers=clean(invitee))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["group_id"] == created["group_id"]

    group = await client.get(f"/api/v1/groups/{created['group_id']}", headers=clean(invitee))
    assert group.status_code == 200
    member_ids = {m["user_id"] for m in group.json()["members"]}
    assert invitee["_user_id"] in member_ids

    outbox = await client.get("/api/v1/invites", headers=clean(owner))
    assert outbox.json()["sent"][0]["status"] == "accepted"
    assert box.status_code == 200


@pytest.mark.asyncio
async def test_invite_decline_and_revoke(client: AsyncClient):
    owner = await make_user(client, "owner2@example.com")
    invitee = await make_user(client, "invitee2@example.com")
    created = await create_group(client, owner)
    gid = created["group_id"]

    first = await client.post(
        f"/api/v1/groups/{gid}/invites", json={"email": "invitee2@example.com"}, headers=clean(owner)
    )
    invite_id = first.json()["id"]

    declined = await client.post(f"/api/v1/invites/{invite_id}/decline", headers=clean(invitee))
    assert declined.status_code == 204

    again = await client.post(f"/api/v1/invites/{invite_id}/accept", headers=clean(invitee))
    assert again.status_code == 409

    second = await client.post(
        f"/api/v1/groups/{gid}/invites", json={"email": "invitee2@example.com"}, headers=clean(owner)
    )
    second_id = second.json()["id"]
    assert second_id != invite_id

    revoked = await client.delete(f"/api/v1/invites/{second_id}", headers=clean(owner))
    assert revoked.status_code == 204

    box = await client.get("/api/v1/invites", headers=clean(invitee))
    assert box.json()["received"] == []


@pytest.mark.asyncio
async def test_invite_to_existing_member_conflicts(client: AsyncClient):
    owner = await make_user(client, "owner3@example.com")
    member = await make_user(client, "member3@example.com")
    created = await create_group(client, owner)

    await client.post(
        "/api/v1/groups/join", json={"invite_code": created["invite_code"]}, headers=clean(member)
    )
    resp = await client.post(
        f"/api/v1/groups/{created['group_id']}/invites",
        json={"email": "member3@example.com"},
        headers=clean(owner),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_invite_only_owner_can_send(client: AsyncClient):
    owner = await make_user(client, "owner4@example.com")
    member = await make_user(client, "member4@example.com")
    created = await create_group(client, owner)
    await client.post(
        "/api/v1/groups/join", json={"invite_code": created["invite_code"]}, headers=clean(member)
    )

    resp = await client.post(
        f"/api/v1/groups/{created['group_id']}/invites",
        json={"email": "someone@example.com"},
        headers=clean(member),
    )
    assert resp.status_code == 403


# ── Live Share sessions ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sharing_invite_is_tracked_and_session_recoverable(client: AsyncClient):
    owner = await make_user(client, "share-owner@example.com")
    invitee = await make_user(client, "share-invitee@example.com")

    resp = await client.post(
        "/api/v1/sharing/invite",
        json={"email": "share-invitee@example.com", "share_scope": "clipboard"},
        headers=clean(owner),
    )
    assert resp.status_code == 201, resp.text
    share_group_id = resp.json()["share_group_id"]

    box = await client.get("/api/v1/invites", headers=clean(invitee))
    received = box.json()["received"]
    assert len(received) == 1
    assert received[0]["group_type"] == "live_share"

    accepted = await client.post(f"/api/v1/invites/{received[0]['id']}/accept", headers=clean(invitee))
    assert accepted.status_code == 200

    sessions = await client.get("/api/v1/sharing/sessions", headers=clean(invitee))
    assert sessions.status_code == 200
    body = sessions.json()
    assert len(body) == 1
    assert body[0]["share_group_id"] == share_group_id
    assert body[0]["owner_id"] == owner["_user_id"]
    assert body[0]["my_wrapped_group_key"] is None

    keys = await client.post(
        f"/api/v1/groups/{share_group_id}/keys",
        json={"wrapped_keys": [{"user_id": invitee["_user_id"], "wrapped_group_key": "wrapped-blob"}]},
        headers=clean(owner),
    )
    assert keys.status_code == 204

    sessions = await client.get("/api/v1/sharing/sessions", headers=clean(invitee))
    assert sessions.json()[0]["my_wrapped_group_key"] == "wrapped-blob"


# ── Shared blob access ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_member_can_download_shared_blob(client: AsyncClient):
    owner = await make_user(client, "blob-owner@example.com")
    member = await make_user(client, "blob-member@example.com")
    outsider = await make_user(client, "blob-outsider@example.com")

    created = await create_group(client, owner)
    gid = created["group_id"]
    await client.post(
        "/api/v1/groups/join", json={"invite_code": created["invite_code"]}, headers=clean(member)
    )

    up = await client.post(
        "/api/v1/blobs/request-upload",
        json={"mime_type": "image/png", "size_bytes": 1024, "checksum": "ab" * 32},
        headers=clean(owner),
    )
    assert up.status_code == 200, up.text
    blob_key = up.json()["blob_key"]
    confirm = await client.post(
        "/api/v1/blobs/confirm-upload", json={"blob_key": blob_key}, headers=clean(owner)
    )
    assert confirm.status_code == 204

    push = await client.post(
        "/api/v1/sync/push",
        json={
            "entries": [
                {
                    "client_id": "img-1",
                    "entry_type": "clipboard",
                    "kind": "image",
                    "encrypted_content": "ciphertext",
                    "created_at": 1,
                    "updated_at": 1,
                    "blob_key": blob_key,
                    "blob_size": 1024,
                    "group_ids": [gid],
                }
            ]
        },
        headers=clean(owner),
    )
    assert push.status_code == 200, push.text

    mine = await client.get(f"/api/v1/blobs/{blob_key}/download-url", headers=clean(owner))
    assert mine.status_code == 200

    shared = await client.get(f"/api/v1/blobs/{blob_key}/download-url", headers=clean(member))
    assert shared.status_code == 200, shared.text

    denied = await client.get(f"/api/v1/blobs/{blob_key}/download-url", headers=clean(outsider))
    assert denied.status_code == 404
