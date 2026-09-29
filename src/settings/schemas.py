from typing import Literal

from pydantic import BaseModel


class SettingsOut(BaseModel):
    encrypted_blob: str
    updated_at: int


class SettingsPutRequest(BaseModel):
    encrypted_blob: str
    updated_at: int
    # The `updated_at` of the stored blob this one was merged from, or 0 when the GET
    # found none. Set, it is a precondition on the write; unset, the PUT is
    # last-write-wins. docs/architecture.md section 5.3 has the rule.
    base_updated_at: int | None = None


class SettingsPutResponse(BaseModel):
    updated_at: int
    winner: Literal["client", "server"]
    encrypted_blob: str
