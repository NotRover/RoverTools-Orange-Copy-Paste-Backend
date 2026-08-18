from pydantic import BaseModel, Field


class AnnouncementOut(BaseModel):
    id: str
    kind: str
    title: str
    body: str
    data: dict
    created_at: int
    expires_at: int | None = None


class AnnouncementListResponse(BaseModel):
    announcements: list[AnnouncementOut]


class AnnouncementCreateRequest(BaseModel):
    """Admin-side input. Everything but ``title`` has a sane default so the
    common case - one line to everybody - is a one-field call."""

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=2000)
    kind: str = Field(default="announcement", max_length=40)
    # Null addresses everyone. An explicit id is checked against the profiles
    # table, so a typo'd uuid fails loudly instead of writing a row nobody reads.
    user_id: str | None = None
    data: dict = Field(default_factory=dict)
    # Milliseconds from now. Null keeps it until the read window ages it out.
    ttl_ms: int | None = Field(default=None, gt=0)


class AnnouncementCreateResponse(BaseModel):
    id: str
    delivered_to: str
