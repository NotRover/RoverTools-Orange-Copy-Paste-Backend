import boto3
from botocore.config import Config

from src.config import settings

_PRESIGNED_PUT_TTL = 300    # 5 minutes
_PRESIGNED_GET_TTL = 3600   # 1 hour
_MAX_BLOB_BYTES = 5_242_880  # 5 MB hard cap


def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
        config=Config(signature_version="s3v4"),
    )


def generate_presigned_put(blob_key: str, mime_type: str) -> str:
    client = _client()
    return client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": blob_key,
            "ContentType": mime_type,
        },
        ExpiresIn=_PRESIGNED_PUT_TTL,
    )


def generate_presigned_get(blob_key: str) -> str:
    client = _client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": blob_key},
        ExpiresIn=_PRESIGNED_GET_TTL,
    )


def delete_object(blob_key: str) -> None:
    client = _client()
    client.delete_object(Bucket=settings.s3_bucket, Key=blob_key)
