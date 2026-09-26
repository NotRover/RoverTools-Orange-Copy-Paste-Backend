"""Comments on entries shared into a space: membership gate, thread, counts, deletion."""

import uuid

import pytest
from httpx import AsyncClient

from tests.test_spaces_invites import clean, create_space, join, make_user

ENTRY_1 = str(uuid.uuid5(uuid.NAMESPACE_URL, "entry-1"))
SAME = str(uuid.uuid5(uuid.NAMESPACE_URL, "same"))
A = str(uuid.uuid5(uuid.NAMESPACE_URL, "a"))
B = str(uuid.uuid5(uuid.NAMESPACE_URL, "b"))


async def post_comment(
    client: AsyncClient,
    headers: dict,
    space_id: str,
    client_id: str = ENTRY_1,
    body: str = "ciphertext",
    entry_type: str = "clipboard",
) -> dict:
    resp = await client.post(
        f"/api/v1/spaces/{space_id}/comments",
        json={
            "client_id": client_id,
            "entry_type": entry_type,
            "encrypted_body": body,
            "wrapped_key": "wrapped",
        },
        headers=clean(headers),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_any_member_can_comment_and_read_the_thread(client: AsyncClient):
    owner = await make_user(client)
    member = await make_user(client)
    space = await create_space(client, owner)
    await join(client, member, space, owner)

    await post_comment(client, owner, space["space_id"], body="from-owner")
    await post_comment(client, member, space["space_id"], body="from-member")

    resp = await client.get(
        f"/api/v1/spaces/{space['space_id']}/comments",
        params={"client_id": ENTRY_1, "entry_type": "clipboard"},
        headers=clean(member),
    )
    assert resp.status_code == 200, resp.text
    bodies = [c["encrypted_body"] for c in resp.json()]
    assert bodies == ["from-owner", "from-member"]


@pytest.mark.asyncio
async def test_non_member_cannot_read_or_write(client: AsyncClient):
    owner = await make_user(client)
    outsider = await make_user(client)
    space = await create_space(client, owner)
    await post_comment(client, owner, space["space_id"])

    write = await client.post(
        f"/api/v1/spaces/{space['space_id']}/comments",
        json={"client_id": ENTRY_1, "encrypted_body": "x", "wrapped_key": "k"},
        headers=clean(outsider),
    )
    assert write.status_code == 403

    read = await client.get(
        f"/api/v1/spaces/{space['space_id']}/comments",
        params={"client_id": ENTRY_1},
        headers=clean(outsider),
    )
    assert read.status_code == 403


@pytest.mark.asyncio
async def test_threads_are_scoped_to_one_entry_and_type(client: AsyncClient):
    """A note and a clipboard entry can share a client_id; their threads must not."""
    owner = await make_user(client)
    space = await create_space(client, owner)
    await post_comment(client, owner, space["space_id"], client_id=SAME, body="clip", entry_type="clipboard")
    await post_comment(client, owner, space["space_id"], client_id=SAME, body="note", entry_type="note")

    resp = await client.get(
        f"/api/v1/spaces/{space['space_id']}/comments",
        params={"client_id": SAME, "entry_type": "note"},
        headers=clean(owner),
    )
    assert [c["encrypted_body"] for c in resp.json()] == ["note"]


@pytest.mark.asyncio
async def test_counts_tally_every_commented_entry(client: AsyncClient):
    owner = await make_user(client)
    space = await create_space(client, owner)
    await post_comment(client, owner, space["space_id"], client_id=A)
    await post_comment(client, owner, space["space_id"], client_id=A)
    await post_comment(client, owner, space["space_id"], client_id=B)

    resp = await client.get(f"/api/v1/spaces/{space['space_id']}/comments/counts", headers=clean(owner))
    assert resp.status_code == 200, resp.text
    counts = {c["client_id"]: c for c in resp.json()}
    assert counts[A]["count"] == 2
    assert counts[B]["count"] == 1
    assert counts[A]["latest_at"] > 0


@pytest.mark.asyncio
async def test_author_deletes_own_comment(client: AsyncClient):
    owner = await make_user(client)
    member = await make_user(client)
    space = await create_space(client, owner)
    await join(client, member, space, owner)
    comment = await post_comment(client, member, space["space_id"])

    resp = await client.delete(
        f"/api/v1/spaces/{space['space_id']}/comments/{comment['id']}", headers=clean(member)
    )
    assert resp.status_code == 204, resp.text

    thread = await client.get(
        f"/api/v1/spaces/{space['space_id']}/comments",
        params={"client_id": ENTRY_1},
        headers=clean(member),
    )
    assert thread.json() == []


@pytest.mark.asyncio
async def test_owner_moderates_and_members_cannot(client: AsyncClient):
    owner = await make_user(client)
    author = await make_user(client)
    bystander = await make_user(client)
    space = await create_space(client, owner)
    await join(client, author, space, owner)
    await join(client, bystander, space, owner)
    comment = await post_comment(client, author, space["space_id"])

    refused = await client.delete(
        f"/api/v1/spaces/{space['space_id']}/comments/{comment['id']}", headers=clean(bystander)
    )
    assert refused.status_code == 403

    allowed = await client.delete(
        f"/api/v1/spaces/{space['space_id']}/comments/{comment['id']}", headers=clean(owner)
    )
    assert allowed.status_code == 204, allowed.text


@pytest.mark.asyncio
async def test_a_comment_on_a_client_id_that_is_not_a_uuid_is_refused(client: AsyncClient):
    owner = await make_user(client)
    space = await create_space(client, owner)
    write = await client.post(
        f"/api/v1/spaces/{space['space_id']}/comments",
        json={"client_id": "entry-1", "encrypted_body": "x", "wrapped_key": "k"},
        headers=clean(owner),
    )
    assert write.status_code == 422
    read = await client.get(
        f"/api/v1/spaces/{space['space_id']}/comments",
        params={"client_id": "entry-1"},
        headers=clean(owner),
    )
    assert read.status_code == 422
