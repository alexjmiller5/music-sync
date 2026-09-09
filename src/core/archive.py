"""Archive the raw Spotify pull to R2 through Cloudflare's REST API (httpx only)."""

from datetime import datetime
from urllib.parse import quote
from uuid import uuid4

import httpx

from core.config import Settings


def key_for(now: datetime) -> str:
    return f"raw/spotify-pull/{now.strftime('%Y-%m-%dT%H%M%SZ')}-{uuid4().hex}.json.gz"


def put(settings: Settings, key: str, data: bytes, http: httpx.Client | None = None) -> None:
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{settings.r2_account_id}"
        f"/r2/buckets/{settings.r2_bucket}/objects/{quote(key, safe='')}"
    )
    r = (http or httpx.Client(timeout=120)).put(
        url,
        content=data,
        headers={
            "Authorization": f"Bearer {settings.r2_api_token}",
            "Content-Type": "application/gzip",
        },
    )
    r.raise_for_status()


# Outside raw/ so raw-backup lifecycle expiration cannot discard retry evidence.
PENDING_KEY = "music-sync/pending-reconcile.json.gz"


def get(settings: Settings, key: str, http: httpx.Client | None = None) -> bytes | None:
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{settings.r2_account_id}"
        f"/r2/buckets/{settings.r2_bucket}/objects/{quote(key, safe='')}"
    )
    r = (http or httpx.Client(timeout=120)).get(
        url, headers={"Authorization": f"Bearer {settings.r2_api_token}"}
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content
