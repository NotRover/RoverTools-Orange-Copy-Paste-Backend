"""The request body ceiling.

Separate from `test_sync.py` and deliberately without the `client` fixture: a
refusal happens before any route runs, so these need no database, and keeping
them independent of one means the guard stays testable wherever it is run.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.main import app

PUSH = "/api/v1/sync/push"


@pytest_asyncio.fixture
async def bare_client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_a_declared_oversized_body_is_refused(bare_client: AsyncClient):
    """The cheap path: the client says how big it is, so nothing is read."""
    body = b"x" * (settings.max_request_bytes + 1)

    resp = await bare_client.post(PUSH, content=body, headers={"content-type": "application/json"})

    assert resp.status_code == 413, resp.text
    # The client reads `detail` off every error; a 413 it cannot read is a 413
    # the user sees as "the server rejected the request (413)".
    assert "detail" in resp.json()


@pytest.mark.asyncio
async def test_an_undeclared_oversized_body_is_refused(bare_client: AsyncClient):
    """The path that matters: no `Content-Length` to check, so only the running
    total stops it. A cap that a chunked request walks past is not a cap."""

    async def chunks():
        # Comfortably over in a handful of chunks, none of them large.
        for _ in range(9):
            yield b"x" * (settings.max_request_bytes // 8)

    resp = await bare_client.post(
        PUSH, content=chunks(), headers={"content-type": "application/json"}
    )

    assert "content-length" not in {k.lower() for k in resp.request.headers}
    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_an_ordinary_body_is_not_touched(bare_client: AsyncClient):
    """The guard must be invisible to everything else. No credentials here, so
    the answer is 401/403 - anything but a 413 proves the body got through."""
    resp = await bare_client.post(PUSH, json={"entries": []})

    assert resp.status_code != 413, resp.text


@pytest.mark.asyncio
async def test_a_body_at_the_limit_is_not_refused(bare_client: AsyncClient):
    """Off-by-one in the wrong direction refuses requests that should have been
    served, which is the more expensive mistake of the two."""
    body = b"x" * settings.max_request_bytes

    resp = await bare_client.post(PUSH, content=body, headers={"content-type": "application/json"})

    assert resp.status_code != 413, resp.text
