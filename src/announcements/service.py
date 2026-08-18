import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.announcements.models import Announcement
from src.announcements.schemas import AnnouncementCreateRequest, AnnouncementOut
from src.auth.models import Profile

# How far back a fresh device is told about. A first launch should arrive with
# what is current, not a changelog of every notice the service has ever posted.
LOOKBACK_MS = 30 * 24 * 60 * 60 * 1000

# Ceiling on one read, so a burst of broadcasts cannot hand a client an
# unbounded list. Newest first, so what gets cut is the least current.
MAX_RETURNED = 100


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def to_out(row: Announcement) -> AnnouncementOut:
    return AnnouncementOut(
        id=str(row.id),
        kind=row.kind,
        title=row.title,
        body=row.body,
        data=row.data or {},
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


async def list_for_user(db: AsyncSession, user_id: str, since: int | None) -> list[Announcement]:
    """Everything this account is owed: its own rows plus the broadcasts.

    ``since`` is the client's watermark, and the reason dismissing works. Rows
    are server-side records with no per-user read state, so re-reading the same
    window would resurrect anything the user had cleared; the client advances
    its watermark past what it has been handed and never asks again.
    """
    now = _now_ms()
    floor = max(since or 0, now - LOOKBACK_MS)
    stmt = (
        select(Announcement)
        .where(
            or_(Announcement.user_id == uuid.UUID(user_id), Announcement.user_id.is_(None)),
            Announcement.created_at > floor,
            or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
        )
        .order_by(Announcement.created_at.desc())
        .limit(MAX_RETURNED)
    )
    return list((await db.scalars(stmt)).all())


async def create(db: AsyncSession, body: AnnouncementCreateRequest) -> Announcement:
    """Write one announcement. Raises ``ValueError`` if it names an unknown user."""
    target: uuid.UUID | None = None
    if body.user_id:
        try:
            target = uuid.UUID(body.user_id)
        except ValueError as exc:
            raise ValueError("user_id is not a uuid") from exc
        if not await db.scalar(select(Profile.id).where(Profile.id == target)):
            raise ValueError("No such user")

    now = _now_ms()
    row = Announcement(
        id=uuid.uuid4(),
        user_id=target,
        kind=body.kind,
        title=body.title,
        body=body.body,
        data=body.data,
        created_at=now,
        expires_at=now + body.ttl_ms if body.ttl_ms else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def remove(db: AsyncSession, announcement_id: str) -> bool:
    """Stop handing a row out. Devices already told keep their copy - this is
    the server forgetting, not a recall."""
    try:
        target = uuid.UUID(announcement_id)
    except ValueError:
        return False
    row = await db.scalar(select(Announcement).where(Announcement.id == target))
    if not row:
        return False
    await db.delete(row)
    await db.commit()
    return True
