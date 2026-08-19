
from pydantic import BaseModel


class RequestUploadBody(BaseModel):
    mime_type: str
    size_bytes: int
    checksum: str  # SHA-256 hex


class RequestUploadResponse(BaseModel):
    blob_key: str
    presigned_put_url: str
    expires_in_seconds: int


class ConfirmUploadBody(BaseModel):
    blob_key: str


class ReleaseUploadBody(BaseModel):
    blob_key: str


class DownloadUrlResponse(BaseModel):
    presigned_get_url: str
    expires_in_seconds: int


class QuotaResponse(BaseModel):
    used_bytes: int
    quota_bytes: int
    # Two more ceilings the client cannot see on its own. Storage is only one of
    # the ways a push gets refused, and a limit the user meets for the first
    # time as an error is a limit they were never told about.
    entry_count: int
    entry_limit: int
    max_entry_bytes: int
