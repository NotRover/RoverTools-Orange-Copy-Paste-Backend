"""Spaces, short invite codes, addressed invites, rekey-on-remove, shared blobs."""

import json
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


async def create_space(client: AsyncClient, headers: dict, name: str = "Test space", **kwargs) -> dict:
    resp = await client.post("/api/v1/spaces", json={"name": name, **kwargs}, headers=clean(headers))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def join(
    client: AsyncClient, joiner: dict, space: dict, approver: dict, wrapped: str | None = None
) -> None:
    """Get `joiner` into `space`: ask by code, then have `approver` let them in.

    A code raises a request rather than granting membership, so every test that
    just needs a second member goes through both halves. `wrapped` is the Space
    Key the approval hands over; None is the honest default for tests that never
    look at keys.
    """
    asked = await client.post(
        "/api/v1/spaces/join", json={"invite_code": space["invite_code"]}, headers=clean(joiner)
    )
    assert asked.status_code == 200, asked.text
    assert asked.json()["status"] == "pending", asked.text

    pending = await client.get(
        f"/api/v1/spaces/{space['space_id']}/join-requests", headers=clean(approver)
    )
    assert pending.status_code == 200, pending.text
    row = next(r for r in pending.json() if r["user_id"] == joiner["_user_id"])

    ok = await client.post(
        f"/api/v1/spaces/{space['space_id']}/join-requests/{row['id']}/approve",
        json={"wrapped_space_keys": wrapped},
        headers=clean(approver),
    )
    assert ok.status_code == 204, ok.text


async def distribute(client: AsyncClient, owner: dict, space_id: str, keyrings: dict[str, str]) -> None:
    resp = await client.post(
        f"/api/v1/spaces/{space_id}/keys",
        json={
            "wrapped_keyrings": [
                {"user_id": uid, "wrapped_space_keys": blob} for uid, blob in keyrings.items()
            ]
        },
        headers=clean(owner),
    )
    assert resp.status_code == 204, resp.text


# ── Short invite codes ─────────────────────────────────────────────────


async def _next_event(pubsub, timeout: float = 1.0) -> dict:
    """The next published event on a subscription, decoded."""
    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
    assert msg is not None, "nothing was published"
    return json.loads(msg["data"])


@pytest.mark.asyncio
async def test_creating_a_space_announces_it_to_the_creators_own_channel(client: AsyncClient, fake_redis):
    """The owner has to be told about their own space, or they never hear it.

    A socket resolves its channel set once, at connect, and re-resolves only
    when the client is told membership changed. Every other membership event is
    published to the space channel *and* to the affected user's own channel,
    for exactly the reason that bites here: you cannot reach someone on a
    channel they are not on yet. Creation was the one path that published
    nothing at all, so an owner who created a space without restarting the app
    sat off their own space channel indefinitely - the entries other members
    shared, their comments, their presence and the "someone joined" event were
    all published to a channel with the owner missing from it.

    This asserts the announcement. That the client answers it with
    `resubscribe`, and that the server then re-resolves, is socket behaviour and
    is only exercised in the running app.
    """
    owner = await make_user(client)
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe(f"user:{owner['_user_id']}")
    try:
        created = await create_space(client, owner)
        event = await _next_event(pubsub)
    finally:
        await pubsub.aclose()

    assert event["event"] == "space:membership_changed"
    assert event["payload"] == {
        "space_id": created["space_id"],
        "action": "joined",
        "user_id": owner["_user_id"],
    }


@pytest.mark.asyncio
async def test_invite_code_is_short_format(client: AsyncClient):
    owner = await make_user(client)
    created = await create_space(client, owner)
    code = created["invite_code"]
    assert len(code) == 8
    assert code.isalnum()
    assert code == code.upper()


@pytest.mark.asyncio
async def test_join_normalizes_code_case_and_dashes(client: AsyncClient):
    owner = await make_user(client)
    created = await create_space(client, owner)
    code = created["invite_code"]
    sloppy = f"{code[:4]}-{code[4:]}".lower()

    joiner = await make_user(client)
    resp = await client.post("/api/v1/spaces/join", json={"invite_code": sloppy}, headers=clean(joiner))
    assert resp.status_code == 200, resp.text
    # A code no longer returns a space, because it no longer grants one - only
    # the name of the space now being knocked on.
    assert resp.json() == {"status": "pending", "space_name": "Test space"}


# ── Addressed invites ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_accept_flow(client: AsyncClient):
    owner = await make_user(client, "owner@example.com")
    invitee = await make_user(client, "invitee@example.com")
    created = await create_space(client, owner)

    sent = await client.post(
        f"/api/v1/spaces/{created['space_id']}/invites",
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
    assert received[0]["space_name"] == "Test space"

    accepted = await client.post(f"/api/v1/invites/{invite['id']}/accept", headers=clean(invitee))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["space_id"] == created["space_id"]

    space = await client.get(f"/api/v1/spaces/{created['space_id']}", headers=clean(invitee))
    assert space.status_code == 200
    member_ids = {m["user_id"] for m in space.json()["members"]}
    assert invitee["_user_id"] in member_ids

    outbox = await client.get("/api/v1/invites", headers=clean(owner))
    assert outbox.json()["sent"][0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_invite_decline_and_revoke(client: AsyncClient):
    owner = await make_user(client, "owner2@example.com")
    invitee = await make_user(client, "invitee2@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    first = await client.post(
        f"/api/v1/spaces/{sid}/invites", json={"email": "invitee2@example.com"}, headers=clean(owner)
    )
    invite_id = first.json()["id"]

    declined = await client.post(f"/api/v1/invites/{invite_id}/decline", headers=clean(invitee))
    assert declined.status_code == 204

    again = await client.post(f"/api/v1/invites/{invite_id}/accept", headers=clean(invitee))
    assert again.status_code == 409

    second = await client.post(
        f"/api/v1/spaces/{sid}/invites", json={"email": "invitee2@example.com"}, headers=clean(owner)
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
    created = await create_space(client, owner)

    await join(client, member, created, owner)
    resp = await client.post(
        f"/api/v1/spaces/{created['space_id']}/invites",
        json={"email": "member3@example.com"},
        headers=clean(owner),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_invite_to_an_address_with_no_account_is_rejected(client: AsyncClient):
    owner = await make_user(client, "owner-noacct@example.com")
    created = await create_space(client, owner)

    resp = await client.post(
        f"/api/v1/spaces/{created['space_id']}/invites",
        json={"email": "nobody-here@example.com"},
        headers=clean(owner),
    )
    assert resp.status_code == 404

    listed = await client.get("/api/v1/invites", headers=clean(owner))
    assert listed.json()["sent"] == []


@pytest.mark.asyncio
async def test_invite_only_owner_can_send(client: AsyncClient):
    owner = await make_user(client, "owner4@example.com")
    member = await make_user(client, "member4@example.com")
    created = await create_space(client, owner)
    await join(client, member, created, owner)

    resp = await client.post(
        f"/api/v1/spaces/{created['space_id']}/invites",
        json={"email": "someone@example.com"},
        headers=clean(member),
    )
    assert resp.status_code == 403


# ── Keyring distribution and recovery ──────────────────────────────────


@pytest.mark.asyncio
async def test_keyring_distribution_and_restart_recovery(client: AsyncClient):
    owner = await make_user(client, "key-owner@example.com")
    member = await make_user(client, "key-member@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    await join(client, member, created, owner)

    # Fresh member has no keyring yet; the owner's client sees that and wraps.
    listing = await client.get("/api/v1/spaces", headers=clean(member))
    assert listing.status_code == 200
    space = listing.json()[0]
    assert space["my_wrapped_space_keys"] is None
    me = next(m for m in space["members"] if m["user_id"] == member["_user_id"])
    assert me["has_space_key"] is False

    await distribute(client, owner, sid, {member["_user_id"]: '["wrapped-k1"]'})

    # The wrapped keyring comes back on listing — the restart recovery path.
    listing = await client.get("/api/v1/spaces", headers=clean(member))
    space = listing.json()[0]
    assert space["my_wrapped_space_keys"] == '["wrapped-k1"]'
    me = next(m for m in space["members"] if m["user_id"] == member["_user_id"])
    assert me["has_space_key"] is True


@pytest.mark.asyncio
async def test_only_owner_can_distribute_keys(client: AsyncClient):
    owner = await make_user(client, "dist-owner@example.com")
    member = await make_user(client, "dist-member@example.com")
    created = await create_space(client, owner)
    await join(client, member, created, owner)

    resp = await client.post(
        f"/api/v1/spaces/{created['space_id']}/keys",
        json={
            "wrapped_keyrings": [
                {"user_id": member["_user_id"], "wrapped_space_keys": '["attacker-keyring"]'}
            ]
        },
        headers=clean(member),
    )
    assert resp.status_code == 403


# ── Rekey on removal ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_removing_a_member_clears_remaining_keyrings(client: AsyncClient):
    """A departure invalidates the Space Key: everyone left must show
    `has_space_key=false` so the owner's reconcile mints and redistributes a
    fresh key the removed member never sees."""
    owner = await make_user(client, "rm-owner@example.com")
    stays = await make_user(client, "rm-stays@example.com")
    leaves = await make_user(client, "rm-leaves@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    for u in (stays, leaves):
        await join(client, u, created, owner)

    await distribute(
        client,
        owner,
        sid,
        {
            owner["_user_id"]: '["k1-owner"]',
            stays["_user_id"]: '["k1-stays"]',
            leaves["_user_id"]: '["k1-leaves"]',
        },
    )

    resp = await client.delete(
        f"/api/v1/spaces/{sid}/members/{leaves['_user_id']}", headers=clean(owner)
    )
    assert resp.status_code == 204

    space = (await client.get(f"/api/v1/spaces/{sid}", headers=clean(owner))).json()
    member_ids = {m["user_id"] for m in space["members"]}
    assert leaves["_user_id"] not in member_ids
    # Every remaining member's keyring was cleared — the rekey trigger.
    assert all(m["has_space_key"] is False for m in space["members"])
    assert space["my_wrapped_space_keys"] is None


@pytest.mark.asyncio
async def test_member_can_leave_but_owner_cannot(client: AsyncClient):
    owner = await make_user(client, "lv-owner@example.com")
    member = await make_user(client, "lv-member@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]
    await join(client, member, created, owner)

    left = await client.delete(f"/api/v1/spaces/{sid}/members/{member['_user_id']}", headers=clean(member))
    assert left.status_code == 204

    denied = await client.delete(f"/api/v1/spaces/{sid}/members/{owner['_user_id']}", headers=clean(owner))
    assert denied.status_code == 400


# ── History policy ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_history_off_hides_earlier_entries_from_new_members(client: AsyncClient):
    """The pull space-arm must respect the per-membership history floor."""
    owner = await make_user(client, "hist-owner@example.com")
    created = await create_space(client, owner, share_history=False)
    sid = created["space_id"]

    early = await client.post(
        "/api/v1/sync/push",
        json={
            "entries": [
                {
                    "client_id": "before-join",
                    "entry_type": "clipboard",
                    "kind": "text",
                    "encrypted_content": "ciphertext",
                    "created_at": 1,
                    "updated_at": 1,
                    "space_ids": [sid],
                    "wrapped_keys": '{"personal": "w", "%s": "w"}' % sid,
                }
            ]
        },
        headers=clean(owner),
    )
    assert early.status_code == 200, early.text

    member = await make_user(client, "hist-member@example.com")
    await join(client, member, created, owner)

    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(member))
    assert pulled.status_code == 200
    assert all(e["client_id"] != "before-join" for e in pulled.json()["entries"])

    late = await client.post(
        "/api/v1/sync/push",
        json={
            "entries": [
                {
                    "client_id": "after-join",
                    "entry_type": "clipboard",
                    "kind": "text",
                    "encrypted_content": "ciphertext",
                    "created_at": 2,
                    "updated_at": 2,
                    "space_ids": [sid],
                    "wrapped_keys": '{"personal": "w", "%s": "w"}' % sid,
                }
            ]
        },
        headers=clean(owner),
    )
    assert late.status_code == 200

    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(member))
    ids = {e["client_id"] for e in pulled.json()["entries"]}
    assert "after-join" in ids
    assert "before-join" not in ids


@pytest.mark.asyncio
async def test_share_history_on_serves_full_history_to_new_members(client: AsyncClient):
    owner = await make_user(client, "hist2-owner@example.com")
    created = await create_space(client, owner, share_history=True)
    sid = created["space_id"]

    await client.post(
        "/api/v1/sync/push",
        json={
            "entries": [
                {
                    "client_id": "old-entry",
                    "entry_type": "clipboard",
                    "kind": "text",
                    "encrypted_content": "ciphertext",
                    "created_at": 1,
                    "updated_at": 1,
                    "space_ids": [sid],
                    "wrapped_keys": '{"personal": "w", "%s": "w"}' % sid,
                }
            ]
        },
        headers=clean(owner),
    )

    member = await make_user(client, "hist2-member@example.com")
    await join(client, member, created, owner)

    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(member))
    ids = {e["client_id"] for e in pulled.json()["entries"]}
    assert "old-entry" in ids


@pytest.mark.asyncio
async def test_opening_share_history_reaches_members_who_already_joined(client: AsyncClient):
    """Flipping the policy on must drop the floor for existing members, not just
    for whoever joins next - they are the ones who were stuck."""
    owner = await make_user(client, "hist3-owner@example.com")
    created = await create_space(client, owner, share_history=False)
    sid = created["space_id"]

    await client.post(
        "/api/v1/sync/push",
        json={
            "entries": [
                {
                    "client_id": "walled-off",
                    "entry_type": "clipboard",
                    "kind": "text",
                    "encrypted_content": "ciphertext",
                    "created_at": 1,
                    "updated_at": 1,
                    "space_ids": [sid],
                    "wrapped_keys": '{"personal": "w", "%s": "w"}' % sid,
                }
            ]
        },
        headers=clean(owner),
    )

    member = await make_user(client, "hist3-member@example.com")
    await join(client, member, created, owner)

    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(member))
    assert all(e["client_id"] != "walled-off" for e in pulled.json()["entries"])

    patched = await client.patch(
        f"/api/v1/spaces/{sid}", json={"share_history": True}, headers=clean(owner)
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["share_history"] is True

    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(member))
    assert "walled-off" in {e["client_id"] for e in pulled.json()["entries"]}


@pytest.mark.asyncio
async def test_only_owner_can_change_share_history(client: AsyncClient):
    owner = await make_user(client, "hist4-owner@example.com")
    created = await create_space(client, owner, share_history=False)
    sid = created["space_id"]

    member = await make_user(client, "hist4-member@example.com")
    await join(client, member, created, owner)

    resp = await client.patch(
        f"/api/v1/spaces/{sid}", json={"share_history": True}, headers=clean(member)
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_closing_share_history_keeps_existing_members_access(client: AsyncClient):
    """Turning it off is a policy for the next joiner. It must not retroactively
    wall off someone who could already read the history."""
    owner = await make_user(client, "hist5-owner@example.com")
    created = await create_space(client, owner, share_history=True)
    sid = created["space_id"]

    await client.post(
        "/api/v1/sync/push",
        json={
            "entries": [
                {
                    "client_id": "still-visible",
                    "entry_type": "clipboard",
                    "kind": "text",
                    "encrypted_content": "ciphertext",
                    "created_at": 1,
                    "updated_at": 1,
                    "space_ids": [sid],
                    "wrapped_keys": '{"personal": "w", "%s": "w"}' % sid,
                }
            ]
        },
        headers=clean(owner),
    )

    member = await make_user(client, "hist5-member@example.com")
    await join(client, member, created, owner)

    resp = await client.patch(
        f"/api/v1/spaces/{sid}", json={"share_history": False}, headers=clean(owner)
    )
    assert resp.status_code == 200, resp.text

    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(member))
    assert "still-visible" in {e["client_id"] for e in pulled.json()["entries"]}


# ── Multi-space fan-out ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entry_in_two_spaces_reaches_both_memberships(client: AsyncClient):
    """One entry, two space_ids, one wrapped_keys map — members of either
    space pull it; an outsider does not."""
    owner = await make_user(client, "fan-owner@example.com")
    a = await create_space(client, owner, name="Space A")
    b = await create_space(client, owner, name="Space B")

    member_a = await make_user(client, "fan-a@example.com")
    member_b = await make_user(client, "fan-b@example.com")
    outsider = await make_user(client, "fan-out@example.com")
    await join(client, member_a, a, owner)
    await join(client, member_b, b, owner)

    push = await client.post(
        "/api/v1/sync/push",
        json={
            "entries": [
                {
                    "client_id": "fan-1",
                    "entry_type": "clipboard",
                    "kind": "text",
                    "encrypted_content": "ciphertext",
                    "created_at": 1,
                    "updated_at": 1,
                    "space_ids": [a["space_id"], b["space_id"]],
                    "wrapped_keys": '{"personal": "w", "%s": "wa", "%s": "wb"}'
                    % (a["space_id"], b["space_id"]),
                }
            ]
        },
        headers=clean(owner),
    )
    assert push.status_code == 200, push.text

    for u in (member_a, member_b):
        pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(u))
        ids = {e["client_id"] for e in pulled.json()["entries"]}
        assert "fan-1" in ids, f"member of a shared space did not receive the entry: {u['_user_id']}"

    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(outsider))
    assert all(e["client_id"] != "fan-1" for e in pulled.json()["entries"])

    # The envelope rides along verbatim for members.
    entry = next(
        e
        for e in (await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(member_a))).json()["entries"]
        if e["client_id"] == "fan-1"
    )
    assert a["space_id"] in entry["wrapped_keys"]
    assert set(entry["space_ids"]) == {a["space_id"], b["space_id"]}


# ── Shared blob access ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_space_member_can_download_shared_blob(client: AsyncClient):
    owner = await make_user(client, "blob-owner@example.com")
    member = await make_user(client, "blob-member@example.com")
    outsider = await make_user(client, "blob-outsider@example.com")

    created = await create_space(client, owner)
    sid = created["space_id"]
    await join(client, member, created, owner)

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
                    "space_ids": [sid],
                    "wrapped_keys": '{"personal": "w", "%s": "w"}' % sid,
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


# ── Removing an entry from a space ─────────────────────────────────────


async def _push_into_space(client: AsyncClient, headers: dict, sid: str, client_id: str) -> None:
    resp = await client.post(
        "/api/v1/sync/push",
        json={
            "entries": [
                {
                    "client_id": client_id,
                    "entry_type": "clipboard",
                    "kind": "text",
                    "encrypted_content": "ciphertext",
                    "created_at": 1,
                    "updated_at": 1,
                    "space_ids": [sid],
                    "wrapped_keys": '{"personal": "w", "%s": "w"}' % sid,
                }
            ]
        },
        headers=clean(headers),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_member_can_remove_their_own_entry_from_a_space(client: AsyncClient):
    """Unsharing. The member who posted it takes it back out; their personal
    copy survives, the space stops carrying it."""
    owner = await make_user(client, "rm1-owner@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    member = await make_user(client, "rm1-member@example.com")
    await join(client, member, created, owner)
    await _push_into_space(client, member, sid, "mine")

    resp = await client.delete(
        f"/api/v1/spaces/{sid}/entries/mine?entry_type=clipboard", headers=clean(member)
    )
    assert resp.status_code == 204, resp.text

    # Gone from the space for everyone else...
    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(owner))
    assert all(e["client_id"] != "mine" for e in pulled.json()["entries"])

    # ...but still the author's own row.
    own = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(member))
    mine = [e for e in own.json()["entries"] if e["client_id"] == "mine"]
    assert len(mine) == 1
    assert mine[0]["space_ids"] == []


@pytest.mark.asyncio
async def test_member_cannot_remove_someone_elses_entry(client: AsyncClient):
    """Only the space owner moderates. A member has no say over what another
    member shared."""
    owner = await make_user(client, "rm2-owner@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    author = await make_user(client, "rm2-author@example.com")
    other = await make_user(client, "rm2-other@example.com")
    for who in (author, other):
        await join(client, who, created, owner)
    await _push_into_space(client, author, sid, "theirs")

    resp = await client.delete(
        f"/api/v1/spaces/{sid}/entries/theirs?entry_type=clipboard", headers=clean(other)
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_owner_can_remove_a_members_entry(client: AsyncClient):
    """Moderation still works: the owner takes down anything in their space."""
    owner = await make_user(client, "rm3-owner@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    member = await make_user(client, "rm3-member@example.com")
    await join(client, member, created, owner)
    await _push_into_space(client, member, sid, "posted")

    resp = await client.delete(
        f"/api/v1/spaces/{sid}/entries/posted?entry_type=clipboard", headers=clean(owner)
    )
    assert resp.status_code == 204, resp.text


# ── Authorship: one entry, one author ──────────────────────────────────────────
#
# Rows are keyed `(user_id, client_id, entry_type)`, so a member pushing an entry
# they did not write cannot overwrite the author's row - it inserts a second one
# carrying the same `client_id`, and both fan out. Clients collapse the two onto
# one item, so the practical result was the author's text and name being replaced
# by whoever pushed last (client bug #8). The client refuses to make such a push
# and refuses to merge one; this is the half that holds when the client does not.


async def _push_raw(client: AsyncClient, headers: dict, sid: str | None, client_id: str) -> dict:
    entry = {
        "client_id": client_id,
        "entry_type": "clipboard",
        "kind": "text",
        "encrypted_content": "ciphertext",
        "created_at": 1,
        "updated_at": 2,
        "space_ids": [sid] if sid else [],
    }
    resp = await client.post("/api/v1/sync/push", json={"entries": [entry]}, headers=clean(headers))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_a_member_cannot_plant_a_rival_copy_of_someone_elses_entry(client: AsyncClient):
    owner = await make_user(client, "auth1-owner@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    author = await make_user(client, "auth1-author@example.com")
    impostor = await make_user(client, "auth1-impostor@example.com")
    for who in (author, impostor):
        await join(client, who, created, owner)
    await _push_into_space(client, author, sid, "shared-note")

    body = await _push_raw(client, impostor, sid, "shared-note")
    assert body["accepted"] == []
    assert [c["reason"] for c in body["conflicts"]] == ["not_your_entry"]

    # And nothing reached the space: the owner still pulls exactly one row for
    # that client_id, the author's.
    pulled = await client.get("/api/v1/sync/pull?after_ts=0", headers=clean(owner))
    rows = [e for e in pulled.json()["entries"] if e["client_id"] == "shared-note"]
    assert len(rows) == 1
    assert rows[0]["user_id"] == author["_user_id"]


@pytest.mark.asyncio
async def test_the_author_can_still_update_their_own_entry(client: AsyncClient):
    """The guard must not cost the author their own edits."""
    owner = await make_user(client, "auth2-owner@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    author = await make_user(client, "auth2-author@example.com")
    await join(client, author, created, owner)
    await _push_into_space(client, author, sid, "mine-to-edit")

    body = await _push_raw(client, author, sid, "mine-to-edit")
    assert body["conflicts"] == []
    assert len(body["accepted"]) == 1


@pytest.mark.asyncio
async def test_the_same_client_id_outside_a_shared_space_is_left_alone(client: AsyncClient):
    """One person, two accounts, the same local history: their entries collide by
    construction and neither impersonates anyone. Only a collision *inside a
    shared space* is refused."""
    first = await make_user(client, "auth3-first@example.com")
    second = await make_user(client, "auth3-second@example.com")

    assert len((await _push_raw(client, first, None, "same-id"))["accepted"]) == 1
    body = await _push_raw(client, second, None, "same-id")
    assert body["conflicts"] == []
    assert len(body["accepted"]) == 1


@pytest.mark.asyncio
async def test_a_collision_in_a_space_the_pusher_is_not_sharing_into_is_left_alone(
    client: AsyncClient,
):
    """The refusal keys on the spaces the push actually targets, not on the
    client_id alone - a personal entry cannot impersonate anything."""
    owner = await make_user(client, "auth4-owner@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    author = await make_user(client, "auth4-author@example.com")
    await join(client, author, created, owner)
    await _push_into_space(client, author, sid, "collides")

    outsider = await make_user(client, "auth4-outsider@example.com")
    body = await _push_raw(client, outsider, None, "collides")
    assert body["conflicts"] == []
    assert len(body["accepted"]) == 1


# ── Join approval ──────────────────────────────────────────────────────


async def _ask(client: AsyncClient, who: dict, space: dict):
    return await client.post(
        "/api/v1/spaces/join", json={"invite_code": space["invite_code"]}, headers=clean(who)
    )


async def _spaces(client: AsyncClient, who: dict) -> list[dict]:
    resp = await client.get("/api/v1/spaces", headers=clean(who))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_a_code_raises_a_request_and_not_a_membership(client: AsyncClient):
    """The whole point: holding the code is no longer enough to be in the space."""
    owner = await make_user(client, "ja1-owner@example.com")
    stranger = await make_user(client, "ja1-stranger@example.com")
    created = await create_space(client, owner)

    asked = await _ask(client, stranger, created)
    assert asked.status_code == 200, asked.text
    assert asked.json()["status"] == "pending"

    # Not a member: the space is not in their list, and reading it is refused.
    assert await _spaces(client, stranger) == []
    denied = await client.get(f"/api/v1/spaces/{created['space_id']}", headers=clean(stranger))
    assert denied.status_code == 403, denied.text

    # The owner sees exactly one person waiting.
    mine = await _spaces(client, owner)
    assert len(mine[0]["members"]) == 1
    assert mine[0]["pending_join_requests"] == 1
    assert mine[0]["i_can_approve"] is True


@pytest.mark.asyncio
async def test_approval_hands_over_the_space_key_in_the_same_call(client: AsyncClient):
    """The reason this is not slower than what it replaces: the approver is
    holding the ring at that moment, so the membership starts with a key."""
    owner = await make_user(client, "ja2-owner@example.com")
    joiner = await make_user(client, "ja2-joiner@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    await _ask(client, joiner, created)
    rows = (await client.get(f"/api/v1/spaces/{sid}/join-requests", headers=clean(owner))).json()
    assert [r["user_id"] for r in rows] == [joiner["_user_id"]]

    ok = await client.post(
        f"/api/v1/spaces/{sid}/join-requests/{rows[0]['id']}/approve",
        json={"wrapped_space_keys": "[ring-for-joiner]"},
        headers=clean(owner),
    )
    assert ok.status_code == 204, ok.text

    theirs = (await _spaces(client, joiner))[0]
    assert theirs["my_wrapped_space_keys"] == "[ring-for-joiner]"
    assert theirs["my_wrapped_by"] == owner["_user_id"]
    # Nothing left waiting, and no second knock possible.
    assert (await _spaces(client, owner))[0]["pending_join_requests"] == 0


@pytest.mark.asyncio
async def test_members_cannot_approve_until_the_owner_says_so(client: AsyncClient):
    owner = await make_user(client, "ja3-owner@example.com")
    member = await make_user(client, "ja3-member@example.com")
    stranger = await make_user(client, "ja3-stranger@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]
    await join(client, member, created, owner)
    await _ask(client, stranger, created)

    # Owner-only by default, so a member sees neither the list nor the count.
    refused = await client.get(f"/api/v1/spaces/{sid}/join-requests", headers=clean(member))
    assert refused.status_code == 403, refused.text
    theirs = next(sp for sp in await _spaces(client, member) if sp["id"] == sid)
    assert theirs["i_can_approve"] is False
    assert theirs["pending_join_requests"] == 0

    opened = await client.patch(
        f"/api/v1/spaces/{sid}", json={"members_can_approve": True}, headers=clean(owner)
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["members_can_approve"] is True
    # The history policy was not restated and must not have moved.
    assert opened.json()["share_history"] is True

    rows = (await client.get(f"/api/v1/spaces/{sid}/join-requests", headers=clean(member))).json()
    ok = await client.post(
        f"/api/v1/spaces/{sid}/join-requests/{rows[0]['id']}/approve",
        json={"wrapped_space_keys": "[ring-from-a-member]"},
        headers=clean(member),
    )
    assert ok.status_code == 204, ok.text
    # Wrapped by whoever actually approved, which is what the recipient needs to
    # know to compute the right shared secret.
    assert (await _spaces(client, stranger))[0]["my_wrapped_by"] == member["_user_id"]


@pytest.mark.asyncio
async def test_only_the_owner_chooses_who_approves(client: AsyncClient):
    owner = await make_user(client, "ja4-owner@example.com")
    member = await make_user(client, "ja4-member@example.com")
    created = await create_space(client, owner)
    await join(client, member, created, owner)

    resp = await client.patch(
        f"/api/v1/spaces/{created['space_id']}",
        json={"members_can_approve": True},
        headers=clean(member),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_a_declined_request_keeps_the_code_spent(client: AsyncClient):
    """The declined row is deliberately kept: deleting it would let the same
    person knock again every time they re-paste a code they still hold."""
    owner = await make_user(client, "ja5-owner@example.com")
    stranger = await make_user(client, "ja5-stranger@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]

    await _ask(client, stranger, created)
    rows = (await client.get(f"/api/v1/spaces/{sid}/join-requests", headers=clean(owner))).json()
    no = await client.post(
        f"/api/v1/spaces/{sid}/join-requests/{rows[0]['id']}/decline", headers=clean(owner)
    )
    assert no.status_code == 204, no.text

    again = await _ask(client, stranger, created)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "declined"
    assert (await _spaces(client, owner))[0]["pending_join_requests"] == 0
    assert await _spaces(client, stranger) == []

    # And it cannot be revived by deciding it twice.
    spent = await client.post(
        f"/api/v1/spaces/{sid}/join-requests/{rows[0]['id']}/approve",
        json={"wrapped_space_keys": None},
        headers=clean(owner),
    )
    assert spent.status_code == 409, spent.text


@pytest.mark.asyncio
async def test_knocking_twice_does_not_stack_requests(client: AsyncClient):
    owner = await make_user(client, "ja6-owner@example.com")
    stranger = await make_user(client, "ja6-stranger@example.com")
    created = await create_space(client, owner)

    for _ in range(3):
        assert (await _ask(client, stranger, created)).status_code == 200

    rows = (
        await client.get(
            f"/api/v1/spaces/{created['space_id']}/join-requests", headers=clean(owner)
        )
    ).json()
    assert len(rows) == 1
    assert (await _spaces(client, owner))[0]["pending_join_requests"] == 1


@pytest.mark.asyncio
async def test_re_pasting_a_code_you_already_used_changes_nothing(client: AsyncClient):
    owner = await make_user(client, "ja7-owner@example.com")
    member = await make_user(client, "ja7-member@example.com")
    created = await create_space(client, owner)
    await join(client, member, created, owner)

    again = await _ask(client, member, created)
    assert again.status_code == 200, again.text
    assert (await _spaces(client, owner))[0]["pending_join_requests"] == 0
    assert len((await _spaces(client, owner))[0]["members"]) == 2


@pytest.mark.asyncio
async def test_a_stranger_cannot_read_or_decide_requests(client: AsyncClient):
    owner = await make_user(client, "ja8-owner@example.com")
    outsider = await make_user(client, "ja8-outsider@example.com")
    knocker = await make_user(client, "ja8-knocker@example.com")
    created = await create_space(client, owner)
    sid = created["space_id"]
    await _ask(client, knocker, created)
    rows = (await client.get(f"/api/v1/spaces/{sid}/join-requests", headers=clean(owner))).json()

    blind = await client.get(f"/api/v1/spaces/{sid}/join-requests", headers=clean(outsider))
    assert blind.status_code == 403, blind.text
    nope = await client.post(
        f"/api/v1/spaces/{sid}/join-requests/{rows[0]['id']}/approve",
        json={"wrapped_space_keys": "[not-theirs-to-give]"},
        headers=clean(outsider),
    )
    assert nope.status_code == 403, nope.text
