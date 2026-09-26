from typing import Literal

from pydantic import BaseModel, Field

# The 5 MB per-blob hard cap, refused as a 422 on the request body.
MAX_BLOB_BYTES = 5_242_880

# What the client uploads: an image's own type for image entries (see the app's
# `mime_from_path` / data-URL parsing) and `application/zip` for file entries.
# `application/octet-stream` is the generic fallback. The bytes are ciphertext
# either way; the type only decides what a store would serve them as.
AllowedMime = Literal[
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/bmp",
    "application/zip",
    "application/octet-stream",
]


class RequestUploadBody(BaseModel):
    mime_type: AllowedMime
    size_bytes: int = Field(gt=0, le=MAX_BLOB_BYTES)
    checksum: str = Field(pattern=r"^[0-9a-fA-F]{64}$")  # SHA-256 hex


class RequestUploadResponse(BaseModel):
    blob_key: str
    presigned_put_url: str
    expires_in_seconds: int


class ConfirmUploadBody(BaseModel):
    blob_key: str = Field(max_length=128)


class ReleaseUploadBody(BaseModel):
    blob_key: str = Field(max_length=128)


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
