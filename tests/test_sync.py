"""Sync push/pull endpoint tests: LWW conflict resolution, cursor pagination."""

import time

from httpx import AsyncClient


def _now_ms() -> int:
    return int(time.time() * 1000)


def _entry(client_id: str, *, ts: int | None = None, deleted: bool = False) -> dict:
    now = ts or _now_ms()
    return {
        "client_id": client_id,
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
    assert body["accepted"][0]["client_id"] == "cid-001"
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
    assert body["conflicts"][0]["client_id"] == "cid-lww"


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
    assert "cid-pull-1" in client_ids
    assert "cid-pull-2" in client_ids


async def test_pull_cursor_pagination(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}

    # Push 3 entries
    entries = [_entry(f"cid-page-{i}") for i in range(3)]
    await client.post("/api/v1/sync/push", json={"entries": entries}, headers=headers)

    # Pull with limit=2 — should get a next_cursor
    resp = await client.get("/api/v1/sync/pull?after_ts=0&limit=2", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 2
    assert body["next_cursor"] is not None

    # Second page
    resp2 = await client.get(f"/api/v1/sync/pull?after_ts={body['next_cursor']}&limit=2", headers=headers)
    assert resp2.status_code == 200
    assert len(resp2.json()["entries"]) >= 1



async def test_update_cursor(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.post(
        "/api/v1/sync/cursor",
        json={"last_server_ts": _now_ms()},
        headers=headers,
    )
    assert resp.status_code == 204


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
    assert [a["client_id"] for a in body["accepted"]] == ["cid-small"]


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
    assert [a["client_id"] for a in body["accepted"]] == ["cid-small"]


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
