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
