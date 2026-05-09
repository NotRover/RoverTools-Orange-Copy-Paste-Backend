from typing import Literal

from pydantic import BaseModel


class SettingsOut(BaseModel):
    encrypted_blob: str
    updated_at: int


class SettingsPutRequest(BaseModel):
    encrypted_blob: str
    updated_at: int


class SettingsPutResponse(BaseModel):
    updated_at: int
    winner: Literal["client", "server"]
    encrypted_blob: str
