import uuid
from typing import Literal

from pydantic import BaseModel, EmailStr


ShareScope = Literal["clipboard", "notes", "both"]


class InviteRequest(BaseModel):
    email: EmailStr
    share_scope: ShareScope = "clipboard"


class InviteResponse(BaseModel):
    share_group_id: uuid.UUID
    invite_code: str
    expires_at: int


class SessionMember(BaseModel):
    user_id: uuid.UUID
    display_name: str
    scope: str
    online: bool = False


class SessionOut(BaseModel):
    share_group_id: uuid.UUID
    members: list[SessionMember]
    my_scope: str
    active_since: int


class ScopeUpdateRequest(BaseModel):
    share_scope: ShareScope
