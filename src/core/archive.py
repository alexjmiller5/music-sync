"""Archive raw pulls and recovery checkpoints through R2's S3 API."""

from contextlib import closing, nullcontext
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from core.config import Settings


def key_for(now: datetime) -> str:
    return f"raw/spotify-pull/{now.strftime('%Y-%m-%dT%H%M%SZ')}-{uuid4().hex}.json.gz"


def _client(settings: Settings) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=sha256(settings.r2_api_token.encode()).hexdigest(),
        config=Config(
            signature_version="s3v4",
            connect_timeout=120,
            read_timeout=120,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def put(settings: Settings, key: str, data: bytes, s3: BaseClient | None = None) -> None:
    with nullcontext(s3) if s3 is not None else closing(_client(settings)) as client:
        client.put_object(
            Bucket=settings.r2_bucket, Key=key, Body=data, ContentType="application/gzip"
        )


# Outside raw/ so raw-backup lifecycle expiration cannot discard retry evidence.
PENDING_KEY = "music-sync/pending-reconcile.json.gz"


def get(settings: Settings, key: str, s3: BaseClient | None = None) -> bytes | None:
    with nullcontext(s3) if s3 is not None else closing(_client(settings)) as client:
        try:
            response = client.get_object(Bucket=settings.r2_bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        with closing(response["Body"]) as body:
            return body.read()
