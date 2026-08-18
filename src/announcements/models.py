import uuid

from sqlalchemy import BigInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class Announcement(Base):
    """One server-authored message, addressed to a user or to everyone.

    ``user_id`` null means broadcast. It is one table rather than two because
    the read path is the same question either way ("what is this account owed"),
    and a broadcast is just the row that skipped the address.
    """

    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Null = every user. Indexed for the per-user half of the read query.
    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True, index=True)
    # Maps to a client NotificationKind. Free-form so the server can start using
    # a new kind before every client knows it; unknown values fall back to
    # "announcement" on arrival rather than being dropped.
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="announcement")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Opaque to the server; handed to the client as-is (a link, an id to act on).
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # After this, the row stops being handed out. A notice about Tuesday's
    # maintenance is worse than useless on Friday, and without a stop date every
    # new device would be told about every window the service ever had.
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
