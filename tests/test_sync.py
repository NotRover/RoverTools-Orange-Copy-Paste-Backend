"""Sync push/pull endpoint tests: LWW conflict resolution, cursor pagination."""

import time
import uuid

from httpx import AsyncClient


def _now_ms() -> int:
    return int(time.time() * 1000)


def cid(label: str) -> str:
    """A readable test label as the canonical UUID the server requires."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, label))


def _entry(label: str, *, ts: int | None = None, deleted: bool = False) -> dict:
    now = ts or _now_ms()
    return {
        "client_id": cid(label),
        "entry_type": "clipboard",
        "kind": "text",
        "encrypted_content": "dGVzdA==",
        "created_at": now - 1000,
        "updated_at": now,
        "deleted_at": now if deleted else None,
    }


async def test_push_single_entry(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("cid-001")]},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["accepted"]) == 1
    assert len(body["conflicts"]) == 0
    assert body["accepted"][0]["client_id"] == cid("cid-001")
    assert "server_id" in body["accepted"][0]
    assert "server_ts" in body["accepted"][0]


async def test_push_multiple_entries(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    entries = [_entry(f"cid-batch-{i}") for i in range(5)]
    resp = await client.post("/api/v1/sync/push", json={"entries": entries}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["accepted"]) == 5
    assert len(body["conflicts"]) == 0


async def test_push_lww_reject_older(client: AsyncClient, auth_headers: dict):
    """Re-pushing an entry with an older updated_at must be rejected as conflict."""
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    now = _now_ms()

    # First push (newer timestamp)
    r1 = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("cid-lww", ts=now)]},
        headers=headers,
    )
    assert r1.status_code == 200
    assert len(r1.json()["accepted"]) == 1

    # Second push (older timestamp) — must conflict
    r2 = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("cid-lww", ts=now - 5000)]},
        headers=headers,
    )
    assert r2.status_code == 200
    body = r2.json()
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["client_id"] == cid("cid-lww")


async def test_push_tombstone_wins(client: AsyncClient, auth_headers: dict):
    """A deleted entry pushed after a live one must be accepted."""
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    now = _now_ms()

    r1 = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("cid-tomb", ts=now)]},
        headers=headers,
    )
    assert r1.status_code == 200

    # Push tombstone (later timestamp)
    r2 = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("cid-tomb", ts=now + 1000, deleted=True)]},
        headers=headers,
    )
    assert r2.status_code == 200
    assert len(r2.json()["accepted"]) == 1


async def test_pull_empty(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.get("/api/v1/sync/pull?after_ts=9999999999999", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["next_cursor"] is None


async def test_pull_returns_pushed_entries(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    push = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("cid-pull-1"), _entry("cid-pull-2")]},
        headers=headers,
    )
    assert push.status_code == 200

    resp = await client.get("/api/v1/sync/pull?after_ts=0", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    client_ids = {e["client_id"] for e in body["entries"]}
    assert cid("cid-pull-1") in client_ids
    assert cid("cid-pull-2") in client_ids


async def test_pull_cursor_pagination(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    # Pushed one at a time so each lands on its own server_ts.
    for i in range(3):
        await client.post("/api/v1/sync/push", json={"entries": [_entry(f"cid-page-{i}")]}, headers=headers)
        time.sleep(0.002)

    # Pull with limit=2 — should get a next_cursor
    resp = await client.get("/api/v1/sync/pull?after_ts=0&limit=2", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 2
    assert body["next_cursor"] is not None

    # Second page
    resp2 = await client.get(f"/api/v1/sync/pull?after_ts={body['next_cursor']}&limit=2", headers=headers)
    assert resp2.status_code == 200
    assert len(resp2.json()["entries"]) == 1


async def test_a_page_never_splits_one_server_ts(client: AsyncClient, auth_headers: dict, db):
    """The cursor is a bare timestamp and the next pull asks for `>` it, so a
    page that stopped inside a run of rows sharing one `server_ts` lost the rest
    of the run for good. Walking every page must return every row."""
    from sqlalchemy import update

    from src.sync.models import SyncEntry

    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    labels = [f"cid-tie-{i}" for i in range(5)]
    await client.post("/api/v1/sync/push", json={"entries": [_entry(x) for x in labels]}, headers=headers)
    # Three of them share one timestamp, the way a batch or a removal stamps them.
    await db.execute(
        update(SyncEntry).where(SyncEntry.client_id.in_([cid(x) for x in labels[1:4]])).values(server_ts=10)
    )
    await db.execute(update(SyncEntry).where(SyncEntry.client_id == cid(labels[0])).values(server_ts=5))
    await db.execute(update(SyncEntry).where(SyncEntry.client_id == cid(labels[4])).values(server_ts=20))
    await db.commit()

    for limit in (1, 2, 3):
        seen: list[str] = []
        cursor = 0
        for _ in range(10):
            page = (
                await client.get(f"/api/v1/sync/pull?after_ts={cursor}&limit={limit}", headers=headers)
            ).json()
            seen += [e["client_id"] for e in page["entries"]]
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        assert sorted(seen) == sorted(cid(x) for x in labels), limit



async def test_update_cursor(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.post(
        "/api/v1/sync/cursor",
        json={"last_server_ts": _now_ms()},
        headers=headers,
    )
    assert resp.status_code == 204


# ── Input shape ───────────────────────────────────────────────────────────────


async def test_a_client_id_that_is_not_a_canonical_uuid_is_refused(
    client: AsyncClient, auth_headers: dict
):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    for bad in ("cid-001", str(uuid.uuid4()).upper(), "{" + str(uuid.uuid4()) + "}", uuid.uuid4().hex):
        entry = _entry("x")
        entry["client_id"] = bad
        resp = await client.post("/api/v1/sync/push", json={"entries": [entry]}, headers=headers)
        assert resp.status_code == 422, bad


async def test_a_timestamp_far_in_the_future_is_refused(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    ahead = _entry("cid-future", ts=_now_ms() + 3600 * 1000)
    resp = await client.post("/api/v1/sync/push", json={"entries": [ahead]}, headers=headers)
    assert resp.status_code == 422

    huge = _entry("cid-huge")
    huge["created_at"] = 2**64
    resp = await client.post("/api/v1/sync/push", json={"entries": [huge]}, headers=headers)
    assert resp.status_code == 422

    # A small skew is an ordinary clock and still goes through.
    near = _entry("cid-near", ts=_now_ms() + 60 * 1000)
    resp = await client.post("/api/v1/sync/push", json={"entries": [near]}, headers=headers)
    assert resp.status_code == 200 and len(resp.json()["accepted"]) == 1


async def test_field_caps_and_kinds(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    def with_(**fields) -> dict:
        entry = _entry("cid-caps")
        entry.update(fields)
        return entry

    for body in (
        with_(kind="something-else"),
        with_(wrapped_keys="x" * 8193),
        with_(blob_key="../../etc/passwd"),
        with_(space_ids=[str(uuid.uuid4()) for _ in range(33)]),
    ):
        resp = await client.post("/api/v1/sync/push", json={"entries": [body]}, headers=headers)
        assert resp.status_code == 422, body

    # The tombstone kind the client sends is an empty string.
    tomb = _entry("cid-kind-tomb", deleted=True)
    tomb["kind"] = ""
    resp = await client.post("/api/v1/sync/push", json={"entries": [tomb]}, headers=headers)
    assert resp.status_code == 200 and len(resp.json()["accepted"]) == 1


async def test_a_tombstone_keeps_no_payload(client: AsyncClient, auth_headers: dict, db):
    """A deleted row is never decrypted, so it must not keep ciphertext: a
    tombstone is not counted against the live-row cap, and would otherwise be
    somewhere to park data for free."""
    from sqlalchemy import select

    from src.sync.models import SyncEntry

    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    now = _now_ms()

    fresh = _entry("cid-strip-new", deleted=True)
    fresh["encrypted_content"] = "A" * 1000
    fresh["encrypted_metadata"] = "B" * 1000
    fresh["wrapped_keys"] = '{"personal": "' + "C" * 500 + '"}'
    live = _entry("cid-strip-old", ts=now)
    live["wrapped_keys"] = '{"personal": "k"}'
    r = await client.post("/api/v1/sync/push", json={"entries": [fresh, live]}, headers=headers)
    assert len(r.json()["accepted"]) == 2

    gone = _entry("cid-strip-old", ts=now + 1000, deleted=True)
    gone["encrypted_content"] = "D" * 1000
    await client.post("/api/v1/sync/push", json={"entries": [gone]}, headers=headers)

    rows = (
        await db.scalars(
            select(SyncEntry).where(SyncEntry.client_id.in_([cid("cid-strip-new"), cid("cid-strip-old")]))
        )
    ).all()
    assert len(rows) == 2
    for row in rows:
        assert row.deleted_at is not None
        assert row.encrypted_content == ""
        assert row.encrypted_metadata is None
        assert row.wrapped_keys == "{}"


async def test_tombstones_have_a_cap_of_their_own(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    from src.config import settings
    from src.sync import service

    monkeypatch.setattr(settings, "max_entries_per_user", 1)
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    cap = settings.max_entries_per_user * service._TOMBSTONE_CAP_FACTOR
    tombs = [_entry(f"cid-cap-tomb-{i}", deleted=True) for i in range(cap + 1)]
    resp = await client.post("/api/v1/sync/push", json={"entries": tombs}, headers=headers)
    body = resp.json()
    assert len(body["accepted"]) == cap
    assert [c["reason"] for c in body["conflicts"]] == ["account_full"]


async def test_reviving_a_tombstone_takes_a_live_slot(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    from src.config import settings

    monkeypatch.setattr(settings, "max_entries_per_user", 1)
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    now = _now_ms()
    await client.post(
        "/api/v1/sync/push", json={"entries": [_entry("cid-rev-a", ts=now, deleted=True)]}, headers=headers
    )
    await client.post("/api/v1/sync/push", json={"entries": [_entry("cid-rev-b", ts=now)]}, headers=headers)
    back = await client.post(
        "/api/v1/sync/push", json={"entries": [_entry("cid-rev-a", ts=now + 1000)]}, headers=headers
    )
    assert [c["reason"] for c in back.json()["conflicts"]] == ["account_full"]


async def test_a_blob_key_must_be_the_callers_confirmed_blob(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    entry = _entry("cid-planted")
    entry["kind"] = "image"
    entry["blob_key"] = f"{uuid.uuid4()}/{uuid.uuid4().hex}"
    resp = await client.post("/api/v1/sync/push", json={"entries": [entry]}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid_blob"


async def test_a_cursor_write_cannot_move_another_accounts_cursor(
    client: AsyncClient, auth_headers: dict, db
):
    from sqlalchemy import select

    from src.sync.models import SyncCursor
    from src.sync.service import update_cursor

    theirs = uuid.uuid4()
    db.add(SyncCursor(device_id=theirs, user_id=uuid.uuid4(), last_server_ts=5))
    await db.commit()
    await update_cursor(db, str(theirs), auth_headers["_user_id"], 999)
    row = await db.scalar(select(SyncCursor).where(SyncCursor.device_id == theirs))
    assert row is not None and row.last_server_ts == 5


# ── Limits ────────────────────────────────────────────────────────────────────


async def test_an_oversized_entry_is_refused_with_a_reason(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    """Refused as a conflict, not a 422: the client turns the reason into a line
    the user reads, and the rest of the push still goes through."""
    from src.config import settings

    monkeypatch.setattr(settings, "max_entry_bytes", 16)
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    big = _entry("cid-big")
    big["encrypted_content"] = "A" * 64

    resp = await client.post(
        "/api/v1/sync/push",
        json={"entries": [big, _entry("cid-small")]},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["reason"] for c in body["conflicts"]] == ["entry_too_large"]
    assert [a["client_id"] for a in body["accepted"]] == [cid("cid-small")]


async def test_oversized_metadata_is_refused_the_same_way(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    """`encrypted_metadata` is ciphertext on the same row and was unbounded
    while `encrypted_content` was capped, so the ceiling could be walked around
    by putting the payload in the other field."""
    from src.config import settings

    monkeypatch.setattr(settings, "max_entry_bytes", 16)
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    big = _entry("cid-big-meta")
    big["encrypted_metadata"] = "A" * 64

    resp = await client.post(
        "/api/v1/sync/push",
        json={"entries": [big, _entry("cid-small")]},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["reason"] for c in body["conflicts"]] == ["entry_too_large"]
    assert [a["client_id"] for a in body["accepted"]] == [cid("cid-small")]


async def test_a_full_account_refuses_new_rows_but_still_accepts_deletes(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    """The important half is the second one. An account that could not be
    emptied once it filled would be a trap, so the cap only ever blocks an
    insert - a tombstone for a row that exists is an update and gets through."""
    from src.config import settings

    monkeypatch.setattr(settings, "max_entries_per_user", 1)
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    first = await client.post(
        "/api/v1/sync/push", json={"entries": [_entry("cid-1")]}, headers=headers
    )
    assert len(first.json()["accepted"]) == 1

    second = await client.post(
        "/api/v1/sync/push", json={"entries": [_entry("cid-2")]}, headers=headers
    )
    assert [c["reason"] for c in second.json()["conflicts"]] == ["account_full"]

    gone = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("cid-1", deleted=True)]},
        headers=headers,
    )
    assert len(gone.json()["accepted"]) == 1

    # And the room it freed is usable again.
    again = await client.post(
        "/api/v1/sync/push", json={"entries": [_entry("cid-3")]}, headers=headers
    )
    assert len(again.json()["accepted"]) == 1


async def test_a_full_account_still_accepts_a_brand_new_tombstone(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    """A received-entry hide ("remove from my devices") is a brand-new tombstone
    row, not an update to one that exists. It adds nothing to a count that excludes
    tombstones, so a full account must still take it - otherwise that hide is the
    one delete quota could block. And it consumes no live slot, so the account
    stays full for a new *live* row."""
    from src.config import settings

    monkeypatch.setattr(settings, "max_entries_per_user", 1)
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    filled = await client.post(
        "/api/v1/sync/push", json={"entries": [_entry("cid-live")]}, headers=headers
    )
    assert len(filled.json()["accepted"]) == 1

    # A tombstone for a client_id this account never held live - a new row, at quota.
    hide = await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("cid-received", deleted=True)]},
        headers=headers,
    )
    assert len(hide.json()["accepted"]) == 1
    assert len(hide.json()["conflicts"]) == 0

    # It freed nothing: the live row still fills the account, so a new live row
    # is still refused.
    blocked = await client.post(
        "/api/v1/sync/push", json={"entries": [_entry("cid-another")]}, headers=headers
    )
    assert [c["reason"] for c in blocked.json()["conflicts"]] == ["account_full"]


async def test_an_oversized_batch_is_rejected_outright(
    client: AsyncClient, auth_headers: dict
):
    """No per-entry reasons here: the client pushes one entry per request, so a
    batch this size is not the app asking."""
    from src.config import settings

    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    entries = [_entry(f"cid-{i}") for i in range(settings.max_push_batch + 1)]

    resp = await client.post("/api/v1/sync/push", json={"entries": entries}, headers=headers)
    assert resp.status_code == 422


async def test_breakdown_counts_live_rows_by_kind(
    client: AsyncClient, auth_headers: dict
):
    """The cloud bar's numbers come straight from one GROUP BY: notes split out,
    clipboard split by kind, a tombstone counted in neither, and an unknown kind
    folded into text so the parts sum to `clipboard`."""
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    def _kind(label: str, entry_type: str, kind: str | None) -> dict:
        now = _now_ms()
        return {
            "client_id": cid(label),
            "entry_type": entry_type,
            "kind": kind,
            "encrypted_content": "dGVzdA==",
            "created_at": now - 1000,
            "updated_at": now,
            "deleted_at": None,
        }

    entries = [
        _kind("bd-text-1", "clipboard", "text"),
        _kind("bd-text-2", "clipboard", "text"),
        _kind("bd-legacy", "clipboard", None),  # counts as text
        _kind("bd-image", "clipboard", "image"),
        _kind("bd-file", "clipboard", "file"),
        _kind("bd-html", "clipboard", "html"),
        _kind("bd-note", "note", "text"),
    ]
    await client.post("/api/v1/sync/push", json={"entries": entries}, headers=headers)

    # A tombstone must not be counted.
    await client.post(
        "/api/v1/sync/push",
        json={"entries": [_entry("bd-gone", deleted=True)]},
        headers=headers,
    )

    resp = await client.get("/api/v1/sync/breakdown", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "clipboard": 6,
        "notes": 1,
        "total": 7,
        "text": 3,
        "image": 1,
        "file": 1,
        "html": 1,
    }
