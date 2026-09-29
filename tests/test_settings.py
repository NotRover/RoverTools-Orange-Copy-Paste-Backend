"""Settings PUT: last-write-wins without `base_updated_at`, compare-and-swap with it."""

import asyncio
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.settings import service
from src.settings.models import UserSettings
from src.settings.schemas import SettingsPutRequest, SettingsPutResponse

URL = "/api/v1/settings"
_NO_BASE = object()


def _headers(auth_headers: dict) -> dict:
    return {k: v for k, v in auth_headers.items() if not k.startswith("_")}


async def _put(client: AsyncClient, auth_headers: dict, blob: str, updated_at: int, base=_NO_BASE) -> dict:
    body: dict = {"encrypted_blob": blob, "updated_at": updated_at}
    if base is not _NO_BASE:
        body["base_updated_at"] = base
    resp = await client.put(URL, json=body, headers=_headers(auth_headers))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _stored(db: AsyncSession, user_id: str) -> tuple[str, int] | None:
    row = (
        await db.execute(
            select(UserSettings.encrypted_blob, UserSettings.updated_at).where(
                UserSettings.user_id == uuid.UUID(user_id)
            )
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


# ── No base: last-write-wins, as before the field existed ──────────────


async def test_without_a_base_a_newer_stored_blob_wins(client: AsyncClient, auth_headers: dict, db: AsyncSession):
    await _put(client, auth_headers, "newer", 200)

    body = await _put(client, auth_headers, "older", 100)

    assert body == {"updated_at": 200, "winner": "server", "encrypted_blob": "newer"}
    assert await _stored(db, auth_headers["_user_id"]) == ("newer", 200)


@pytest.mark.parametrize("new_at", [200, 100], ids=["newer", "same-updated-at"])
async def test_without_a_base_a_request_not_older_writes(
    client: AsyncClient, auth_headers: dict, db: AsyncSession, new_at: int
):
    await _put(client, auth_headers, "first", 100)

    body = await _put(client, auth_headers, "second", new_at)

    assert body == {"updated_at": new_at, "winner": "client", "encrypted_blob": "second"}
    assert await _stored(db, auth_headers["_user_id"]) == ("second", new_at)


async def test_an_explicit_null_base_is_last_write_wins(client: AsyncClient, auth_headers: dict, db: AsyncSession):
    await _put(client, auth_headers, "first", 100)

    # A same-`updated_at` write: last-write-wins takes it, a base of 100 would not.
    body = await _put(client, auth_headers, "second", 100, base=None)

    assert body == {"updated_at": 100, "winner": "client", "encrypted_blob": "second"}
    assert await _stored(db, auth_headers["_user_id"]) == ("second", 100)


# ── With a base: write only over the blob the client merged from ───────


async def test_a_base_matching_the_stored_blob_writes(client: AsyncClient, auth_headers: dict, db: AsyncSession):
    await _put(client, auth_headers, "pulled", 100)

    body = await _put(client, auth_headers, "merged", 101, base=100)

    assert body == {"updated_at": 101, "winner": "client", "encrypted_blob": "merged"}
    assert await _stored(db, auth_headers["_user_id"]) == ("merged", 101)


async def test_a_stale_base_returns_the_stored_blob_and_writes_nothing(
    client: AsyncClient, auth_headers: dict, db: AsyncSession
):
    """The case the field exists for: another device wrote between this one's GET
    and PUT. The request is newer by the clock and would win last-write-wins; with
    the base it is refused, so the other device's change is not overwritten."""
    await _put(client, auth_headers, "pulled", 100)
    await _put(client, auth_headers, "other-device", 150, base=100)

    body = await _put(client, auth_headers, "merged-from-pulled", 200, base=100)

    assert body == {"updated_at": 150, "winner": "server", "encrypted_blob": "other-device"}
    assert await _stored(db, auth_headers["_user_id"]) == ("other-device", 150)


@pytest.mark.parametrize("base", [0, 12345], ids=["zero", "nonzero"])
async def test_a_base_with_nothing_stored_writes(
    client: AsyncClient, auth_headers: dict, db: AsyncSession, base: int
):
    body = await _put(client, auth_headers, "first", 100, base=base)

    assert body == {"updated_at": 100, "winner": "client", "encrypted_blob": "first"}
    assert await _stored(db, auth_headers["_user_id"]) == ("first", 100)


async def test_base_zero_with_a_blob_stored_returns_the_stored_blob(
    client: AsyncClient, auth_headers: dict, db: AsyncSession
):
    """Two devices both saw 404 and both push their first blob: the second is refused."""
    await _put(client, auth_headers, "first-device", 100, base=0)

    body = await _put(client, auth_headers, "second-device", 200, base=0)

    assert body == {"updated_at": 100, "winner": "server", "encrypted_blob": "first-device"}
    assert await _stored(db, auth_headers["_user_id"]) == ("first-device", 100)


@pytest.mark.parametrize("new_at", [100, 99], ids=["equal", "older"])
async def test_a_matching_base_does_not_move_updated_at_backwards(
    client: AsyncClient, auth_headers: dict, db: AsyncSession, new_at: int
):
    await _put(client, auth_headers, "pulled", 100)

    body = await _put(client, auth_headers, "merged", new_at, base=100)

    assert body == {"updated_at": 100, "winner": "server", "encrypted_blob": "pulled"}
    assert await _stored(db, auth_headers["_user_id"]) == ("pulled", 100)


async def test_only_a_written_blob_is_announced(client: AsyncClient, auth_headers: dict, fake_redis):
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe(f"user:{auth_headers['_user_id']}")
    await pubsub.get_message(timeout=0.2)  # the subscribe confirmation
    try:
        await _put(client, auth_headers, "first", 100, base=0)
        await _put(client, auth_headers, "stale", 200, base=50)
        events = []
        while (msg := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)) is not None:
            events.append(json.loads(msg["data"]))
    finally:
        await pubsub.aclose()

    assert [(e["event"], e["payload"]) for e in events] == [("settings:updated", {"updated_at": 100})]


# ── Concurrent PUTs ────────────────────────────────────────────────────
#
# The `db` fixture is one connection shared by every request in a test, so it
# cannot run two statements at once. These tests go through the service on their
# own sessions from the engine, commit for real, and delete their row after.


async def _race(
    test_engine, user_id: str, seed: SettingsPutRequest, a: SettingsPutRequest, b: SettingsPutRequest
) -> tuple[list[SettingsPutResponse], tuple[str, int]]:
    """Run `a` and `b` concurrently against a stored `seed`.

    A third session holds the row with `FOR UPDATE` until both PUTs are waiting on
    it, so they genuinely overlap instead of happening to run one after the other.
    """
    uid = uuid.UUID(user_id)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as s:
        await service.put_settings(s, user_id, seed)
    try:
        async with sessions() as blocker, sessions() as s1, sessions() as s2:
            await blocker.execute(select(UserSettings.user_id).where(UserSettings.user_id == uid).with_for_update())
            pids = [await s.scalar(text("SELECT pg_backend_pid()")) for s in (s1, s2)]
            puts = asyncio.gather(service.put_settings(s1, user_id, a), service.put_settings(s2, user_id, b))
            try:
                for _ in range(100):
                    waiting = await blocker.scalar(
                        text("SELECT count(*) FROM unnest(CAST(:pids AS int[])) AS p WHERE cardinality(pg_blocking_pids(p)) > 0"),
                        {"pids": pids},
                    )
                    if waiting == 2:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("the two PUTs never both waited on the row")
            finally:
                await blocker.rollback()
            results = list(await puts)
        async with sessions() as s:
            row = (
                await s.execute(
                    select(UserSettings.encrypted_blob, UserSettings.updated_at).where(UserSettings.user_id == uid)
                )
            ).one()
        return results, (row[0], row[1])
    finally:
        async with sessions() as s:
            await s.execute(delete(UserSettings).where(UserSettings.user_id == uid))
            await s.commit()


async def test_two_puts_merged_from_the_same_blob_have_one_winner(test_engine):
    user_id = str(uuid.uuid4())
    results, stored = await _race(
        test_engine,
        user_id,
        SettingsPutRequest(encrypted_blob="pulled", updated_at=100),
        SettingsPutRequest(encrypted_blob="from-a", updated_at=201, base_updated_at=100),
        SettingsPutRequest(encrypted_blob="from-b", updated_at=202, base_updated_at=100),
    )

    winners = [r for r in results if r.winner == "client"]
    losers = [r for r in results if r.winner == "server"]
    assert len(winners) == 1 and len(losers) == 1
    won = winners[0]
    # The loser is handed the winner's blob to merge into, and that is what is stored.
    assert (losers[0].encrypted_blob, losers[0].updated_at) == (won.encrypted_blob, won.updated_at)
    assert stored == (won.encrypted_blob, won.updated_at)


async def test_without_a_base_a_racing_older_put_does_not_overwrite_a_newer_one(test_engine):
    """Last-write-wins under a race: whichever order the two land in, the newer
    blob is what stays stored, and an older request that lands second is told so."""
    user_id = str(uuid.uuid4())
    results, stored = await _race(
        test_engine,
        user_id,
        SettingsPutRequest(encrypted_blob="seed", updated_at=100),
        SettingsPutRequest(encrypted_blob="newer", updated_at=300),
        SettingsPutRequest(encrypted_blob="older", updated_at=200),
    )

    newer, older = results
    assert newer == SettingsPutResponse(updated_at=300, winner="client", encrypted_blob="newer")
    assert older.winner == "client" or (older.encrypted_blob, older.updated_at) == ("newer", 300)
    assert stored == ("newer", 300)
