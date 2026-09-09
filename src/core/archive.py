"""Retained raw pulls through life-data; recovery checkpoints in project-owned R2."""

from contextlib import closing, nullcontext
from datetime import datetime
from hashlib import sha256
from urllib.parse import quote
from uuid import uuid4

import boto3
import httpx
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from core.config import Settings
from core.hub import USER_AGENT


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
    if key.startswith("raw/"):
        _file_request(settings, "PUT", key, data)
        return
    with nullcontext(s3) if s3 is not None else closing(_client(settings)) as client:
        client.put_object(
            Bucket=settings.r2_bucket, Key=key, Body=data, ContentType="application/gzip"
        )


# Outside raw/ so raw-backup lifecycle expiration cannot discard retry evidence.
PENDING_KEY = "music-sync/pending-reconcile.json.gz"


def get(settings: Settings, key: str, s3: BaseClient | None = None) -> bytes | None:
    if key.startswith("raw/"):
        return _file_request(settings, "GET", key)
    with nullcontext(s3) if s3 is not None else closing(_client(settings)) as client:
        try:
            response = client.get_object(Bucket=settings.r2_bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        with closing(response["Body"]) as body:
            return body.read()


def _file_request(
    settings: Settings, method: str, key: str, data: bytes | None = None
) -> bytes | None:
    response = httpx.request(
        method,
        f"{settings.life_hub_url.rstrip('/')}/v1/files/{quote(key, safe='/')}",
        headers={
            "Authorization": f"Bearer {settings.life_hub_token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/gzip",
        },
        content=data,
        timeout=120,
    )
    if method == "GET" and response.status_code == 404:
        return None
    response.raise_for_status()
    return response.content
