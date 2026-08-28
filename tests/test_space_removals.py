"""Withdrawing an entry from a space has to leave a record a pull can read.

The bug: removing an entry from a space strips the space id from
`sync_entries.space_ids`, and pull's space arm matches on exactly that array. So
from that instant the row is not "changed" and not "deleted" to a member - it is
absent, indistinguishable from an entry that never existed. The only signal was
the `space:entry_removed` event, and an event reaches whoever is connected when
it fires. A member whose device was closed came back, pulled, matched nothing,
and kept its copy of withdrawn content indefinitely - with a healthy Redis, a
successful publish, and nothing failing anywhere.

The `merge_cursors` tests need no database. The rest need Postgres, like every
other endpoint test here.
"""

import pytest
from httpx import AsyncClient

from src.sync.service import merge_cursors
from tests.test_spaces_invites import clean, create_space, join, make_user


# ── The watermark rule ───────────────────────────────────────────────


def test_neither_stream_truncated_means_caught_up():
    assert merge_cursors(None, None) is None


def test_a_truncated_entry_stream_sets_the_cursor():
    assert merge_cursors(1000, None) == 1000


def test_a_truncated_removal_stream_sets_the_cursor():
    assert merge_cursors(None, 300) == 300


def test_the_cursor_never_passes_an_incomplete_removal_stream():
    """The one that matters.

    Entries paginated out at 1000 while removals stopped at 300. Advancing to
    1000 would step the device over every removal between 300 and 1000, and a
    missed removal is the single fact here that no later pull re-derives - the
    row it would have to match on no longer carries the space id.
    """
    assert merge_cursors(1000, 300) == 300


def test_erring_low_is_the_only_safe_direction():
    """Symmetric: a truncated entry stream holds the cursor back too, even though
    re-sending entries is merely redundant."""
    assert merge_cursors(300, 1000) == 300


def test_a_shared_truncation_point_is_not_special_cased():
    assert merge_cursors(500, 500) == 500


def test_a_zero_cursor_is_a_position_not_an_absence():
    """`0` is falsy, and a `min` over a list built with a truthiness filter would
    silently drop it. It means "truncated at the very beginning", not "complete"."""
    assert merge_cursors(0, None) == 0
    assert merge_cursors(0, 900) == 0


# ── End to end ───────────────────────────────────────────────────────


def _shared_entry(client_id: str, space_id: str, ts: int) -> dict:
    return {
        "client_id": client_id,
        "entry_type": "clipboard",
        "kind": "text",
        "encrypted_content": "c2hhcmVkLXNlY3JldA==",
        "created_at": ts - 1000,
        "updated_at": ts,
        "space_ids": [space_id],
        "wrapped_keys": '{"personal":"aaa","%s":"bbb"}' % space_id,
    }


async def _pull(client: AsyncClient, headers: dict, after_ts: int = 0) -> dict:
    resp = await client.get(
        f"/api/v1/sync/pull?after_ts={after_ts}&limit=200", headers=clean(headers)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_the_recorded_author_is_not_whichever_row_came_back_first(client: AsyncClient):
    """Two accounts, one `client_id`, and the owner removing their own item.

    Rows are keyed `(user_id, client_id, entry_type)`, so two accounts can each
    hold a row under one `client_id` - a client build that pushed an entry it had
    received produced exactly that, and those rows are still on the server. The
    owner path clears every one of them, while `space_entry_removals` has a
    unique index on (space_id, client_id, entry_type) and so has room for one
    author.

    It used to take `matched[0]`, and that list is unordered. When the member's
    row came back first the removal was recorded as author=member,
    removed_by=owner - two different ids for what was the owner taking down
    their own item - and every client computing "did the author remove this"
    from the pair reported the owner as having been moderated by a space owner.

    The owner wrote one of these rows, so the owner is who the record has to
    name.
    """
    owner = await make_user(client, "owner-author@example.com")
    member = await make_user(client, "member-author@example.com")
    space = await create_space(client, owner)
    space_id = space["space_id"]
    await join(client, member, space, owner)

    ts = 2_000_000_000_000
    # Both accounts push under the same client_id. Order matters: the member
    # pushes first, so their row is the older one and the likelier to be
    # returned first by a query with no ORDER BY.
    for who, at in ((member, ts), (owner, ts + 1000)):
        pushed = await client.post(
            "/api/v1/sync/push",
            json={"entries": [_shared_entry("cid-contested", space_id, at)]},
            headers=clean(who),
        )
        assert pushed.status_code == 200, pushed.text

    gone = await client.delete(
        f"/api/v1/spaces/{space_id}/entries/cid-contested?entry_type=clipboard",
        headers=clean(owner),
    )
    assert gone.status_code == 204, gone.text

    removals = (await _pull(client, member))["removals"]
    row = next(r for r in removals if r["client_id"] == "cid-contested")
    assert row["removed_by"] == owner["_user_id"]
    assert row["author_id"] == owner["_user_id"], (
        "the remover wrote one of the cleared rows, so naming anyone else "
        "reports their own removal as somebody moderating them"
    )


@pytest.mark.asyncio
async def test_a_contested_client_id_names_an_author_stably(client: AsyncClient):
    """Same collision, but the remover wrote none of the rows.

    There is no right answer here - one row, two authors - so the requirement is
    only that it is the same answer every time. An arbitrary pick means two
    members pulling the same removal can be told two different things about who
    wrote it.
    """
    owner = await make_user(client, "arbiter@example.com")
    first = await make_user(client, "first@example.com")
    second = await make_user(client, "second@example.com")
    space = await create_space(client, owner)
    space_id = space["space_id"]
    await join(client, first, space, owner)
    await join(client, second, space, owner)

    ts = 2_000_000_000_000
    for who, at in ((first, ts), (second, ts + 1000)):
        pushed = await client.post(
            "/api/v1/sync/push",
            json={"entries": [_shared_entry("cid-neither", space_id, at)]},
            headers=clean(who),
        )
        assert pushed.status_code == 200, pushed.text

    gone = await client.delete(
        f"/api/v1/spaces/{space_id}/entries/cid-neither?entry_type=clipboard",
        headers=clean(owner),
    )
    assert gone.status_code == 204, gone.text

    row = next(
        r
        for r in (await _pull(client, first))["removals"]
        if r["client_id"] == "cid-neither"
    )
    assert row["removed_by"] == owner["_user_id"]
    # Lowest of the two ids, so the choice does not depend on row order.
    assert row["author_id"] == min(first["_user_id"], second["_user_id"])


@pytest.mark.asyncio
async def test_a_member_who_was_offline_learns_the_entry_was_withdrawn(client: AsyncClient):
    """The regression, end to end.

    No WebSocket anywhere in this test, which is the point: the member is exactly
    the device that was not connected when the withdrawal happened, so the event
    never reached them and the pull is all they have.
    """
    owner = await make_user(client, "owner@example.com")
    member = await make_user(client, "member@example.com")
    space = await create_space(client, owner)
    space_id = space["space_id"]
    await join(client, member, space, owner)

    pushed = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_shared_entry("cid-withdrawn", space_id, 2_000_000_000_000)]},
        headers=clean(owner),
    )
    assert pushed.status_code == 200, pushed.text

    # The member sees it, the ordinary way.
    seen = await _pull(client, member)
    assert [e["client_id"] for e in seen["entries"]] == ["cid-withdrawn"]
    cursor = max(e["server_ts"] for e in seen["entries"])

    # The owner takes it down while the member is not connected.
    gone = await client.delete(
        f"/api/v1/spaces/{space_id}/entries/cid-withdrawn?entry_type=clipboard",
        headers=clean(owner),
    )
    assert gone.status_code == 204, gone.text

    after = await _pull(client, member, after_ts=cursor)

    # This is the assertion the bug failed. The entry is genuinely absent from
    # the entries stream - that part was never in question and is why the row
    # alone can never carry the news.
    assert [e["client_id"] for e in after["entries"]] == []
    assert len(after["removals"]) == 1, after
    removal = after["removals"][0]
    assert removal["client_id"] == "cid-withdrawn"
    assert removal["space_id"] == space_id
    assert removal["entry_type"] == "clipboard"
    # Moderated, not self-withdrawn: the pair is what lets the client say which.
    assert removal["author_id"] == owner["_user_id"]
    assert removal["removed_by"] == owner["_user_id"]


@pytest.mark.asyncio
async def test_un_sharing_by_push_is_recorded_too(client: AsyncClient):
    """The other way an entry leaves a space: the author re-pushes it with the
    space dropped from `space_ids`. Same hole, different route in."""
    owner = await make_user(client, "author@example.com")
    member = await make_user(client, "reader@example.com")
    space = await create_space(client, owner)
    space_id = space["space_id"]
    await join(client, member, space, owner)

    ts = 2_000_000_000_000
    r1 = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_shared_entry("cid-unshared", space_id, ts)]},
        headers=clean(owner),
    )
    assert r1.status_code == 200, r1.text
    cursor = max(e["server_ts"] for e in (await _pull(client, member))["entries"])

    # Re-push with no spaces: an un-share.
    unshared = _shared_entry("cid-unshared", space_id, ts + 1000)
    unshared["space_ids"] = []
    unshared["wrapped_keys"] = '{"personal":"aaa"}'
    r2 = await client.post("/api/v1/sync/push", json={"entries": [unshared]}, headers=clean(owner))
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["accepted"]) == 1, r2.text

    after = await _pull(client, member, after_ts=cursor)
    assert [e["client_id"] for e in after["entries"]] == []
    assert [r["client_id"] for r in after["removals"]] == ["cid-unshared"]
    # Self-inflicted: this path is a push from the entry's own owner, so the two
    # ids match and the client shows "withdrawn" rather than "removed".
    assert after["removals"][0]["author_id"] == after["removals"][0]["removed_by"]


@pytest.mark.asyncio
async def test_a_re_shared_entry_survives_its_own_removal_record(client: AsyncClient):
    """Withdraw, then share again. The entry row carries the newer `server_ts`,
    so a client applying removals before entries ends up holding the entry -
    which is why the contract fixes that order rather than leaving it to taste."""
    owner = await make_user(client, "owner2@example.com")
    member = await make_user(client, "member2@example.com")
    space = await create_space(client, owner)
    space_id = space["space_id"]
    await join(client, member, space, owner)

    ts = 2_000_000_000_000
    await client.post(
        "/api/v1/sync/push",
        json={"entries": [_shared_entry("cid-again", space_id, ts)]},
        headers=clean(owner),
    )
    await client.delete(
        f"/api/v1/spaces/{space_id}/entries/cid-again?entry_type=clipboard",
        headers=clean(owner),
    )
    again = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_shared_entry("cid-again", space_id, ts + 5000)]},
        headers=clean(owner),
    )
    assert again.status_code == 200, again.text

    full = await _pull(client, member)
    entry = next(e for e in full["entries"] if e["client_id"] == "cid-again")
    removal = next(r for r in full["removals"] if r["client_id"] == "cid-again")
    assert entry["server_ts"] > removal["server_ts"], (
        "the re-share must be newer than the withdrawal, or applying removals "
        "first would drop a live entry"
    )


@pytest.mark.asyncio
async def test_removing_twice_leaves_one_record_with_the_later_timestamp(client: AsyncClient):
    """The unique index on (space_id, client_id, entry_type) is the upsert target,
    so a share/withdraw/re-share/withdraw cycle updates in place instead of
    accumulating a history nobody reads."""
    owner = await make_user(client, "owner3@example.com")
    member = await make_user(client, "member3@example.com")
    space = await create_space(client, owner)
    space_id = space["space_id"]
    await join(client, member, space, owner)

    ts = 2_000_000_000_000
    for offset in (0, 5000):
        await client.post(
            "/api/v1/sync/push",
            json={"entries": [_shared_entry("cid-twice", space_id, ts + offset)]},
            headers=clean(owner),
        )
        gone = await client.delete(
            f"/api/v1/spaces/{space_id}/entries/cid-twice?entry_type=clipboard",
            headers=clean(owner),
        )
        assert gone.status_code == 204, gone.text

    after = await _pull(client, member)
    mine = [r for r in after["removals"] if r["client_id"] == "cid-twice"]
    assert len(mine) == 1, mine


@pytest.mark.asyncio
async def test_a_stranger_is_not_told_about_another_space(client: AsyncClient):
    """Removals are scoped by membership exactly as entries are. Leaking them
    would be a slow disclosure of what other people share and take down."""
    owner = await make_user(client, "owner4@example.com")
    stranger = await make_user(client, "stranger@example.com")
    space = await create_space(client, owner)
    space_id = space["space_id"]

    await client.post(
        "/api/v1/sync/push",
        json={"entries": [_shared_entry("cid-private", space_id, 2_000_000_000_000)]},
        headers=clean(owner),
    )
    await client.delete(
        f"/api/v1/spaces/{space_id}/entries/cid-private?entry_type=clipboard",
        headers=clean(owner),
    )

    after = await _pull(client, stranger)
    assert after["removals"] == []
