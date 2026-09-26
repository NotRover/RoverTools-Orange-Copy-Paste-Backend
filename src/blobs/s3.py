from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from src.config import settings

PRESIGNED_PUT_TTL = 300  # 5 minutes
_PRESIGNED_GET_TTL = 3600  # 1 hour


def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
        config=Config(signature_version="s3v4"),
    )


def generate_presigned_put(blob_key: str, mime_type: str, size_bytes: int) -> str:
    """A PUT URL for exactly `size_bytes`.

    `ContentLength` is part of the signature, so the store refuses a body of any
    other length: the declared size the quota was charged for is the size that
    can land. The client's HTTP library sets the header from the body it sends.
    """
    client = _client()
    return client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": blob_key,
            "ContentType": mime_type,
            "ContentLength": size_bytes,
        },
        ExpiresIn=PRESIGNED_PUT_TTL,
    )


def generate_presigned_get(blob_key: str) -> str:
    """A GET URL whose response is always a download, never a rendered page.

    The bytes are ciphertext, so nothing legitimate ever wants them shown inline;
    forcing `attachment` means an object uploaded with a misleading type cannot
    be opened as HTML on the storage origin.
    """
    client = _client()
    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": blob_key,
            "ResponseContentDisposition": "attachment",
        },
        ExpiresIn=_PRESIGNED_GET_TTL,
    )


@dataclass(frozen=True)
class ObjectHead:
    size_bytes: int
    # Base64 SHA-256 as the store reports it, when the upload carried one.
    checksum_sha256: str | None


def head_object(blob_key: str) -> ObjectHead | None:
    """What actually landed under `blob_key`, or None when nothing did."""
    client = _client()
    try:
        head = client.head_object(Bucket=settings.s3_bucket, Key=blob_key, ChecksumMode="ENABLED")
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    return ObjectHead(size_bytes=int(head.get("ContentLength", 0)), checksum_sha256=head.get("ChecksumSHA256"))


def delete_object(blob_key: str) -> None:
    client = _client()
    client.delete_object(Bucket=settings.s3_bucket, Key=blob_key)
