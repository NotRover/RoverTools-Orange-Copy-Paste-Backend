"""Blob upload/quota endpoint tests (presigned-URL generation is mocked)."""

from unittest.mock import patch

from httpx import AsyncClient


async def test_request_upload(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    with patch("src.blobs.s3.generate_presigned_put", return_value="https://r2.example.com/put?sig=test"):
        resp = await client.post(
            "/api/v1/blobs/request-upload",
            json={"mime_type": "image/png", "size_bytes": 1024, "checksum": "abc123"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blob_key"]
    assert body["presigned_put_url"].startswith("https://")
    assert body["expires_in_seconds"] == 300


async def test_request_upload_over_5mb_rejected(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.post(
        "/api/v1/blobs/request-upload",
        json={"mime_type": "image/png", "size_bytes": 6_000_000, "checksum": "abc"},
        headers=headers,
    )
    assert resp.status_code == 413


async def test_quota(client: AsyncClient, auth_headers: dict):
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    resp = await client.get("/api/v1/blobs/quota", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["used_bytes"] == 0
    assert body["quota_bytes"] == 52_428_800  # 50 MB default


async def _upload_blob(client: AsyncClient, headers: dict, size: int) -> str:
    """Request and confirm a blob, returning its key."""
    with patch("src.blobs.s3.generate_presigned_put", return_value="https://r2.example.com/put"):
        resp = await client.post(
            "/api/v1/blobs/request-upload",
            json={"mime_type": "image/png", "size_bytes": size, "checksum": "abc"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    key = resp.json()["blob_key"]
    confirm = await client.post("/api/v1/blobs/confirm-upload", json={"blob_key": key}, headers=headers)
    assert confirm.status_code == 204, confirm.text
    return key


async def _used_bytes(client: AsyncClient, headers: dict) -> int:
    resp = await client.get("/api/v1/blobs/quota", headers=headers)
    assert resp.status_code == 200
    return resp.json()["used_bytes"]


def _image_entry(client_id: str, blob_key: str | None, *, ts: int, deleted: bool = False) -> dict:
    return {
        "client_id": client_id,
        "entry_type": "clipboard",
        "kind": "image",
        "encrypted_content": "dGVzdA==",
        "created_at": ts - 1000,
        "updated_at": ts,
        "deleted_at": ts if deleted else None,
        "blob_key": blob_key,
        "blob_size": 1024,
    }


async def test_tombstone_releases_blob(client: AsyncClient, auth_headers: dict):
    """Deleting an image entry must give its storage back.

    Without this the quota only ever grows: nothing else references the blob,
    and the orphan reaper only collects unconfirmed ones.
    """
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    now = 1_800_000_000_000
    key = await _upload_blob(client, headers, 1024)
    assert await _used_bytes(client, headers) == 1024

    await client.post(
        "/api/v1/sync/push",
        json={"entries": [_image_entry("cid-blob-del", key, ts=now)]},
        headers=headers,
    )
    await client.post(
        "/api/v1/sync/push",
        json={"entries": [_image_entry("cid-blob-del", None, ts=now + 1, deleted=True)]},
        headers=headers,
    )

    assert await _used_bytes(client, headers) == 0


async def test_replacing_an_image_releases_the_old_blob(client: AsyncClient, auth_headers: dict):
    """Re-pushing an image uploads a fresh object; the previous one must go.

    A pin or group edit re-pushes the entry, so without this every such edit
    permanently leaks one image's worth of quota.
    """
    headers = {k: v for k, v in auth_headers.items() if not k.startswith("_")}
    now = 1_800_000_000_000
    first = await _upload_blob(client, headers, 1024)
    await client.post(
        "/api/v1/sync/push",
        json={"entries": [_image_entry("cid-blob-swap", first, ts=now)]},
        headers=headers,
    )

    second = await _upload_blob(client, headers, 2048)
    await client.post(
        "/api/v1/sync/push",
        json={"entries": [_image_entry("cid-blob-swap", second, ts=now + 1)]},
        headers=headers,
    )

    # Only the replacement counts.
    assert await _used_bytes(client, headers) == 2048
