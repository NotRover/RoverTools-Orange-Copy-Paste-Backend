"""Server-authored announcements: addressing, the client watermark, and expiry."""

import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from src.config import settings
from tests.conftest import make_token

ADMIN = {"X-Admin-Key": "test-admin-key"}


@pytest.fixture(autouse=True)
def _enable_admin():
    """The admin surface 503s unless a key is configured."""
    with patch.object(settings, "admin_api_key", "test-admin-key"):
        yield


def _user(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if not k.startswith("_")}


async def _post(client: AsyncClient, **body) -> dict:
    resp = await client.post("/internal/v1/admin/announcements", json=body, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _list(client: AsyncClient, headers: dict, since: int | None = None) -> list[dict]:
    url = "/api/v1/announcements" + (f"?since={since}" if since is not None else "")
    resp = await client.get(url, headers=_user(headers))
    assert resp.status_code == 200, resp.text
    return resp.json()["announcements"]


async def test_broadcast_reaches_a_user(client: AsyncClient, auth_headers: dict):
    created = await _post(client, title="Maintenance Saturday", body="Sync pauses for an hour.")
    assert created["delivered_to"] == "everyone"

    rows = await _list(client, auth_headers)
    assert [r["title"] for r in rows] == ["Maintenance Saturday"]
    assert rows[0]["kind"] == "announcement"


async def test_addressed_announcement_is_not_shared(client: AsyncClient, auth_headers: dict):
    # A real second account, not the same token again - addressing one user has
    # nothing to prove unless somebody else exists to be excluded.
    other_id = str(uuid.uuid4())
    boot = await client.post(
        "/api/v1/auth/bootstrap",
        json={"display_name": "Other"},
        headers={"Authorization": f"Bearer {make_token(other_id)}"},
    )
    assert boot.status_code == 200, boot.text

    await _post(client, title="Just for them", user_id=other_id, kind="reminder")
    assert await _list(client, auth_headers) == []

    await _post(client, title="Yours", user_id=auth_headers["_user_id"])
    assert [r["title"] for r in await _list(client, auth_headers)] == ["Yours"]


async def test_unknown_user_is_rejected(client: AsyncClient):
    resp = await client.post(
        "/internal/v1/admin/announcements",
        json={"title": "Nobody", "user_id": str(uuid.uuid4())},
        headers=ADMIN,
    )
    assert resp.status_code == 400


async def test_since_watermark_excludes_what_was_already_handed_over(
    client: AsyncClient, auth_headers: dict
):
    await _post(client, title="First")
    first = await _list(client, auth_headers)
    watermark = first[0]["created_at"]

    # Nothing new: the client asking again gets nothing back, which is what lets
    # it dismiss a row without the next refresh resurrecting it.
    assert await _list(client, auth_headers, since=watermark) == []

    await _post(client, title="Second")
    later = await _list(client, auth_headers, since=watermark)
    assert [r["title"] for r in later] == ["Second"]


async def test_expired_announcement_stops_being_served(client: AsyncClient, auth_headers: dict):
    await _post(client, title="Window opens now", ttl_ms=1)
    # ttl_ms of 1 has already elapsed by the time the read runs.
    assert await _list(client, auth_headers) == []


async def test_delete_stops_serving_it(client: AsyncClient, auth_headers: dict):
    created = await _post(client, title="Recalled")
    assert len(await _list(client, auth_headers)) == 1

    resp = await client.delete(f"/internal/v1/admin/announcements/{created['id']}", headers=ADMIN)
    assert resp.status_code == 200
    assert await _list(client, auth_headers) == []


async def test_admin_key_is_required(client: AsyncClient):
    resp = await client.post("/internal/v1/admin/announcements", json={"title": "No key"})
    assert resp.status_code in (401, 403)
