import uuid

from sqlalchemy import BigInteger, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    encrypted_blob: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
