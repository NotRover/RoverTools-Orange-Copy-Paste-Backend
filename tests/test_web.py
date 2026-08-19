"""Tests for the two human-facing HTML pages.

Neither page needs auth or a database row, so these only assert what a person
clicking a link depends on: a 200, the deep link the app is handed, and a policy
header that still permits the page's own inline style.
"""

from httpx import AsyncClient

from src.web import router as web


async def test_join_page_renders_without_auth(client: AsyncClient):
    resp = await client.get("/join/KX7Q-2M4X")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "KX7Q-2M4X" in body
    assert "orange://join?code=KX7Q2M4X" in body


async def test_join_page_accepts_the_undashed_code(client: AsyncClient):
    """The two surfaces disagree on format; both must land on the same page."""
    resp = await client.get("/join/kx7q2m4x")
    assert resp.status_code == 200
    assert "KX7Q-2M4X" in resp.text


async def test_join_page_sets_its_own_csp(client: AsyncClient):
    """The global policy is `default-src 'none'`, which would blank the page."""
    resp = await client.get("/join/KX7Q-2M4X")
    csp = resp.headers["content-security-policy"]
    assert "style-src 'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp


async def test_reset_page_carries_the_code(client: AsyncClient):
    resp = await client.get("/reset", params={"code": "abc123"})
    assert resp.status_code == 200
    assert "orange://reset?code=abc123" in resp.text
    assert 'class="ok"' in resp.text


async def test_reset_page_without_a_code_explains_itself(client: AsyncClient):
    """A link that has been used or expired arrives with no code at all."""
    resp = await client.get("/reset")
    assert resp.status_code == 200
    assert 'class="fail"' in resp.text
    assert "orange://reset" not in resp.text


def test_join_url_normalizes_before_building():
    assert web.join_url("kx7q2m4x").endswith("/join/KX7Q-2M4X")
    assert web.join_url("KX7Q-2M4X") == web.join_url("kx7q2m4x")
